"""Feature-gap step 3a — the SETTINGS JSON endpoints (the `[API]` gaps).

The legacy HTML console exposes the System schema (typed + secret-masked), the live
Providers&Models catalog, custom-provider add/remove, a per-key Test probe, and the
per-project workspace (repo_root) editor — but ALL of those are HTML-only, so the
React SPA literally can't reach them. This file pins the JSON mirrors the SPA needs
(`docs/2026-06-06-feature-list-and-gap-analysis.md` §4 + bucket B):

  1. GET  /settings/{project}/system-schema    — the System form as JSON (typed +
                                                  groups), secrets MASKED (never raw).
  2. GET  /settings/{project}/providers         — the live model catalog, grouped by
                                                  provider; graceful-degrade on error.
  3. POST /settings/{project}/custom-providers          — add (name+base_url+api_key)
     POST /settings/{project}/custom-providers/delete   — remove by name/id
  4. POST /settings/{project}/provider-key-test — probe a provider key → {ok, detail}.
  5. POST /settings/{project}/workspace         — set a project's repo_root via the
                                                  admin path (token NEVER in response).

STRICT TDD + the established settings_module style: the pure service is driven with
FAKES (no live providers / Cortex / DB), the api shell wires the concretes. The
LOAD-BEARING contract is secret-masking: a secret field NEVER returns its raw value
(only `is_set` + a masked placeholder) — `test_system_schema_never_leaks_secret`
asserts the raw secret string appears NOWHERE in the JSON.

These tests are written BEFORE the implementation, matching `test_settings_module.py`.
"""

from __future__ import annotations

import pytest

from app.domain.ports import CatalogModel


# ---------------------------------------------------------------------------
#  Fakes — no live providers / Cortex / DB.
# ---------------------------------------------------------------------------


class FakeCatalogPort:
    """Structural `ModelCatalogPort` stand-in: returns scripted `CatalogModel`s.

    `raise_on_list` flips `list_models()` to RAISE so the providers endpoint's
    graceful-degrade (→ empty/partial catalog, never a 500) can be proven even
    though the real adapter never raises."""

    def __init__(self, models=None, *, raise_on_list=False):
        self._models = list(models or [])
        self._raise = raise_on_list
        self.calls: list[str] = []

    async def list_models(self):
        self.calls.append("list_models")
        if self._raise:
            raise RuntimeError("provider fetch blew up")
        return list(self._models)

    async def price_for(self, model_id):  # unused here; present for the Protocol
        self.calls.append("price_for")
        from app.domain.ports import ModelPrice

        return ModelPrice(model_id=model_id)


class FakeCustomStore:
    """Stand-in for the custom-provider store (the SAME surface `app.settings`'s
    custom-provider helpers expose: add / remove / view). In-memory; masks the key
    in `view` exactly like the legacy settings facade (never echoes the raw api_key)."""

    MASK = "•••• set"

    def __init__(self, existing=None):
        self._rows = [dict(r) for r in (existing or [])]
        self.calls: list[tuple[str, tuple]] = []

    def add_custom_provider(self, name, base_url, api_key):
        self.calls.append(("add", (name, base_url, api_key)))
        nm = (name or "").strip()
        if not nm:
            raise ValueError("a provider name is required")
        pid = nm.lower().replace(" ", "-")
        entry = {"id": pid, "name": nm, "base_url": base_url or "", "api_key": api_key or ""}
        self._rows.append(entry)
        return entry

    def remove_custom_provider(self, provider_id):
        self.calls.append(("remove", (provider_id,)))
        pid = (provider_id or "").strip()
        kept = [r for r in self._rows if r["id"] != pid]
        removed = len(kept) != len(self._rows)
        self._rows = kept
        return removed

    def view_custom_providers(self):
        self.calls.append(("view", ()))
        return [
            {
                "id": r["id"],
                "name": r["name"],
                "base_url": r.get("base_url", ""),
                "has_key": bool(r.get("api_key")),
                "key_display": self.MASK if r.get("api_key") else "",
            }
            for r in self._rows
        ]


