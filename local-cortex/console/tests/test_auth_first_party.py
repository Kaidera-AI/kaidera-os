from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import auth


class _FakeUniqueViolation(Exception):
    """Stand-in for asyncpg.UniqueViolationError — carries the 23505 SQLSTATE the
    endpoint's structural check (`_is_unique_violation`) keys off."""

    sqlstate = "23505"

    def __init__(self, constraint: str = "auth_users_email_key") -> None:
        super().__init__(constraint)


# Name it so `_is_unique_violation` (which matches on __class__.__name__) also fires.
_FakeUniqueViolation.__name__ = "UniqueViolationError"


class MemoryAuthStore:
    def __init__(self) -> None:
        self.users: list[dict] = []
        self.email_challenges: list[dict] = []
        self.sessions: list[dict] = []
        self.audits: list[dict] = []

    async def count_users(self) -> int:
        return len(self.users)

    async def get_user_by_email(self, email: str):
        email = auth.normalize_email(email)
        return next((u for u in self.users if u["email"] == email), None)

    async def get_user_by_id(self, user_id: str):
        return next((u for u in self.users if u["id"] == user_id), None)

    async def create_user(self, email: str, *, role: str = "user", display_name=None, verified=False):
        existing = await self.get_user_by_email(email)
        if existing:
            return existing
        row = {
            "id": f"user_{len(self.users) + 1}",
            "email": auth.normalize_email(email),
            "display_name": display_name,
            "role": role,
            "status": "active",
            "email_verified_at": datetime.now(timezone.utc) if verified else None,
            "created_at": datetime.now(timezone.utc),
            "last_login_at": None,
        }
        self.users.append(row)
        return row

    async def list_users(self):
        return list(self.users)

    async def count_active_admins(self) -> int:
        return sum(1 for u in self.users if u["role"] == "admin" and u["status"] == "active")

    async def set_user_role(self, user_id: str, role: str):
        user = await self.get_user_by_id(user_id)
        if user:
            user["role"] = role if role in {"admin", "user"} else "user"
        return user

    async def set_user_status(self, user_id: str, status: str):
        # Mirror the auth_users CHECK constraint: only 'active'/'disabled' are valid (a CheckViolation
        # otherwise in Pg). 'disabled' is the blocked state.
        user = await self.get_user_by_id(user_id)
        if user:
            user["status"] = status if status in {"active", "disabled"} else "active"
        return user

    async def delete_user(self, user_id: str) -> bool:
        before = len(self.users)
        self.users = [u for u in self.users if u["id"] != user_id]
        return len(self.users) < before

    async def update_user_profile(self, user_id: str, *, email=None, display_name=None):
        user = await self.get_user_by_id(user_id)
        if not user:
            return None
        if email is not None:
            email = auth.normalize_email(email)
            clash = next(
                (u for u in self.users if u["email"] == email and u["id"] != user_id), None
            )
            if clash:
                # Mimic asyncpg's unique-violation so the endpoint maps it to a 409.
                raise _FakeUniqueViolation("auth_users_email_key")
            user["email"] = email
        if display_name is not None:
            user["display_name"] = display_name
        return user

    async def save_email_challenge(self, row: dict) -> None:
        self.email_challenges.append(dict(row, attempts=0, consumed_at=None, created_at=auth._now()))

    async def latest_email_challenge(self, email: str):
        email = auth.normalize_email(email)
        live = [
            c for c in self.email_challenges
            if c["email"] == email and c["consumed_at"] is None and c["expires_at"] > auth._now()
        ]
        return live[-1] if live else None

    async def email_challenge_by_token_hash(self, token_hash: str):
        return next(
            (
                c for c in self.email_challenges
                if c["token_hash"] == token_hash
                and c["consumed_at"] is None
                and c["expires_at"] > auth._now()
            ),
            None,
        )

    async def consume_email_challenge(self, challenge_id: str) -> None:
        for c in self.email_challenges:
            if c["id"] == challenge_id:
                c["consumed_at"] = auth._now()

    async def increment_email_attempts(self, challenge_id: str) -> None:
        for c in self.email_challenges:
            if c["id"] == challenge_id:
                c["attempts"] += 1

    async def create_session(self, user_id: str, token_hash: str, expires_at, request) -> None:
        self.sessions.append({"user_id": user_id, "token_hash": token_hash, "expires_at": expires_at})
        user = await self.get_user_by_id(user_id)
        if user:
            user["last_login_at"] = auth._now()

    async def session_by_token_hash(self, token_hash: str):
        sess = next((s for s in self.sessions if s["token_hash"] == token_hash), None)
        if not sess:
            return None
        return await self.get_user_by_id(sess["user_id"])

    async def revoke_session(self, token_hash: str) -> None:
        self.sessions = [s for s in self.sessions if s["token_hash"] != token_hash]

    async def audit(self, event_type: str, **kwargs) -> None:
        self.audits.append({"event_type": event_type, **kwargs})


