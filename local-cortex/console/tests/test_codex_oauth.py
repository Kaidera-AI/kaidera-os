"""codex_oauth — the exact, testable half: storage, expiry, bearer, JWT account-id.

The OAuth HTTP flows (device-code, PKCE exchange, refresh) are LIVE-UNVERIFIED against the
undocumented codex endpoints and are not asserted here beyond structure. See
docs/2026-06-13-codex-oauth-design.md.
"""

from __future__ import annotations

import base64
import json

import pytest

from app import codex_oauth


class _FakeDB:
    def __init__(self):
        self.store: dict = {}
    def load_app_settings(self):
        return dict(self.store)
    def upsert_app_settings(self, items):
        self.store.update(items)
        return True
    def delete_app_setting(self, key):
        self.store.pop(key, None)
        return True


@pytest.fixture
def fake_db(monkeypatch):
    db = _FakeDB()
    monkeypatch.setattr(codex_oauth, "_db", db)
    return db


def _jwt(payload: dict) -> str:
    def seg(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return f"{seg({'alg': 'none'})}.{seg(payload)}.sig"


def test_storage_round_trip(fake_db):
    assert codex_oauth.load_codex_oauth_blob() is None
    assert codex_oauth.is_logged_in() is False
    blob = {"access_token": "AT", "refresh_token": "RT", "expires_at": 0.0}
    assert codex_oauth.save_codex_oauth_blob(blob) is True
    assert codex_oauth.load_codex_oauth_blob() == blob
    assert codex_oauth.is_logged_in() is True
    assert codex_oauth.clear_codex_oauth_blob() is True
    assert codex_oauth.load_codex_oauth_blob() is None
    assert codex_oauth.is_logged_in() is False


def test_load_blob_when_db_unavailable(monkeypatch):
    class Down:
        def load_app_settings(self):
            return codex_oauth._UNAVAILABLE
    monkeypatch.setattr(codex_oauth, "_db", Down())
    assert codex_oauth.load_codex_oauth_blob() is None
    assert codex_oauth.is_logged_in() is False


def test_needs_refresh():
    now = codex_oauth._now()
    assert codex_oauth.needs_refresh({"expires_at": now + 1000}) is False
    assert codex_oauth.needs_refresh({"expires_at": now + 100}) is True   # within margin
    assert codex_oauth.needs_refresh({"expires_at": now - 10}) is True    # already expired
    assert codex_oauth.needs_refresh({}) is True                          # missing expiry
    assert codex_oauth.needs_refresh({"expires_at": "bad"}) is True       # unparseable


def test_account_id_from_id_token():
    assert codex_oauth.account_id_from_id_token(_jwt({"chatgpt_account_id": "acct-1"})) == "acct-1"
    assert codex_oauth.account_id_from_id_token(_jwt({"account_id": "acct-2"})) == "acct-2"
    nested = _jwt({"https://api.openai.com/auth": {"chatgpt_account_id": "acct-3"}})
    assert codex_oauth.account_id_from_id_token(nested) == "acct-3"
    assert codex_oauth.account_id_from_id_token("not-a-jwt") == ""
    assert codex_oauth.account_id_from_id_token(_jwt({"sub": "x"})) == ""


def test_blob_from_token_response_carries_forward_refresh():
    data = {"access_token": "newAT", "expires_in": 3600,
            "id_token": _jwt({"chatgpt_account_id": "acct-9"})}
    blob = codex_oauth._blob_from_token_response(data, keep={"refresh_token": "keepRT"})
    assert blob["access_token"] == "newAT"
    assert blob["refresh_token"] == "keepRT"          # refresh response omitted it → kept
    assert blob["chatgpt_account_id"] == "acct-9"
    assert blob["expires_at"] > codex_oauth._now()


def test_parse_codex_cli_device_output_strips_ansi():
    text = """
    Follow these steps to sign in with ChatGPT using device code authorization:
       \x1b[94mhttps://auth.openai.com/codex/device\x1b[0m
       \x1b[94m61ET-9NJ4B\x1b[0m
    """
    assert codex_oauth._parse_codex_device_output(text) == (
        "https://auth.openai.com/codex/device",
        "61ET-9NJ4B",
    )


def test_codex_cli_status_reports_missing(monkeypatch):
    monkeypatch.setattr(codex_oauth, "_codex_cli_path", lambda: "")
    assert codex_oauth.codex_cli_status() == {
        "available": False,
        "logged_in": False,
        "auth_method": "",
        "message": "codex CLI not found on PATH.",
    }


def test_codex_cli_status_detects_chatgpt_login(monkeypatch):
    class Result:
        returncode = 0
        stdout = "Logged in with ChatGPT\n"

    monkeypatch.setattr(codex_oauth, "_codex_cli_path", lambda: "/bin/codex")
    monkeypatch.setattr(codex_oauth.subprocess, "run", lambda *_a, **_kw: Result())
    status = codex_oauth.codex_cli_status()
    assert status["available"] is True
    assert status["logged_in"] is True
    assert status["auth_method"] == "chatgpt"


@pytest.mark.asyncio
async def test_get_bearer_empty_when_logged_out(fake_db):
    assert await codex_oauth.get_codex_oauth_bearer() == ""


@pytest.mark.asyncio
async def test_get_bearer_returns_token_when_fresh(fake_db):
    fake_db.store[codex_oauth.CODEX_OAUTH_KEY] = {
        "access_token": "AT", "refresh_token": "RT", "expires_at": codex_oauth._now() + 9999,
    }
    assert await codex_oauth.get_codex_oauth_bearer() == "AT"


@pytest.mark.asyncio
async def test_get_bearer_refreshes_when_stale(fake_db, monkeypatch):
    fake_db.store[codex_oauth.CODEX_OAUTH_KEY] = {
        "access_token": "OLD", "refresh_token": "RT", "expires_at": codex_oauth._now() - 1,
    }
    async def fake_refresh(blob):
        return {"access_token": "FRESH", "refresh_token": "RT",
                "expires_at": codex_oauth._now() + 9999}
    monkeypatch.setattr(codex_oauth, "refresh_codex_oauth_token", fake_refresh)
    assert await codex_oauth.get_codex_oauth_bearer() == "FRESH"


@pytest.mark.asyncio
async def test_poll_device_flow_reports_done_from_cli_status(monkeypatch):
    codex_oauth._LOGIN_FLOWS.clear()
    codex_oauth._LOGIN_FLOWS["flow-1"] = {
        "returncode": None,
        "tail": [],
        "user_code": "61ET-9NJ4B",
        "verification_uri": "https://auth.openai.com/codex/device",
    }
    monkeypatch.setattr(
        codex_oauth,
        "codex_cli_status",
        lambda: {
            "available": True,
            "logged_in": True,
            "auth_method": "chatgpt",
            "message": "Logged in with ChatGPT",
        },
    )
    try:
        result = await codex_oauth.poll_device_flow("flow-1", "61ET-9NJ4B")
    finally:
        codex_oauth._LOGIN_FLOWS.clear()
    assert result == {
        "status": "done",
        "method": "codex_cli",
        "auth_method": "chatgpt",
        "message": "Logged in with ChatGPT",
    }