class FakeKeyTester:
    """Stand-in for `provider_check.test_provider` (async). Returns a scripted
    structured result; records the (field, value) it was probed with so a test can
    assert the value was forwarded (and the secret is not echoed back)."""

    def __init__(self, result=None):
        self._result = result or {"ok": True, "status": "ok", "message": "key works.",
                                  "label": "Anthropic"}
        self.calls: list[tuple] = []

    async def __call__(self, field, value=None):
        self.calls.append((field, value))
        return dict(self._result)


class FakeRepoRootClient:
    """Stand-in for `CortexClient.set_project_repo_root` (the admin PATCH path).

    `error` can be a callable raising the exception to simulate (ValueError for a
    bad path, AdminTokenMissing for no token, httpx errors). Records the call so a
    test can assert the right project/path were forwarded — and that the admin
    token NEVER appears in the endpoint's response."""

    def __init__(self, *, result=None, error=None):
        self._result = result or {}
        self._error = error
        self.calls: list[tuple[str, str]] = []

    async def set_project_repo_root(self, project_key, repo_root):
        self.calls.append((project_key, repo_root))
        if self._error is not None:
            raise self._error
        return dict(self._result)


# Realistic scripted catalog rows (the `CatalogModel` shape the adapter emits).
SAMPLE_MODELS = [
    CatalogModel(
        provider="anthropic", id="claude-opus-4-8", display_name="Claude Opus 4.8",
        type="chat", context_window=200000, max_output=64000,
        reasoning_levels=["low", "medium", "high"],
        price_in_per_mtok=5.0, price_out_per_mtok=25.0, source="merged",
    ),
    CatalogModel(
        provider="anthropic", id="claude-haiku-4-5", display_name="Claude Haiku 4.5",
        type="chat", context_window=200000, reasoning_levels=[],
        price_in_per_mtok=1.0, price_out_per_mtok=5.0, source="live",
    ),
    CatalogModel(
        provider="openai", id="gpt-5.5", display_name="gpt-5.5", type="chat",
        reasoning_levels=["supported"], source="supplement",
    ),
]

# A raw System SCHEMA fragment (the `app.settings.SCHEMA` shape: groups → fields).
SAMPLE_SCHEMA = [
    {
        "id": "cortex", "title": "Cortex connection", "sub": "…", "icon": "<svg/>",
        "open": True,
        "fields": [
            {"key": "cortex_base_url", "label": "Base URL", "type": "text",
             "default": "http://localhost:8501", "hint": "h"},
        ],
    },
    {
        "id": "providers", "title": "Provider API keys", "sub": "…", "icon": "<svg/>",
        "open": True,
        "fields": [
            {"key": "anthropic_api_key", "label": "Anthropic API key",
             "type": "secret", "default": "", "hint": "sk-ant-…"},
            {"key": "fireworks_account_id", "label": "Fireworks account ID",
             "type": "text", "default": "", "hint": "slug"},
        ],
    },
    {
        "id": "app", "title": "App preferences", "sub": "…", "icon": "<svg/>",
        "open": False,
        "fields": [
            {"key": "poll_interval_secs", "label": "Poll interval", "type": "number",
             "default": 10, "hint": "n"},
            {"key": "harness_autostart", "label": "Auto-start", "type": "bool",
             "default": False, "hint": "b"},
        ],
    },
]

# A secret value the masking contract must NEVER leak into the JSON.
SECRET_VALUE = "sk-ant-SUPER-SECRET-do-not-leak-0123456789"


# ===========================================================================
#  1. system-schema — typed System form as JSON, secrets MASKED.
# ===========================================================================