def fake_request(cookie: str | None = None, store=None):
    # `current_user_from_request` resolves the store from request.app.state.auth_store,
    # so when a test exercises THAT path it passes the store to attach here.
    app = SimpleNamespace(state=SimpleNamespace(auth_store=store, appdb=None))
    return SimpleNamespace(
        headers={"user-agent": "pytest"},
        cookies={auth.COOKIE_NAME: cookie} if cookie else {},
        client=SimpleNamespace(host="127.0.0.1"),
        base_url="http://testserver/",
        url=SimpleNamespace(scheme="http", hostname="testserver", path="/app/", query=""),
        app=app,
    )


def _cookie_value(response) -> str:
    """Pull the session cookie's raw value out of a JSONResponse's Set-Cookie header."""
    raw = response.headers["set-cookie"]
    # Set-Cookie: kaidera_session=<token>; Path=/; ...
    seg = raw.split(";", 1)[0]
    name, _, value = seg.partition("=")
    assert name.strip() == auth.COOKIE_NAME
    return value.strip()


@pytest.fixture(autouse=True)
def auth_env(monkeypatch):
    monkeypatch.setenv("KAIDERA_AUTH_SECRET", "test-secret")
    monkeypatch.setenv("KAIDERA_AUTH_EMAIL_DELIVERY", "dev")
    monkeypatch.setenv("KAIDERA_AUTH_ENABLED", "0")


@pytest.mark.asyncio
async def test_email_request_for_first_user_waits_for_verification():
    store = MemoryAuthStore()
    out = await auth.request_email_login(
        fake_request(),
        {"email": "Admin@Example.com"},
        store,
    )

    assert out["ok"] is True
    assert out["dev_code"]
    assert out["dev_link"].startswith("http://testserver/auth/email/consume")
    assert store.users == []
    assert len(store.email_challenges) == 1


@pytest.mark.asyncio
async def test_email_code_verification_consumes_challenge_and_sets_session_cookie():
    store = MemoryAuthStore()
    req = fake_request()
    out = await auth.request_email_login(req, {"email": "admin@example.com"}, store)

    response = await auth.verify_email_login(
        req,
        {"email": "admin@example.com", "code": out["dev_code"]},
        store,
    )

    body = json.loads(response.body)
    assert body["ok"] is True
    assert body["user"]["is_admin"] is True
    assert store.users[0]["email"] == "admin@example.com"
    assert store.users[0]["role"] == "admin"
    assert store.email_challenges[0]["consumed_at"] is not None
    assert len(store.sessions) == 1
    assert auth.COOKIE_NAME in response.headers["set-cookie"]


@pytest.mark.asyncio
async def test_unknown_email_after_bootstrap_does_not_create_login_challenge():
    store = MemoryAuthStore()
    await store.create_user("admin@example.com", role="admin", verified=True)

    out = await auth.request_email_login(
        fake_request(),
        {"email": "stranger@example.com"},
        store,
    )

    assert out == {"ok": True, "sent": True, "delivery": "none"}
    assert store.email_challenges == []
    assert store.audits[-1]["event_type"] == "auth.login_request_ignored"


