from __future__ import annotations

import datetime as dt
import json

import httpx
import pytest


def test_bedrock_provider_is_testable_and_uses_region_aliases():
    from app import provider_check, providers, providers_env

    assert provider_check.is_testable("aws_secret_access_key") is True
    assert providers_env._SETTING_ENV_VAR["aws_region"] == "AWS_REGION"
    assert providers._SETTING_ENV_VAR["aws_region"] == "AWS_REGION"
    assert "AWS_DEFAULT_REGION" in provider_check._env_vars_for_field("aws_region")

    rows = providers.builtin_provider_config({
        "aws_access_key_id": "AKIAEXAMPLE",
        "aws_secret_access_key": "secret",
    })
    bedrock = next(row for row in rows if row["name"] == "bedrock")

    assert bedrock["key_is_set"] is True
    assert bedrock["testable"] is True
    assert bedrock["key_field"] == "aws_secret_access_key"


def test_bedrock_sigv4_headers_are_deterministic():
    from app import provider_check

    headers = provider_check._bedrock_headers(
        access_key_id="AKIAEXAMPLE",
        secret_access_key="secret",
        region="us-east-1",
        session_token="session-token",
        now=dt.datetime(2026, 6, 25, 12, 34, 56, tzinfo=dt.UTC),
    )

    assert headers["host"] == "bedrock.us-east-1.amazonaws.com"
    assert headers["x-amz-date"] == "20260625T123456Z"
    assert headers["x-amz-security-token"] == "session-token"
    assert "Credential=AKIAEXAMPLE/20260625/us-east-1/bedrock/aws4_request" in headers["Authorization"]
    assert "SignedHeaders=accept;host;x-amz-date;x-amz-security-token" in headers["Authorization"]
    assert "Signature=" in headers["Authorization"]
    assert "secret" not in json.dumps(headers)


@pytest.mark.asyncio
async def test_bedrock_provider_test_requires_access_key_and_secret(monkeypatch):
    from app import provider_check

    monkeypatch.setattr(
        provider_check,
        "_resolve_builtin_key",
        lambda field, value: "secret" if field == "aws_secret_access_key" else "",
    )

    result = await provider_check.test_provider("aws_secret_access_key")

    assert result["ok"] is False
    assert result["status"] == "no_key"
    assert "both AWS access key ID and AWS secret access key" in result["message"]


@pytest.mark.asyncio
async def test_bedrock_provider_test_lists_foundation_models(monkeypatch):
    from app import provider_check

    seen: dict[str, object] = {}

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url, headers):
            seen["url"] = url
            seen["headers"] = headers
            return httpx.Response(200, json={"modelSummaries": [{"modelId": "a"}, {"modelId": "b"}]})

    def resolve(field, value):
        values = {
            "aws_access_key_id": "AKIAEXAMPLE",
            "aws_secret_access_key": value or "secret",
            "aws_region": "eu-west-2",
        }
        return values.get(field, "")

    monkeypatch.setattr(provider_check, "_resolve_builtin_key", resolve)
    monkeypatch.setattr(provider_check, "_resolve_aws_session_token", lambda: "")
    monkeypatch.setattr(provider_check.httpx, "AsyncClient", FakeClient)

    result = await provider_check.test_provider("aws_secret_access_key")

    assert result["ok"] is True
    assert result["status"] == "ok"
    assert "eu-west-2" in result["message"]
    assert "2 foundation models" in result["message"]
    assert seen["url"] == "https://bedrock.eu-west-2.amazonaws.com/foundation-models"
    headers = seen["headers"]
    assert isinstance(headers, dict)
    assert headers["host"] == "bedrock.eu-west-2.amazonaws.com"
    assert "Authorization" in headers
    assert "secret" not in json.dumps(result)


@pytest.mark.asyncio
async def test_bedrock_provider_test_reports_permission_denied(monkeypatch):
    from app import provider_check

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, _url, headers):
            assert "Authorization" in headers
            return httpx.Response(
                403,
                json={"message": "User is not authorized to perform: bedrock:ListFoundationModels"},
                headers={"x-amzn-errortype": "AccessDeniedException"},
            )

    def resolve(field, value):
        values = {
            "aws_access_key_id": "AKIAEXAMPLE",
            "aws_secret_access_key": value or "secret",
            "aws_region": "us-east-1",
        }
        return values.get(field, "")

    monkeypatch.setattr(provider_check, "_resolve_builtin_key", resolve)
    monkeypatch.setattr(provider_check, "_resolve_aws_session_token", lambda: "")
    monkeypatch.setattr(provider_check.httpx, "AsyncClient", FakeClient)

    result = await provider_check.test_provider("aws_secret_access_key")

    assert result["ok"] is False
    assert result["status"] == "permission_denied"
    assert "authenticated" in result["message"]
    assert "ListFoundationModels" in result["message"]