def test_system_schema_uses_the_edition_visible_harness_set(monkeypatch):
    from app import harness
    from app.settings_module import api as settings_api

    monkeypatch.setattr(
        harness,
        "harness_options",
        lambda: [
            {"value": "kaidera", "label": "Kaidera"},
            {"value": "codex", "label": "Codex"},
            {"value": "pi", "label": "PI"},
        ],
    )

    schema = settings_api.get_system_schema()
    field = next(
        field
        for group in schema
        for field in group["fields"]
        if field["key"] == "harness_default"
    )

    assert field["options"] == ["kaidera", "codex", "pi"]


def test_build_system_schema_shape_and_types():
    """The pure `build_system_schema(schema, values)` returns `{groups:[{key,label,
    fields:[{key,label,type,group,help,...}]}]}` with `type ∈ text|number|bool|
    secret|readonly` and each non-secret field's current `value`."""
    from app.settings_module import service as svc

    out = svc.build_system_schema(
        SAMPLE_SCHEMA,
        {"cortex_base_url": "http://localhost:8501", "poll_interval_secs": 30,
         "harness_autostart": True, "fireworks_account_id": "my-acct"},
    )

    assert set(out) == {"groups"}
    groups = {g["key"]: g for g in out["groups"]}
    assert set(groups) == {"cortex", "providers", "app"}
    assert groups["cortex"]["label"] == "Cortex connection"

    fields = {f["key"]: f for g in out["groups"] for f in g["fields"]}
    # types carried through, all within the allowed set
    allowed = {"text", "number", "bool", "secret", "readonly"}
    assert {f["type"] for f in fields.values()} <= allowed
    assert fields["cortex_base_url"]["type"] == "text"
    assert fields["poll_interval_secs"]["type"] == "number"
    assert fields["harness_autostart"]["type"] == "bool"
    # each field declares its group + help, and a label
    assert fields["cortex_base_url"]["group"] == "cortex"
    assert fields["cortex_base_url"]["help"] == "h"
    assert fields["cortex_base_url"]["label"] == "Base URL"
    # non-secret current values are returned
    assert fields["cortex_base_url"]["value"] == "http://localhost:8501"
    assert fields["poll_interval_secs"]["value"] == 30
    assert fields["harness_autostart"]["value"] is True
    assert fields["fireworks_account_id"]["value"] == "my-acct"


def test_build_system_schema_masks_secret_value_and_sets_is_set():
    """A SECRET field returns `is_set` (reflecting presence) + a masked placeholder,
    and NEVER the raw secret value — neither in `value` nor anywhere on the field.
    This is the load-bearing contract."""
    from app.settings_module import service as svc

    # secret present
    out = svc.build_system_schema(SAMPLE_SCHEMA, {"anthropic_api_key": SECRET_VALUE})
    fields = {f["key"]: f for g in out["groups"] for f in g["fields"]}
    sec = fields["anthropic_api_key"]
    assert sec["type"] == "secret"
    assert sec["is_set"] is True
    # masked placeholder present; raw secret absent from EVERY value on the field
    assert sec.get("value", "") == ""           # secrets never seed the input value
    assert SECRET_VALUE not in str(sec)          # not in placeholder/value/anything
    assert sec.get("placeholder") or sec.get("display")  # some masked marker is shown

    # secret absent → is_set False, still no raw value
    out2 = svc.build_system_schema(SAMPLE_SCHEMA, {"anthropic_api_key": ""})
    fields2 = {f["key"]: f for g in out2["groups"] for f in g["fields"]}
    assert fields2["anthropic_api_key"]["is_set"] is False


def test_system_schema_never_leaks_secret_anywhere_in_json():
    """END-TO-END masking proof: with a secret SET in the values, the FULL serialized
    JSON response contains the secret value NOWHERE (the contract `is_set` + mask,
    never the raw secret)."""
    import json

    from app.settings_module import service as svc

    out = svc.build_system_schema(
        SAMPLE_SCHEMA,
        {"anthropic_api_key": SECRET_VALUE, "cortex_base_url": "http://localhost:8501"},
    )
    blob = json.dumps(out)
    assert SECRET_VALUE not in blob
    # and the is_set flag still truthfully reflects the secret IS configured
    fields = {f["key"]: f for g in out["groups"] for f in g["fields"]}
    assert fields["anthropic_api_key"]["is_set"] is True