def test_passkeys_use_py_webauthn_dependency():
    reqs = Path(__file__).resolve().parents[1] / "requirements.txt"
    assert "webauthn==2.8.0" in reqs.read_text()
    assert "fastapi-users" not in reqs.read_text()


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("/app/", "/app/"),
        ("/app/?project=kaidera-os", "/app/?project=kaidera-os"),
        ("/app/#/settings", "/app/#/settings"),
        (None, "/app/"),
        ("", "/app/"),
        ("app/", "/app/"),
        ("https://evil.example/app", "/app/"),
        ("//evil.example/app", "/app/"),
        ("///evil.example/app", "/app/"),
        ("/\\evil.example/app", "/app/"),
        ("/%5Cevil.example/app", "/app/"),
        ("/%2Fevil.example/app", "/app/"),
        ("/%5C%5Cevil.example/app", "/app/"),
        ("/auth/login", "/app/"),
        ("/auth/email/consume", "/app/"),
    ],
)
def test_safe_next_allows_only_same_origin_app_paths(value, expected):
    assert auth.safe_next(value) == expected


def _link_token(out):
    from urllib.parse import urlsplit, parse_qs
    return parse_qs(urlsplit(out["dev_link"]).query)["token"][0]


@pytest.mark.asyncio
async def test_email_link_get_does_not_consume_token():
    """GET /auth/email/consume must render a confirm page WITHOUT consuming the one-time token —
    email security scanners (MS365 Safe Links etc.) pre-fetch the URL and must not spend it. This is
    the core regression guard for the reported 'the code works but the link does not'."""
    store = MemoryAuthStore()
    req = fake_request()
    out = await auth.request_email_login(req, {"email": "admin@example.com"}, store)
    token = _link_token(out)

    page = await auth.consume_email_link_page(req, token, None)

    assert page.status_code == 200
    assert token in page.body.decode()  # embedded for the POST the human will fire
    assert store.email_challenges[0]["consumed_at"] is None  # NOT consumed by the GET
    assert store.sessions == []
    assert store.users == []


@pytest.mark.asyncio
async def test_email_link_post_consumes_and_sets_session_cookie():
    """POST /auth/email/consume (the human click) consumes the token, creates the first admin, and
    sets the session cookie — returning the safe next target for the browser to navigate to."""
    store = MemoryAuthStore()
    req = fake_request()
    out = await auth.request_email_login(req, {"email": "admin@example.com"}, store)
    token = _link_token(out)

    response = await auth.consume_email_link(req, {"token": token, "next": "/app/"}, store)

    body = json.loads(response.body)
    assert body["ok"] is True
    assert body["next"] == "/app/"
    assert body["user"]["is_admin"] is True
    assert auth.COOKIE_NAME in response.headers["set-cookie"]
    assert store.email_challenges[0]["consumed_at"] is not None
    assert len(store.sessions) == 1
    assert store.users[0]["email"] == "admin@example.com"
    assert store.users[0]["role"] == "admin"


@pytest.mark.asyncio
async def test_email_link_post_rejects_already_spent_token():
    """A second POST with the same token (re-click, or a scanner that does POST) is rejected 401 —
    single-use is preserved now that consumption moved to the POST."""
    store = MemoryAuthStore()
    req = fake_request()
    out = await auth.request_email_login(req, {"email": "admin@example.com"}, store)
    token = _link_token(out)

    await auth.consume_email_link(req, {"token": token, "next": "/app/"}, store)
    with pytest.raises(auth.HTTPException) as exc:
        await auth.consume_email_link(req, {"token": token, "next": "/app/"}, store)
    assert exc.value.status_code == 401


# ---------------------------------------------------------------------------
#  Admin panel + profile (user CRUD, both guards)
# ---------------------------------------------------------------------------


