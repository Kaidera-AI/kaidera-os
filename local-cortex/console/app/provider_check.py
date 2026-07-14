"""Provider API-key connectivity test (R4a follow-up; E007).

A small, dependency-light "does this key actually work?" probe for the System-tab
provider keys (Anthropic / OpenAI / OpenRouter / Fireworks) AND operator-added
custom providers. The System page can STORE a key, but a stored key always renders
masked ("•••• set") — visually identical to a wrong or expired key. This module
makes a cheap, READ-ONLY call to the provider (list models / key info — NEVER a
completion, so it spends no tokens) and reports ok / rejected / unreachable, so the
operator gets real confirmation that a save took AND that the key authenticates.

Server-side only: it reads the REAL stored secret via app.settings (secrets never
reach the browser), and the raw key NEVER appears in the returned dict — only a
boolean + a human-readable message + a coarse status. The route renders that into
the small inline ✓/✗ result partial.

Graceful by design: every failure mode (no key, bad key, network down, unknown
field) returns a structured result dict; nothing raises, so the route can't 500.
The probe is also defensive about the network — a short timeout, a single GET, no
retries — so a dead provider can't hang the console.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import os
from typing import Any

import httpx

from . import providers_env as _providers_env
from . import settings as _settings

# How long to wait on the provider before declaring it unreachable. Short on
# purpose: this is an interactive "click Test, get an answer" probe, not a
# resilient API call. A slow/hung provider returns "couldn't reach", not a stall.
_TIMEOUT_SECS = 8.0

# Built-in provider secret-field key -> how to validate it.
#   label   : human name for the message
#   url     : a CHEAP, READ-ONLY endpoint (model list / key info). A 2xx means the
#             key authenticated; we never POST a completion (zero token spend).
#   auth    : "bearer"  -> Authorization: Bearer <key>
#             "x-api-key" -> x-api-key: <key>  (Anthropic)
#   headers : any extra static headers the endpoint requires.
_BUILTIN: dict[str, dict[str, Any]] = {
    "anthropic_api_key": {
        "label": "Anthropic",
        "url": "https://api.anthropic.com/v1/models",
        "auth": "x-api-key",
        "headers": {"anthropic-version": "2023-06-01"},
    },
    "openai_api_key": {
        "label": "OpenAI",
        "url": "https://api.openai.com/v1/models",
        "auth": "bearer",
    },
    "openrouter_api_key": {
        "label": "OpenRouter",
        # /key returns this key's own rate-limit/credit info — the tightest
        # "is THIS key valid" check (the /models list is public, so it wouldn't
        # actually exercise the credential).
        "url": "https://openrouter.ai/api/v1/key",
        "auth": "bearer",
    },
    "fireworks_api_key": {
        "label": "Fireworks",
        # Fireworks is OpenAI-compatible at /inference/v1; the model list requires
        # the API key, so a 401 here is a wrong/expired key.
        "url": "https://api.fireworks.ai/inference/v1/models",
        "auth": "bearer",
    },
    "groq_api_key": {
        "label": "Groq",
        # OpenAI-compatible; the model list requires the key (docs curl uses it).
        "url": "https://api.groq.com/openai/v1/models",
        "auth": "bearer",
    },
    "siliconflow_api_key": {
        "label": "SiliconFlow",
        # OpenAI-compatible (.com is the international host; .cn is the China host).
        "url": "https://api.siliconflow.com/v1/models",
        "auth": "bearer",
    },
    "dashscope_api_key": {
        "label": "Alibaba DashScope",
        # Qwen via the OpenAI-compatible mode (intl/Singapore host). NOTE: a
        # GET /models is NOT documented for compatible-mode — this is the standard
        # OpenAI path; if DashScope doesn't serve it the probe reports the HTTP code
        # honestly (it won't false-pass). Verify against live before relying on it.
        "url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/models",
        "auth": "bearer",
    },
    "alibaba_cloud_api_key": {
        "label": "Alibaba Cloud",
        # Alibaba Cloud Model Studio uses the same compatible-mode host as DashScope.
        # The probe shares the same caveat: /models is the standard OpenAI path and
        # may not be served; the probe reports the real HTTP code.
        "url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/models",
        "auth": "bearer",
    },
    "deepseek_api_key": {
        "label": "DeepSeek",
        # OpenAI-compatible; /models is a documented endpoint and needs the key.
        "url": "https://api.deepseek.com/v1/models",
        "auth": "bearer",
    },
    "together_api_key": {
        "label": "Together AI",
        # OpenAI-compatible; GET /v1/models is documented + key-scoped.
        "url": "https://api.together.xyz/v1/models",
        "auth": "bearer",
    },
    "cohere_api_key": {
        "label": "Cohere",
        # Cohere's NATIVE v2 API (not OpenAI-compatible), but the list-models
        # endpoint still takes Authorization: Bearer <key>, so the bearer probe works.
        "url": "https://api.cohere.com/v2/models",
        "auth": "bearer",
    },
    "nvidia_api_key": {
        "label": "NVIDIA NIM",
        # Hosted NVIDIA NIM exposes an OpenAI-compatible surface at /v1; the
        # model list is a cheap read-only auth probe.
        "url": "https://integrate.api.nvidia.com/v1/models",
        "auth": "bearer",
    },
    "inception_api_key": {
        "label": "Inception",
        # Mercury — OpenAI-compatible (chat/completions confirmed). A GET /models is
        # NOT explicitly documented; this is the standard OpenAI path, and the probe
        # surfaces the real HTTP code rather than false-passing if it 404s.
        "url": "https://api.inceptionlabs.ai/v1/models",
        "auth": "bearer",
    },
    "moonshot_api_key": {
        "label": "Moonshot AI",
        # Kimi — OpenAI-compatible; the model-list endpoint is documented + key-scoped.
        "url": "https://api.moonshot.ai/v1/models",
        "auth": "bearer",
    },
    "xai_api_key": {
        "label": "xAI",
        # Grok — OpenAI-compatible; GET /v1/models is documented + key-scoped.
        "url": "https://api.x.ai/v1/models",
        "auth": "bearer",
    },
    "ollama_cloud_api_key": {
        "label": "Ollama Cloud",
        # Ollama Cloud (the hosted ollama.com API) exposes the OpenAI-compatible
        # surface at the /v1 path (the same /v1 Ollama serves locally); GET
        # /v1/models lists the models and authenticates the Bearer key — a 401 here
        # is a wrong/expired key.
        "url": "https://ollama.com/v1/models",
        "auth": "bearer",
    },
    # NOTE — two providers are intentionally NOT in _BUILTIN (no honest bearer
    # /models test exists for them); test_provider() special-cases both:
    #   * perplexity_api_key — Perplexity's GET /v1/models is documented PUBLIC
    #     (security: []), so it would 200 even for a wrong/empty key (a false pass);
    #     no cheap authenticating GET is published, so we report "not testable here".
    #   * aws_secret_access_key (Bedrock) — not bearer-compatible. It uses a
    #     dedicated SigV4 ListFoundationModels probe below.
}

# Prefix that marks a custom-provider test target: field = "custom:<provider_id>".
_CUSTOM_PREFIX = "custom:"
_BEDROCK_FIELD = "aws_secret_access_key"
_BEDROCK_LABEL = "Amazon Bedrock"
_BEDROCK_REGION_DEFAULT = "us-east-1"
_AWS_SESSION_TOKEN_ENV_VARS = ("AWS_SESSION_TOKEN", "AWS_SECURITY_TOKEN")


def is_testable(field: str) -> bool:
    """True if `field` names something this module knows how to probe (a built-in
    secret key or a `custom:<id>` target). Lets the template show the Test button
    only where it does something."""
    f = (field or "").strip()
    return f in _BUILTIN or f == _BEDROCK_FIELD or f.startswith(_CUSTOM_PREFIX)


def _result(ok: bool, status: str, message: str, label: str) -> dict[str, Any]:
    """The structured probe result the route hands to the template. NEVER carries
    the raw key — only ok + a coarse status + a human message + the provider label."""
    return {"ok": bool(ok), "status": status, "message": message, "label": label}


def _headers(spec: dict[str, Any], key: str) -> dict[str, str]:
    """Build the request headers for a built-in spec + key (bearer vs x-api-key,
    plus any static extras the endpoint needs)."""
    out: dict[str, str] = dict(spec.get("headers") or {})
    if spec.get("auth") == "x-api-key":
        out["x-api-key"] = key
    else:
        out["Authorization"] = f"Bearer {key}"
    return out


# Built-in field -> the REAL environment variable the harness/.env uses. The
# console settings store is a deliberate sandbox (NOT the real .env), so a provider
# can work at runtime off its .env key while the console store is empty (this is
# exactly the OpenRouter case). Test resolves: typed > console store > process env >
# local-cortex/.env — so the ✓/✗ reflects the key the system ACTUALLY runs with.
# CANONICAL home is app/providers_env.py — re-exported here (same dict object) so the
# module-attribute name `provider_check._ENV_VAR` other code/tests subscript resolves.
# It's the superset map; the probe only ever queries the Bearer-key subset, so the
# extra Bedrock SigV4 keys are inert here.
_ENV_VAR = _providers_env._SETTING_ENV_VAR
_ENV_VAR_ALIASES = _providers_env._SETTING_ENV_ALIASES


# ── Low-level env helpers — delegate to the one shared copy (app/providers_env.py) ─
# The module-private names stay (tests call/patch `provider_check._env_file_value` /
# `provider_check._env_vars_for_field` by attribute); the bodies just delegate so a
# change to HOW provider auth is read happens in exactly ONE place. Byte-identical.


def _env_vars_for_field(field: str) -> tuple[str, ...]:
    """The real env-var name(s) a built-in field resolves to (alias-aware)."""
    return _providers_env.env_vars_for(field)


def _env_file_value(var: str) -> str:
    """Best-effort, read-only read of `var` from local-cortex/.env (self-contained
    mode short-circuits). See app/providers_env.env_file_value."""
    return _providers_env.env_file_value(var)


def _resolve_builtin_key(field: str, value: str | None) -> str:
    """Pick which key to test for a built-in field. Resolution order:
      1. the operator-typed `value` (real, non-mask) — feedback BEFORE a save;
      2. the console settings store (the post-save check);
      3. the process environment variable (direnv / exported .env);
      4. the local-cortex/.env file (the real harness env, deepest fallback).
    Returns "" only when no key is found anywhere."""
    typed = (value or "").strip()
    if typed and typed != _settings.MASK_PLACEHOLDER:
        return typed

    # load_with_secrets(), NOT load(): provider keys live OUTSIDE the System schema,
    # so normalize() inside load() drops them — reading load() here made every saved
    # provider key resolve to "" → the spurious "no key stored" right after a save.
    stored = _settings.load_with_secrets().get(field)
    stored = str(stored).strip() if stored else ""
    if stored:
        return stored

    for var in _env_vars_for_field(field):
        env_val = (os.environ.get(var) or "").strip()
        if env_val:
            return env_val
        file_val = _env_file_value(var)
        if file_val:
            return file_val
    try:
        from . import providers as providers_catalog

        pi_provider = providers_catalog._PI_AUTH_PROVIDER_FOR_SETTING.get(field)
        if pi_provider:
            pi_val = providers_catalog._pi_auth_api_key(pi_provider)
            if pi_val:
                return pi_val
    except Exception:
        pass
    return ""


def _resolve_aws_session_token() -> str:
    """Read an optional AWS session token from env/.env without adding a UI field."""

    for var in _AWS_SESSION_TOKEN_ENV_VARS:
        env_val = (os.environ.get(var) or "").strip()
        if env_val:
            return env_val
        file_val = _env_file_value(var)
        if file_val:
            return file_val
    return ""


def _aws_signing_key(secret_key: str, date_stamp: str, region: str, service: str) -> bytes:
    key = ("AWS4" + secret_key).encode("utf-8")
    for item in (date_stamp, region, service, "aws4_request"):
        key = hmac.new(key, item.encode("utf-8"), hashlib.sha256).digest()
    return key


def _bedrock_headers(
    *,
    access_key_id: str,
    secret_access_key: str,
    region: str,
    session_token: str = "",
    now: _dt.datetime | None = None,
) -> dict[str, str]:
    """Build AWS SigV4 headers for Bedrock ListFoundationModels.

    This avoids a boto3 dependency just for a read-only provider test.
    """

    service = "bedrock"
    host = f"bedrock.{region}.amazonaws.com"
    timestamp = now or _dt.datetime.now(_dt.UTC)
    amz_date = timestamp.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = timestamp.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(b"").hexdigest()
    headers = {
        "accept": "application/json",
        "host": host,
        "x-amz-date": amz_date,
    }
    if session_token:
        headers["x-amz-security-token"] = session_token

    signed_header_names = sorted(headers)
    canonical_headers = "".join(f"{name}:{headers[name]}\n" for name in signed_header_names)
    signed_headers = ";".join(signed_header_names)
    canonical_request = "\n".join([
        "GET",
        "/foundation-models",
        "",
        canonical_headers,
        signed_headers,
        payload_hash,
    ])
    scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256",
        amz_date,
        scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
    ])
    signature = hmac.new(
        _aws_signing_key(secret_access_key, date_stamp, region, service),
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    headers["Authorization"] = (
        "AWS4-HMAC-SHA256 "
        f"Credential={access_key_id}/{scope}, "
        f"SignedHeaders={signed_headers}, "
        f"Signature={signature}"
    )
    return headers


def _aws_error_type(resp: httpx.Response) -> str:
    raw = (
        resp.headers.get("x-amzn-errortype")
        or resp.headers.get("x-amzn-error-type")
        or ""
    )
    return raw.split(":", 1)[0].strip()


def _aws_error_message(resp: httpx.Response) -> str:
    try:
        data = resp.json()
    except ValueError:
        return (resp.text or "").strip()
    if isinstance(data, dict):
        for key in ("message", "Message", "__type"):
            val = data.get(key)
            if val:
                return str(val)
    return ""


async def _test_bedrock(value: str | None = None) -> dict[str, Any]:
    """Read-only Amazon Bedrock auth probe using SigV4 ListFoundationModels."""

    access_key_id = _resolve_builtin_key("aws_access_key_id", None)
    secret_access_key = _resolve_builtin_key("aws_secret_access_key", value)
    region = _resolve_builtin_key("aws_region", None) or _BEDROCK_REGION_DEFAULT
    region = region.strip()
    if not access_key_id or not secret_access_key:
        return _result(
            False,
            "no_key",
            (
                "Amazon Bedrock needs both AWS access key ID and AWS secret access "
                "key before it can be tested."
            ),
            _BEDROCK_LABEL,
        )
    if not region:
        return _result(False, "no_region", "Amazon Bedrock needs an AWS region.", _BEDROCK_LABEL)

    url = f"https://bedrock.{region}.amazonaws.com/foundation-models"
    headers = _bedrock_headers(
        access_key_id=access_key_id,
        secret_access_key=secret_access_key,
        region=region,
        session_token=_resolve_aws_session_token(),
    )
    try:
        async with httpx.AsyncClient(
            timeout=_TIMEOUT_SECS,
            follow_redirects=True,
        ) as client:
            resp = await client.get(url, headers=headers)
    except httpx.TimeoutException:
        return _result(False, "timeout", f"Timed out reaching Amazon Bedrock in {region}.", _BEDROCK_LABEL)
    except httpx.HTTPError as exc:
        return _result(False, "unreachable", f"Couldn't reach Amazon Bedrock in {region}: {exc}", _BEDROCK_LABEL)

    code = resp.status_code
    if 200 <= code < 300:
        try:
            data = resp.json()
        except ValueError:
            data = {}
        models = data.get("modelSummaries") if isinstance(data, dict) else None
        count = len(models) if isinstance(models, list) else 0
        suffix = f" ({count} foundation models visible)." if count else "."
        return _result(True, "ok", f"Amazon Bedrock credentials work in {region}{suffix}", _BEDROCK_LABEL)
    if code == 429:
        return _result(True, "ok", f"Amazon Bedrock credentials valid in {region} (rate-limited, HTTP 429).", _BEDROCK_LABEL)

    err_type = _aws_error_type(resp)
    message = _aws_error_message(resp)
    detail = f" {err_type}" if err_type else ""
    if message:
        detail = f"{detail}: {message}" if detail else f": {message}"
    if code == 403 and err_type == "AccessDeniedException":
        return _result(
            False,
            "permission_denied",
            (
                f"Amazon Bedrock authenticated in {region}, but this IAM principal "
                f"cannot call ListFoundationModels (HTTP 403{detail})."
            ),
            _BEDROCK_LABEL,
        )
    if code in (401, 403):
        return _result(
            False,
            "rejected",
            f"Amazon Bedrock rejected the AWS credentials in {region} (HTTP {code}{detail}).",
            _BEDROCK_LABEL,
        )
    return _result(False, "http_error", f"Amazon Bedrock returned HTTP {code}{detail}.", _BEDROCK_LABEL)


async def _probe(url: str, headers: dict[str, str], label: str) -> dict[str, Any]:
    """Single GET against `url` with `headers`; map the outcome to a result.

    2xx        -> ok (authenticated)
    401 / 403  -> rejected (invalid / expired key)
    429        -> ok-ish (valid but rate-limited — the key DID authenticate)
    other      -> http_error (surfaced with the code)
    timeout / connection error -> unreachable (never raises)."""
    try:
        async with httpx.AsyncClient(
            timeout=_TIMEOUT_SECS, follow_redirects=True
        ) as client:
            resp = await client.get(url, headers=headers)
    except httpx.TimeoutException:
        return _result(False, "timeout", f"Timed out reaching {label}.", label)
    except httpx.HTTPError as exc:
        return _result(False, "unreachable", f"Couldn't reach {label}: {exc}", label)

    code = resp.status_code
    if 200 <= code < 300:
        return _result(True, "ok", f"{label} key works.", label)
    if code in (401, 403):
        return _result(
            False, "rejected", f"{label} rejected the key (HTTP {code}) — invalid or expired.", label
        )
    if code == 429:
        return _result(True, "ok", f"{label} key valid (rate-limited, HTTP 429).", label)
    return _result(False, "http_error", f"{label} returned HTTP {code}.", label)


async def _test_custom(provider_id: str) -> dict[str, Any]:
    """Probe an operator-added custom provider by id. Uses the OpenAI-compatible
    convention: GET {base_url}/models with a Bearer key. Reads the REAL stored
    base_url + key server-side (both are masked in the UI)."""
    pid = (provider_id or "").strip()
    for c in _settings.load_custom_providers():
        if c.get("id") != pid:
            continue
        label = c.get("name") or pid
        base = (c.get("base_url") or "").strip().rstrip("/")
        key = (c.get("api_key") or "").strip()
        if not base:
            return _result(False, "no_url", f"{label} has no base URL to test against.", label)
        if not key:
            return _result(False, "no_key", f"{label} has no stored key — add one first.", label)
        return await _probe(f"{base}/models", {"Authorization": f"Bearer {key}"}, label)
    return _result(False, "not_found", "That custom provider no longer exists.", provider_id)


# Secret fields that are deliberately NOT testable — each returns a clear,
# graceful "why not" instead of a misleading pass/fail (see _BUILTIN note).
#   * Perplexity: its only model-list endpoint is PUBLIC, so it can't validate a key.
_NOT_TESTABLE: dict[str, dict[str, str]] = {
    "perplexity_api_key": {
        "label": "Perplexity",
        "message": (
            "Perplexity's model-list endpoint is public (it doesn't authenticate), "
            "so a key test can't be run here — the key is used at request time."
        ),
    },
}


async def test_provider(field: str, value: str | None = None) -> dict[str, Any]:
    """Test one provider credential and return a structured result.

    `field` is either a built-in secret key (anthropic_api_key / openai_api_key /
    openrouter_api_key / fireworks_api_key / groq_api_key / …) or `custom:<id>` for
    an operator-added provider. `value` is the optional operator-typed key from the
    form (built-ins only): a real value is tested directly (pre-save feedback);
    blank/masked falls back to the stored key. Never raises.

    A couple of provider secrets (Perplexity, the Bedrock SigV4 secret) have no
    honest cheap key test; those return a clear, graceful "not testable here"
    result (status "not_testable") rather than a misleading ✓/✗."""
    f = (field or "").strip()
    if f.startswith(_CUSTOM_PREFIX):
        return await _test_custom(f[len(_CUSTOM_PREFIX):])
    if f == _BEDROCK_FIELD:
        return await _test_bedrock(value)

    special = _NOT_TESTABLE.get(f)
    if special:
        return _result(False, "not_testable", special["message"], special["label"])

    spec = _BUILTIN.get(f)
    if not spec:
        return _result(False, "unsupported", "Testing isn't supported for this field.", f or "provider")

    key = _resolve_builtin_key(f, value)
    if not key:
        bridged = await _pi_bridge_result(f, spec)
        if bridged:
            return bridged
        return _result(
            False, "no_key", f"No {spec['label']} key stored yet — save one, then Test.", spec["label"]
        )
    return await _probe(spec["url"], _headers(spec, key), spec["label"])


async def _pi_bridge_result(field: str, spec: dict[str, Any]) -> dict[str, Any] | None:
    """Treat a host PI extension login as a successful provider test.

    The console container cannot read PI's host-side auth file. The host
    harness-service can report PI-visible provider model groups without returning
    secrets; if the requested provider has rows there, the login is usable.
    """
    try:
        from . import providers as providers_catalog

        provider = providers_catalog._PI_AUTH_PROVIDER_FOR_SETTING.get(field)
        if not provider:
            return None
        group = await providers_catalog._pi_bridge_provider_group(provider)
    except Exception:
        return None
    if not group or not (group.get("rows") or []):
        return None
    label = str(spec.get("label") or provider)
    return _result(
        True,
        "ok",
        f"{label} key works via PI extension login.",
        label,
    )