@pytest.mark.asyncio
async def test_router_system_schema_endpoint_masks_secret():
    """`GET /settings/{project}/system-schema` returns the typed System form as JSON
    with the current values from the store, secrets masked — driven directly with a
    fake port whose app-settings carry a secret value (which must not leak)."""
    from tests.test_settings_module import FakeOpStore

    from app.settings_module import api as settings_api

    store = FakeOpStore(app_settings={
        "anthropic_api_key": SECRET_VALUE,
        "cortex_base_url": "http://localhost:8501",
        "poll_interval_secs": 30,
    })
    result = await settings_api.system_schema_endpoint(
        "kaidera-os", store=store, schema=SAMPLE_SCHEMA,
    )

    assert result["project"] == "kaidera-os"
    fields = {f["key"]: f for g in result["groups"] for f in g["fields"]}
    assert fields["anthropic_api_key"]["is_set"] is True
    assert fields["cortex_base_url"]["value"] == "http://localhost:8501"

    import json
    assert SECRET_VALUE not in json.dumps(result)


@pytest.mark.asyncio
async def test_router_system_schema_endpoint_down_store_uses_defaults():
    """A down store yields the schema with each field's DEFAULT value (and secrets
    `is_set=false`) rather than a 500 — the System form still renders."""
    from tests.test_settings_module import FakeOpStore

    from app.settings_module import api as settings_api

    result = await settings_api.system_schema_endpoint(
        "kaidera-os", store=FakeOpStore(down=True), schema=SAMPLE_SCHEMA,
    )
    fields = {f["key"]: f for g in result["groups"] for f in g["fields"]}
    assert fields["cortex_base_url"]["value"] == "http://localhost:8501"  # default
    assert fields["anthropic_api_key"]["is_set"] is False
    assert result["store_connected"] is False


# ===========================================================================
#  2. providers — the live model catalog grouped by provider; graceful-degrade.
# ===========================================================================


def test_group_catalog_models_shape():
    """The pure `group_catalog_models(models)` groups `CatalogModel`s by provider
    into `{providers:[{name, models:[{model,type,reasoning_tiers,
    input_price_per_mtok,output_price_per_mtok,context_window,source,freshness}]}]}`."""
    from app.settings_module import service as svc

    out = svc.group_catalog_models(SAMPLE_MODELS)
    assert set(out) == {"providers"}
    provs = {p["name"]: p for p in out["providers"]}
    assert set(provs) == {"anthropic", "openai"}
    assert len(provs["anthropic"]["models"]) == 2

    opus = next(m for m in provs["anthropic"]["models"] if m["model"] == "claude-opus-4-8")
    assert opus["type"] == "chat"
    assert opus["reasoning_tiers"] == ["low", "medium", "high"]
    assert opus["input_price_per_mtok"] == 5.0
    assert opus["output_price_per_mtok"] == 25.0
    assert opus["context_window"] == 200000
    assert opus["source"] == "merged"
    # freshness is a derived human label of the provenance (source), present + non-empty
    assert isinstance(opus["freshness"], str) and opus["freshness"]


def test_group_catalog_models_empty():
    """An empty catalog → `{providers: []}` (the graceful-degrade target shape), not
    an error."""
    from app.settings_module import service as svc

    assert svc.group_catalog_models([]) == {"providers": []}


@pytest.mark.asyncio
async def test_router_providers_endpoint_shape():
    """`GET /settings/{project}/providers` returns the grouped live catalog from the
    `ModelCatalogPort` (fake) in the documented shape."""
    from app.settings_module import api as settings_api

    catalog = FakeCatalogPort(models=SAMPLE_MODELS)
    result = await settings_api.providers_endpoint("kaidera-os", catalog=catalog)

    assert result["project"] == "kaidera-os"
    provs = {p["name"]: p for p in result["providers"]}
    assert set(provs) == {"anthropic", "openai"}
    assert "list_models" in catalog.calls