async def _seed(store, email, *, role="user", status="active"):
    """Create a user via the store and force a known role/status (the bootstrap-admin
    rule in create_user only applies to the first user)."""
    u = await store.create_user(email, role=role, verified=True)
    u["role"] = role
    u["status"] = status
    return u


def admin_user():
    return {"id": "user_admin", "email": "admin@example.com", "role": "admin", "status": "active"}


@pytest.mark.asyncio
async def test_list_users_returns_all_via_admin_endpoint():
    store = MemoryAuthStore()
    await _seed(store, "admin@example.com", role="admin")
    await _seed(store, "bob@example.com", role="user")

    out = await auth.list_users(admin=admin_user(), store=store)

    emails = {u["email"] for u in out["users"]}
    assert emails == {"admin@example.com", "bob@example.com"}
    bob = next(u for u in out["users"] if u["email"] == "bob@example.com")
    assert bob["role"] == "user"
    assert bob["status"] == "active"


@pytest.mark.asyncio
async def test_admin_create_then_toggle_role():
    store = MemoryAuthStore()
    await _seed(store, "admin@example.com", role="admin")
    created = await auth.create_user(
        {"email": "new@example.com", "role": "user"}, admin=admin_user(), store=store
    )
    uid = created["user"]["id"]
    assert created["user"]["role"] == "user"

    promoted = await auth.update_user(
        fake_request(), uid, {"role": "admin"}, admin=admin_user(), store=store
    )
    assert promoted["user"]["role"] == "admin"

    demoted = await auth.update_user(
        fake_request(), uid, {"role": "user"}, admin=admin_user(), store=store
    )
    assert demoted["user"]["role"] == "user"


@pytest.mark.asyncio
async def test_admin_block_then_unblock_user():
    store = MemoryAuthStore()
    await _seed(store, "admin@example.com", role="admin")
    target = await _seed(store, "bob@example.com", role="user")

    # The UI sends the friendly "blocked"; the endpoint normalizes it to the schema's "disabled".
    blocked = await auth.update_user(
        fake_request(), target["id"], {"status": "blocked"}, admin=admin_user(), store=store
    )
    assert blocked["user"]["status"] == "disabled"

    unblocked = await auth.update_user(
        fake_request(), target["id"], {"status": "active"}, admin=admin_user(), store=store
    )
    assert unblocked["user"]["status"] == "active"


@pytest.mark.asyncio
async def test_status_disabled_value_accepted_directly():
    """The schema value 'disabled' is accepted as-is (not only the 'blocked' alias)."""
    store = MemoryAuthStore()
    await _seed(store, "admin@example.com", role="admin")
    target = await _seed(store, "bob@example.com", role="user")
    out = await auth.update_user(
        fake_request(), target["id"], {"status": "disabled"}, admin=admin_user(), store=store
    )
    assert out["user"]["status"] == "disabled"


@pytest.mark.asyncio
async def test_guard_a_blocked_user_cannot_authenticate():
    """A user with a live session cookie who is then BLOCKED must not resolve to a current
    user — current_user_from_request rejects a non-active status (immediate effect)."""
    store = MemoryAuthStore()
    req = fake_request()
    out = await auth.request_email_login(req, {"email": "admin@example.com"}, store)
    # First user bootstraps as admin; verify to mint a session cookie.
    resp = await auth.verify_email_login(
        req, {"email": "admin@example.com", "code": out["dev_code"]}, store
    )
    cookie = _cookie_value(resp)
    # Add a second user so blocking the FIRST one below isn't a last-admin case.
    await _seed(store, "second@example.com", role="admin")

    # Sanity: the cookie resolves while active.
    assert (await auth.current_user_from_request(fake_request(cookie, store))) is not None

    # Block the user (schema status 'disabled'), then the SAME cookie must no longer authenticate.
    user = await store.get_user_by_email("admin@example.com")
    await store.set_user_status(user["id"], "disabled")
    assert (await auth.current_user_from_request(fake_request(cookie, store))) is None