@pytest.mark.asyncio
async def test_router_providers_endpoint_degrades_on_fetch_error():
    """A provider fetch error degrades to an EMPTY/partial catalog (never a 500) —
    `providers_endpoint` swallows the failure and returns `{providers: []}`."""
    from app.settings_module import api as settings_api

    catalog = FakeCatalogPort(raise_on_list=True)
    result = await settings_api.providers_endpoint("kaidera-os", catalog=catalog)

    assert result["project"] == "kaidera-os"
    assert result["providers"] == []  # graceful-degrade, not an exception


# ===========================================================================
#  3. custom-providers — add / delete JSON mirrors (delegate to the store).
# ===========================================================================


@pytest.mark.asyncio
async def test_router_custom_provider_add_delegates_to_store():
    """`POST /settings/{project}/custom-providers` adds (name+base_url+api_key) via
    the SAME store the HTML route uses, and returns the refreshed MASKED list (the
    raw api_key never echoed)."""
    from app.settings_module import api as settings_api

    store = FakeCustomStore()
    result = await settings_api.custom_provider_add_endpoint(
        "kaidera-os",
        {"name": "MyProv", "base_url": "https://api.myprov.ai/v1", "api_key": "sk-xyz"},
        store=store,
    )
    assert result["ok"] is True
    assert result["added"] == "MyProv"
    assert any(c[0] == "add" for c in store.calls)
    # the refreshed list is masked (has_key true, no raw key)
    row = next(r for r in result["custom_providers"] if r["name"] == "MyProv")
    assert row["has_key"] is True
    import json
    assert "sk-xyz" not in json.dumps(result)


@pytest.mark.asyncio
async def test_router_custom_provider_add_blank_name_is_error():
    """A blank name is a graceful error (ok=false + a message), not a 500."""
    from app.settings_module import api as settings_api

    store = FakeCustomStore()
    result = await settings_api.custom_provider_add_endpoint(
        "kaidera-os", {"name": "  ", "base_url": "x", "api_key": "y"}, store=store,
    )
    assert result["ok"] is False
    assert result["error"]


@pytest.mark.asyncio
async def test_router_custom_provider_delete_delegates_to_store():
    """`POST /settings/{project}/custom-providers/delete` removes by id/name via the
    store and returns the refreshed list with `removed` reflecting the outcome."""
    from app.settings_module import api as settings_api

    store = FakeCustomStore(existing=[{"id": "myprov", "name": "MyProv",
                                       "base_url": "u", "api_key": "k"}])
    result = await settings_api.custom_provider_delete_endpoint(
        "kaidera-os", {"id": "myprov"}, store=store,
    )
    assert result["ok"] is True
    assert result["removed"] is True
    assert any(c[0] == "remove" for c in store.calls)
    assert result["custom_providers"] == []  # the row is gone

    # deleting an unknown id → removed False (no row matched), still ok (no crash)
    result2 = await settings_api.custom_provider_delete_endpoint(
        "kaidera-os", {"id": "nope"}, store=store,
    )
    assert result2["removed"] is False


# ===========================================================================
#  4. provider-key-test — JSON mirror of the HTML test-key probe.
# ===========================================================================


@pytest.mark.asyncio
async def test_router_provider_key_test_ok():
    """`POST /settings/{project}/provider-key-test` probes a provider key via the
    reused legacy probe (fake) and returns `{ok, detail}`. The probe gets the
    field + the typed key; the response carries a human detail, never the key."""
    from app.settings_module import api as settings_api

    tester = FakeKeyTester(result={"ok": True, "status": "ok",
                                   "message": "Anthropic key works.", "label": "Anthropic"})
    result = await settings_api.provider_key_test_endpoint(
        "kaidera-os", {"provider": "anthropic_api_key", "key": "sk-ant-typed"},
        key_test=tester,
    )
    assert result["ok"] is True
    assert "works" in result["detail"]
    # the probe was called with the field + the typed key
    assert tester.calls and tester.calls[0][0] == "anthropic_api_key"
    assert tester.calls[0][1] == "sk-ant-typed"