@pytest.mark.asyncio
async def test_guard_b_cannot_demote_last_admin():
    store = MemoryAuthStore()
    only = await _seed(store, "admin@example.com", role="admin")

    with pytest.raises(auth.HTTPException) as exc:
        await auth.update_user(
            fake_request(), only["id"], {"role": "user"}, admin=admin_user(), store=store
        )
    assert exc.value.status_code == 409
    assert exc.value.detail == "cannot_demote_last_admin"
    # Unchanged.
    assert (await store.get_user_by_id(only["id"]))["role"] == "admin"


@pytest.mark.asyncio
async def test_guard_b_cannot_block_last_admin():
    store = MemoryAuthStore()
    only = await _seed(store, "admin@example.com", role="admin")

    with pytest.raises(auth.HTTPException) as exc:
        await auth.update_user(
            fake_request(), only["id"], {"status": "blocked"}, admin=admin_user(), store=store
        )
    assert exc.value.status_code == 409
    assert exc.value.detail == "cannot_block_last_admin"
    assert (await store.get_user_by_id(only["id"]))["status"] == "active"


@pytest.mark.asyncio
async def test_guard_b_cannot_delete_last_admin():
    store = MemoryAuthStore()
    only = await _seed(store, "admin@example.com", role="admin")

    with pytest.raises(auth.HTTPException) as exc:
        await auth.delete_user(only["id"], admin=admin_user(), store=store)
    assert exc.value.status_code == 409
    assert exc.value.detail == "cannot_delete_last_admin"
    assert await store.get_user_by_id(only["id"]) is not None


@pytest.mark.asyncio
async def test_can_demote_admin_when_another_admin_exists():
    store = MemoryAuthStore()
    a1 = await _seed(store, "admin1@example.com", role="admin")
    await _seed(store, "admin2@example.com", role="admin")

    out = await auth.update_user(
        fake_request(), a1["id"], {"role": "user"}, admin=admin_user(), store=store
    )
    assert out["user"]["role"] == "user"


@pytest.mark.asyncio
async def test_delete_non_last_admin_user():
    store = MemoryAuthStore()
    await _seed(store, "admin@example.com", role="admin")
    target = await _seed(store, "bob@example.com", role="user")

    out = await auth.delete_user(target["id"], admin=admin_user(), store=store)
    assert out["removed"] is True
    assert await store.get_user_by_id(target["id"]) is None


@pytest.mark.asyncio
async def test_profile_update_changes_email_and_name():
    store = MemoryAuthStore()
    me = await _seed(store, "me@example.com", role="user")

    out = await auth.update_profile(
        {"email": "me-new@example.com", "display_name": "My Name"}, user=me, store=store
    )
    assert out["user"]["email"] == "me-new@example.com"
    assert out["user"]["name"] == "My Name"
    refreshed = await store.get_user_by_id(me["id"])
    assert refreshed["email"] == "me-new@example.com"
    assert refreshed["display_name"] == "My Name"


@pytest.mark.asyncio
async def test_profile_update_rejects_invalid_email():
    store = MemoryAuthStore()
    me = await _seed(store, "me@example.com", role="user")
    with pytest.raises(auth.HTTPException) as exc:
        await auth.update_profile({"email": "not-an-email"}, user=me, store=store)
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_profile_update_rejects_duplicate_email():
    store = MemoryAuthStore()
    await _seed(store, "taken@example.com", role="user")
    me = await _seed(store, "me@example.com", role="user")

    with pytest.raises(auth.HTTPException) as exc:
        await auth.update_profile({"email": "taken@example.com"}, user=me, store=store)
    assert exc.value.status_code == 409
    assert exc.value.detail == "email_already_in_use"


@pytest.mark.asyncio
async def test_update_user_unknown_id_is_404():
    store = MemoryAuthStore()
    await _seed(store, "admin@example.com", role="admin")
    with pytest.raises(auth.HTTPException) as exc:
        await auth.update_user(
            fake_request(), "user_missing", {"role": "user"}, admin=admin_user(), store=store
        )
    assert exc.value.status_code == 404