@pytest.mark.asyncio
async def test_router_provider_key_test_fail():
    """A rejected key returns `ok=false` + the human detail (graceful, not a 500)."""
    from app.settings_module import api as settings_api

    tester = FakeKeyTester(result={"ok": False, "status": "rejected",
                                   "message": "Anthropic rejected the key (HTTP 401).",
                                   "label": "Anthropic"})
    result = await settings_api.provider_key_test_endpoint(
        "kaidera-os", {"provider": "anthropic_api_key"}, key_test=tester,
    )
    assert result["ok"] is False
    assert "rejected" in result["detail"].lower()
    # provider + use-stored (no typed key) → value forwarded as None (uses stored)
    assert tester.calls[0][1] is None


# ===========================================================================
#  5. workspace — set a project's repo_root via the admin path (token-safe).
# ===========================================================================


@pytest.mark.asyncio
async def test_router_workspace_set_repo_root_ok():
    """`POST /settings/{project}/workspace` sets a project's `repo_root` via the
    existing admin path (fake `set_project_repo_root`) and returns the updated value
    + the previous one. The admin token is NEVER in the response."""
    from app.settings_module import api as settings_api

    client = FakeRepoRootClient(result={
        "project_key": "kaidera-os", "repo_root": "/abs/new",
        "previous_repo_root": "/abs/old",
    })
    result = await settings_api.workspace_endpoint(
        "kaidera-os", {"repo_root": "/abs/new"}, repo_client=client,
    )
    assert result["ok"] is True
    assert result["repo_root"] == "/abs/new"
    assert result["previous_repo_root"] == "/abs/old"
    assert client.calls == [("kaidera-os", "/abs/new")]
    # token-safety: nothing token-shaped in the response
    import json
    blob = json.dumps(result).lower()
    assert "token" not in blob
    assert "admin" not in blob


@pytest.mark.asyncio
async def test_router_workspace_uses_path_project_when_body_omits_target():
    """The project whose folder is set defaults to the path `{project}` when the body
    doesn't name a different `project_key` (the SPA edits the selected project)."""
    from app.settings_module import api as settings_api

    client = FakeRepoRootClient(result={"repo_root": "/abs/x", "previous_repo_root": None})
    await settings_api.workspace_endpoint(
        "kaidera-os", {"repo_root": "/abs/x"}, repo_client=client,
    )
    assert client.calls[0][0] == "kaidera-os"


@pytest.mark.asyncio
async def test_router_workspace_set_repo_root_bad_path_is_clear_error():
    """A blank/relative path → a clear error (ok=false + message), not a 500. Reuses
    the admin method's own ValueError (it rejects a non-absolute path)."""
    from app.settings_module import api as settings_api

    client = FakeRepoRootClient(error=ValueError("repo_root must be an absolute path"))
    result = await settings_api.workspace_endpoint(
        "kaidera-os", {"repo_root": "relative/path"}, repo_client=client,
    )
    assert result["ok"] is False
    assert "absolute" in result["error"]


@pytest.mark.asyncio
async def test_router_workspace_admin_token_missing_is_graceful():
    """No admin token configured → a graceful 'not configured' error (ok=false), and
    NOTHING is leaked — the token never reaches the response (it was never sent)."""
    from app.cortex_client import AdminTokenMissing
    from app.settings_module import api as settings_api

    client = FakeRepoRootClient(error=AdminTokenMissing("CORTEX_ADMIN_TOKEN is not configured"))
    result = await settings_api.workspace_endpoint(
        "kaidera-os", {"repo_root": "/abs/new"}, repo_client=client,
    )
    assert result["ok"] is False
    assert "token" in result["error"].lower()  # explains WHY, but no token VALUE
    # the configured-token value is never present (there is none); the message is advisory
    assert "CORTEX_ADMIN_TOKEN" in result["error"] or "admin token" in result["error"].lower()


# ===========================================================================
#  Routing — the new endpoints are collision-free + don't shadow the legacy HTML.
# ===========================================================================


def test_new_json_routes_registered_and_collision_free():
    """The five new JSON routes live under the module's `/settings/{project}/...`
    JSON shape (so they can't shadow the one-segment HTML `GET /settings/{page}`),
    and NONE collides with a live HTML `POST /settings/...` route (which all carry a
    LITERAL first segment under /settings/ — `system`, `system/...`, `projects/...`,
    `configure`)."""
    from app.settings_module.api import router

    paths = {r.path for r in router.routes}
    # the five new leaves
    assert "/settings/{project}/system-schema" in paths
    assert "/settings/{project}/providers" in paths
    assert "/settings/{project}/custom-providers" in paths
    assert "/settings/{project}/custom-providers/delete" in paths
    assert "/settings/{project}/provider-key-test" in paths
    assert "/settings/{project}/workspace" in paths

    # NONE of the live HTML POST /settings/... routes is claimed (literal-first).
    live_html_settings_posts = {
        "/settings/projects/{project_key}/folder",
        "/settings/system",
        "/settings/system/test-key",
        "/settings/system/custom-provider",
        "/settings/system/custom-provider/delete",
        "/settings/configure",
    }
    assert not (paths & live_html_settings_posts)
    # and the module still owns NO one-segment HTML tab path, nor any /agents/ path.
    assert "/settings/{page}" not in paths
    assert not any(p.startswith("/agents/") for p in paths)


def test_new_routes_methods():
    """system-schema + providers are GETs; custom-providers (+/delete), key-test, and
    workspace are POSTs (the write/probe mirrors)."""
    from app.settings_module.api import router

    def methods_for(path):
        for r in router.routes:
            if r.path == path:
                return getattr(r, "methods", set())
        return set()

    assert "GET" in methods_for("/settings/{project}/system-schema")
    assert "GET" in methods_for("/settings/{project}/providers")
    assert "POST" in methods_for("/settings/{project}/custom-providers")
    assert "POST" in methods_for("/settings/{project}/custom-providers/delete")
    assert "POST" in methods_for("/settings/{project}/provider-key-test")
    assert "POST" in methods_for("/settings/{project}/workspace")


def test_service_still_imports_nothing_outward_after_additions():
    """GUARD (re-pinned): even with the new system-schema + catalog-grouping pure
    helpers, `service.py` imports NOTHING outward (no fastapi / httpx / subprocess /
    psycopg2 / asyncpg) and does NOT reach for app.main / the concrete appdb /
    adapters / the legacy app.settings facade — only the domain port + stdlib.

    The new I/O (catalog fetch, key-test probe, custom-provider store, repo_root
    admin PATCH) lives in `api.py` (the shell), injected into the pure helpers, so
    the module-isolation contract holds."""
    import ast
    from pathlib import Path

    src = (
        Path(__file__).resolve().parents[1]
        / "app" / "settings_module" / "service.py"
    ).read_text()
    tree = ast.parse(src)
    top: set[str] = set()
    dotted: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                top.add(a.name.split(".")[0])
                dotted.add(a.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                top.add(node.module.split(".")[0])
                dotted.add(node.module)

    forbidden = {"fastapi", "starlette", "httpx", "subprocess", "psycopg2", "asyncpg"}
    assert not (top & forbidden), f"service.py imports outward: {sorted(top & forbidden)}"
    assert "app.main" not in dotted
    assert "app.settings" not in dotted, "service.py must not import the legacy facade"
    assert not any(m == "app.appdb" or m.startswith("app.adapters") for m in dotted)
    # the feature-module independence rule: never import a sibling feature module
    assert not any(
        m in {"app.analytics", "app.agents", "app.dispatch", "app.runs"} for m in dotted
    ), "service.py must not import a sibling feature module"
