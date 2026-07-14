"""Feature-gap #81 — the in-console REGISTRATION console routes (the SPA backend).

Three additive console routes wrap the `CortexClient` registration writes so the
SPA's registration UX (add agent, deregister agent, add project) has a JSON backend.
Each is GRACEFUL-DEGRADE + token-safe, returning a friendly `{ok, error, ...}` echo
(never a 500, never the admin token):

  * POST /agents/{project}/register        → CortexClient.create_agent
  * POST /agents/{project}/{agent}/deregister → CortexClient.remove_agent
  * POST /projects/register                → CortexClient.create_project

Driven by calling the router handlers directly with a FAKE cortex client (the
settings_module idiom — the concrete is resolved at a Depends seam in production),
so no live Cortex. The friendly-error mapping (caller-not-a-writer / admin-not-
configured) is asserted, and that a degraded write (None/False) is a soft `ok=false`.
"""

from __future__ import annotations

import json

import pytest

from app import registration_api as reg


class FakeCortex:
    """Records the registration writes + serves scripted results. `*_result`
    controls each call's simulated outcome (None/False = a degraded/failed write)."""

    def __init__(self, *, create_agent_result=None, remove_result=False,
                 create_project_result=None, roster=None):
        self.create_agent_result = create_agent_result
        self.remove_result = remove_result
        self.create_project_result = create_project_result
        self.roster = list(roster or [])
        self.calls: list[tuple] = []
        self.agent_calls: list[dict] = []  # full kwargs incl role_description (the persona brief)
        self.knowledge_calls: list[dict] = []

    async def create_agent(self, project_key, *, name, role, capabilities=None,
                           writer_scope=None, role_description=None):
        self.calls.append(("create_agent", project_key, name, role, capabilities,
                           writer_scope))
        self.agent_calls.append({"project_key": project_key, "name": name, "role": role,
                                 "capabilities": capabilities, "writer_scope": writer_scope,
                                 "role_description": role_description})
        return self.create_agent_result

    async def remove_agent(self, project_key, name):
        self.calls.append(("remove_agent", project_key, name))
        return self.remove_result

    async def create_project(self, *, project_key, display_name=None, repo_root=None,
                             repo_type=None, default_agent=None, agents=None):
        self.calls.append(("create_project", project_key, display_name, repo_root, default_agent, agents))
        return self.create_project_result

    async def get_roster(self, project_key):
        self.calls.append(("get_roster", project_key))
        return list(self.roster)

    async def ingest_knowledge(self, project_key, *, content, source_file, category=None,
                               section=None, on_conflict="update"):
        self.knowledge_calls.append({
            "project_key": project_key,
            "content": content,
            "source_file": source_file,
            "category": category,
            "section": section,
            "on_conflict": on_conflict,
        })
        return {"id": "k1", "status": "created"}


def write_basic_pack(root, *, key="basic-project-pack", extension_env="KAIDERA_OS_EXTENSION_MODULES"):
    pack_dir = root / ".kaidera-os" / "project-packs" / key
    seed_dir = pack_dir / "cortex-seed"
    seed_dir.mkdir(parents=True)
    portal_dir = pack_dir / "portal"
    portal_dir.mkdir(parents=True)
    (seed_dir / "README.md").write_text("# Seed\n\nPack-local knowledge.\n", encoding="utf-8")
    (portal_dir / "index.html").write_text("<!doctype html><title>Portal</title>\n", encoding="utf-8")
    (pack_dir / "project-pack.json").write_text(
        json.dumps({
            "schema_version": "1.0",
            "kind": "kaidera-os.project-pack",
            "pack": {
                "key": key,
                "name": "Basic Project Pack",
                "version": "0.1.0",
                "description": "Generic starter pack.",
            },
            "project": {"default_key": "customer-project"},
            "extensions": [{"module": "basic_project_pack.example_worker", "required": False}],
            "portals": [
                {
                    "key": "operator-chat",
                    "type": "thin-web",
                    "agent": "lead",
                    "route_prefix": "/portal/operator-chat",
                    "auth": "kaidera-os-auth",
                    "stream_contract": "runstate-sse",
                    "frontend_path": "portal/index.html",
                    "required": True,
                }
            ],
            "assets": [
                {"path": "cortex-seed/README.md", "type": "cortex_seed", "required": True},
                {"path": "portal/index.html", "type": "frontend", "required": True},
            ],
            "install": {
                "cortex_seed_glob": "cortex-seed/*.md",
                "enable_extensions_env": extension_env,
            },
        }),
        encoding="utf-8",
    )
    (pack_dir / "extensions.env").write_text(
        f"{extension_env}=basic_project_pack.example_worker\n"
        f"KAIDERA_OS_EXTENSION_PATHS={pack_dir}\n",
        encoding="utf-8",
    )
    return pack_dir


def pack_by_key(result, key="basic-project-pack"):
    return next(p for p in result["packs"] if p["key"] == key)


# ===========================================================================
#  POST /agents/{project}/register → create_agent
# ===========================================================================


@pytest.mark.asyncio
async def test_register_agent_ok():
    """A successful create_agent → `{ok:true, agent, role}` + the right write call
    (name/role/capabilities built from the harness/model/reasoning/writer_scope)."""
    cortex = FakeCortex(create_agent_result={"registered": True, "agent": "newbie",
                                             "role": "qa"})
    result = await reg.register_agent_route(
        "kaidera-os",
        {"name": "newbie", "role": "qa", "harness": "claude-code", "model": "opus",
         "reasoning": "high", "designation": "autonomous", "writer_scope": "work"},
        cortex=cortex,
    )
    assert result["ok"] is True
    assert result["agent"] == "newbie"
    assert result["error"] is None
    call = next(c for c in cortex.calls if c[0] == "create_agent")
    _, proj, name, role, caps, ws = call
    assert (proj, name, role) == ("kaidera-os", "newbie", "qa")
    # the config fields are folded into capabilities
    assert caps["harness"] == "claude-code"
    assert caps["model"] == "opus"
    assert caps["reasoning"] == "high"
    assert caps["designation"] == "autonomous"
    assert ws == "work"


@pytest.mark.asyncio
async def test_register_agent_requires_name_and_role():
    """A blank name or role is a friendly `ok=false` + a clear error WITHOUT a call
    (validated before touching Cortex)."""
    cortex = FakeCortex(create_agent_result={"registered": True})
    r1 = await reg.register_agent_route("kaidera-os", {"name": "", "role": "qa"}, cortex=cortex)
    r2 = await reg.register_agent_route("kaidera-os", {"name": "xy", "role": ""}, cortex=cortex)
    assert r1["ok"] is False and "name" in (r1["error"] or "").lower()
    assert r2["ok"] is False and "role" in (r2["error"] or "").lower()
    assert cortex.calls == []


@pytest.mark.asyncio
async def test_register_agent_rejects_transient_and_mismatched_display_names():
    cortex = FakeCortex(create_agent_result={"registered": True})
    r1 = await reg.register_agent_route(
        "kaidera-os",
        {"name": "claude-subagent-deadbeef", "role": "qa"},
        cortex=cortex,
    )
    r2 = await reg.register_agent_route(
        "kaidera-os",
        {"name": "hue@dxb", "role": "qa"},
        cortex=cortex,
    )
    assert r1["ok"] is False and "transient" in (r1["error"] or "").lower()
    assert r2["ok"] is False and "dxb" in (r2["error"] or "").lower()
    assert cortex.calls == []


@pytest.mark.asyncio
async def test_register_agent_strips_matching_display_project_suffix():
    cortex = FakeCortex(create_agent_result={"registered": True, "agent": "hue", "role": "qa"})
    result = await reg.register_agent_route(
        "kaidera-os",
        {"name": "Hue@Kaidera OS", "role": "qa"},
        cortex=cortex,
    )
    assert result["ok"] is True
    call = next(c for c in cortex.calls if c[0] == "create_agent")
    assert call[2] == "hue"


@pytest.mark.asyncio
async def test_register_agent_degraded_write_is_friendly_error():
    """A degraded create_agent (None — e.g. the caller isn't a registered writer, or
    Cortex is down) is a soft `ok=false` with a friendly, NON-leaky error."""
    cortex = FakeCortex(create_agent_result=None)
    result = await reg.register_agent_route(
        "kaidera-os", {"name": "x", "role": "qa"}, cortex=cortex
    )
    assert result["ok"] is False
    assert result["error"]  # a human message
    assert "token" not in (result["error"] or "").lower()  # never leak token wording for a writer-gated route


@pytest.mark.asyncio
async def test_register_agent_rejects_second_orchestrator():
    """Add Worker uses the same one-orchestrator product rule as Config save."""
    cortex = FakeCortex(
        create_agent_result={"registered": True},
        roster=[{"name": "beat", "role": "orchestrator"}],
    )

    result = await reg.register_agent_route(
        "kaidera-os",
        {"name": "orchestrator", "role": "orchestrator", "designation": "deterministic"},
        cortex=cortex,
    )

    assert result["ok"] is False
    assert "Only one deterministic orchestrator" in (result["error"] or "")
    assert not any(call[0] == "create_agent" for call in cortex.calls)


@pytest.mark.asyncio
async def test_register_agent_allows_upserting_existing_orchestrator():
    """Re-registering the already-designated orchestrator is an update, not a duplicate."""
    cortex = FakeCortex(
        create_agent_result={"registered": True, "agent": "orchestrator", "role": "orchestrator"},
        roster=[{"name": "orchestrator", "role": "orchestrator"}],
    )

    result = await reg.register_agent_route(
        "kaidera-os",
        {"name": "orchestrator", "role": "orchestrator", "designation": "deterministic"},
        cortex=cortex,
    )

    assert result["ok"] is True
    assert ("create_agent", "kaidera-os", "orchestrator", "orchestrator", {"designation": "deterministic"}, None) in cortex.calls


@pytest.mark.asyncio
async def test_register_agent_none_cortex_is_friendly():
    """A None cortex (degraded console) is a friendly `ok=false`, no crash."""
    result = await reg.register_agent_route(
        "kaidera-os", {"name": "x", "role": "qa"}, cortex=None
    )
    assert result["ok"] is False
    assert result["error"]


# ===========================================================================
#  POST /agents/{project}/{agent}/deregister → remove_agent
# ===========================================================================


@pytest.mark.asyncio
async def test_deregister_agent_ok():
    """A successful remove → `{ok:true, removed:true}` + the right call."""
    cortex = FakeCortex(remove_result=True)
    result = await reg.deregister_agent_route("kaidera-os", "gone", cortex=cortex)
    assert result["ok"] is True
    assert result["removed"] is True
    assert ("remove_agent", "kaidera-os", "gone") in cortex.calls


@pytest.mark.asyncio
async def test_deregister_agent_failure_mentions_admin_token():
    """A failed remove (False — admin token missing / Cortex rejected) is a friendly
    `ok=false` whose error nudges the admin-token requirement (the remove route is
    admin-gated, like the workspace editor)."""
    cortex = FakeCortex(remove_result=False)
    result = await reg.deregister_agent_route("kaidera-os", "gone", cortex=cortex)
    assert result["ok"] is False
    assert "admin" in (result["error"] or "").lower()


@pytest.mark.asyncio
async def test_deregister_agent_blank_name_is_error():
    """A blank agent name is a friendly error WITHOUT a call."""
    cortex = FakeCortex(remove_result=True)
    result = await reg.deregister_agent_route("kaidera-os", "  ", cortex=cortex)
    assert result["ok"] is False
    assert cortex.calls == []


# ===========================================================================
#  POST /projects/register → create_project
# ===========================================================================


@pytest.mark.asyncio
async def test_register_project_ok():
    """A successful create_project → `{ok:true, project_key}` + the right call
    (project_key/display_name/repo_root forwarded)."""
    cortex = FakeCortex(create_project_result={"project_key": "demo", "registered": True})
    result = await reg.register_project_route(
        {"project_key": "demo", "display_name": "Demo", "repo_root": "/abs/demo"},
        cortex=cortex,
    )
    assert result["ok"] is True
    assert result["project_key"] == "demo"
    call = next(c for c in cortex.calls if c[0] == "create_project")
    _, key, dn, root, default_agent, agents = call
    assert key == "demo"
    assert dn == "Demo"
    assert root == "/abs/demo"
    assert default_agent == "lead"
    assert agents and agents[0]["name"] == "lead"


@pytest.mark.asyncio
async def test_list_project_packs_discovers_installed_pack(tmp_path, monkeypatch):
    """GET /project-packs reads only the selected repo_root's installed packs."""
    monkeypatch.setenv("KAIDERA_OS_EXTENSION_MODULES", "")
    pack_dir = write_basic_pack(tmp_path)
    result = await reg.list_project_packs_route(repo_root=str(tmp_path))
    assert result["ok"] is True
    assert result["error"] is None
    assert len(result["packs"]) == 1
    pack = result["packs"][0]
    assert pack["key"] == "basic-project-pack"
    assert pack["name"] == "Basic Project Pack"
    assert pack["default_project_key"] == "customer-project"
    assert pack["seed_files"] == ["cortex-seed/README.md"]
    assert pack["seed_count"] == 1
    assert pack["extension_modules"] == ["basic_project_pack.example_worker"]
    assert pack["extensions"][0]["module"] == "basic_project_pack.example_worker"
    assert pack["extension_paths_env"] == "KAIDERA_OS_EXTENSION_PATHS"
    assert pack["extension_path"] == str(pack_dir.resolve())
    assert pack["portals"][0]["key"] == "operator-chat"
    assert pack["portals"][0]["route_prefix"] == "/portal/operator-chat"
    assert pack["portals"][0]["frontend_exists"] is True
    assert pack["portals"][0]["status"] == "ready"
    runtime = pack["portals"][0]["runtime_contract"]
    assert runtime["contract"] == "runstate-sse"
    assert runtime["chat_endpoint_template"] == "/agents/{project}/lead/chat"
    assert runtime["stream_endpoint_template"] == "/runstate/stream?project={project}&run={run_id}"
    assert runtime["run_endpoint_template"] == "/runs/run/{run_id}"
    assert runtime["stream_events"] == ["runstate"]
    assert pack["extensions"][0]["status"] == "enabled_restart_required"
    assert pack["restart_required"] is True


@pytest.mark.asyncio
async def test_list_project_packs_rejects_relative_root():
    result = await reg.list_project_packs_route(repo_root="relative/project")
    assert result["ok"] is False
    assert result["packs"] == []
    assert "absolute" in (result["error"] or "").lower()


@pytest.mark.asyncio
async def test_project_pack_extension_toggle_updates_pack_env(tmp_path, monkeypatch):
    """A pack-declared extension can be disabled/enabled via the pack-local helper."""
    monkeypatch.setenv("KAIDERA_OS_EXTENSION_MODULES", "")
    write_basic_pack(tmp_path)

    disabled = await reg.set_project_pack_extension_route(
        {
            "repo_root": str(tmp_path),
            "pack_key": "basic-project-pack",
            "module": "basic_project_pack.example_worker",
            "enabled": False,
        }
    )
    assert disabled["ok"] is True
    row = disabled["pack"]["extensions"][0]
    assert row["enabled"] is False
    assert row["loaded"] is False
    assert row["status"] == "disabled"
    assert "KAIDERA_OS_EXTENSION_MODULES=" in (
        tmp_path / ".kaidera-os" / "project-packs" / "basic-project-pack" / "extensions.env"
    ).read_text(encoding="utf-8")

    enabled = await reg.set_project_pack_extension_route(
        {
            "repo_root": str(tmp_path),
            "pack_key": "basic-project-pack",
            "module": "basic_project_pack.example_worker",
            "enabled": True,
        }
    )
    assert enabled["ok"] is True
    row = enabled["pack"]["extensions"][0]
    assert row["enabled"] is True
    assert row["loaded"] is False
    assert row["status"] == "enabled_restart_required"
    assert row["restart_required"] is True


@pytest.mark.asyncio
async def test_project_pack_extension_status_detects_loaded_but_disabled(tmp_path, monkeypatch):
    """Loaded modules not present in the helper are called out as restart-required drift."""
    monkeypatch.setenv("KAIDERA_OS_EXTENSION_MODULES", "basic_project_pack.example_worker")
    write_basic_pack(tmp_path)
    await reg.set_project_pack_extension_route(
        {
            "repo_root": str(tmp_path),
            "pack_key": "basic-project-pack",
            "module": "basic_project_pack.example_worker",
            "enabled": False,
        }
    )
    result = await reg.list_project_packs_route(repo_root=str(tmp_path))
    row = pack_by_key(result)["extensions"][0]
    assert row["enabled"] is False
    assert row["loaded"] is True
    assert row["status"] == "loaded_disable_restart_required"


@pytest.mark.asyncio
async def test_project_pack_extension_status_uses_manifest_env_name(tmp_path, monkeypatch):
    """Loaded status honors the pack manifest's extension env name."""
    monkeypatch.setenv("KAIDERA_OS_EXTENSION_MODULES", "")
    monkeypatch.setenv("CUSTOM_PACK_EXTENSIONS", "basic_project_pack.example_worker")
    write_basic_pack(tmp_path, extension_env="CUSTOM_PACK_EXTENSIONS")
    result = await reg.list_project_packs_route(repo_root=str(tmp_path))
    pack = pack_by_key(result)
    row = pack["extensions"][0]
    assert pack["extension_env"] == "CUSTOM_PACK_EXTENSIONS"
    assert row["enabled"] is True
    assert row["loaded"] is True
    assert row["status"] == "loaded"


@pytest.mark.asyncio
async def test_project_pack_extension_rejects_module_not_declared_by_pack(tmp_path):
    write_basic_pack(tmp_path)
    result = await reg.set_project_pack_extension_route(
        {
            "repo_root": str(tmp_path),
            "pack_key": "basic-project-pack",
            "module": "app.not_in_manifest",
            "enabled": True,
        }
    )
    assert result["ok"] is False
    assert "not declared" in (result["error"] or "").lower()


@pytest.mark.asyncio
async def test_register_project_with_pack_ingests_cortex_seed(tmp_path):
    """Selecting an installed pack imports its seed docs into the newly registered project."""
    write_basic_pack(tmp_path)
    cortex = FakeCortex(create_project_result={"project_key": "demo", "registered": True})
    result = await reg.register_project_route(
        {
            "project_key": "demo",
            "display_name": "Demo",
            "repo_root": str(tmp_path),
            "project_pack_key": "basic-project-pack",
        },
        cortex=cortex,
    )
    assert result["ok"] is True
    assert result["project_pack"]["key"] == "basic-project-pack"
    assert result["project_pack"]["ingested"] == 1
    assert result["project_pack"]["errors"] == []
    assert len(cortex.knowledge_calls) == 1
    call = cortex.knowledge_calls[0]
    assert call["project_key"] == "demo"
    assert call["source_file"] == ".kaidera-os/project-packs/basic-project-pack/cortex-seed/README.md"
    assert call["category"] == "project-pack"
    assert call["on_conflict"] == "update"
    assert "Pack-local knowledge" in call["content"]


@pytest.mark.asyncio
async def test_register_project_with_missing_pack_is_friendly_error(tmp_path):
    cortex = FakeCortex(create_project_result={"project_key": "demo", "registered": True})
    result = await reg.register_project_route(
        {"project_key": "demo", "repo_root": str(tmp_path), "project_pack_key": "missing-pack"},
        cortex=cortex,
    )
    assert result["ok"] is False
    assert "not installed" in (result["error"] or "").lower()
    assert cortex.calls == []


@pytest.mark.asyncio
async def test_register_project_requires_key_and_abs_repo_root():
    """A blank project_key OR a non-absolute repo_root is a friendly error WITHOUT a
    call (the repo_root must be absolute — same rule as the workspace editor)."""
    cortex = FakeCortex(create_project_result={"registered": True})
    r1 = await reg.register_project_route({"project_key": "", "repo_root": "/abs/x"}, cortex=cortex)
    r2 = await reg.register_project_route({"project_key": "demo", "repo_root": "relative/x"}, cortex=cortex)
    assert r1["ok"] is False and "project" in (r1["error"] or "").lower()
    assert r2["ok"] is False and "absolute" in (r2["error"] or "").lower()
    assert cortex.calls == []


@pytest.mark.asyncio
async def test_register_project_rejects_bad_key_and_ephemeral_lead():
    cortex = FakeCortex(create_project_result={"registered": True})
    r1 = await reg.register_project_route({"project_key": "--help", "repo_root": "/abs/x"}, cortex=cortex)
    r2 = await reg.register_project_route(
        {"project_key": "demo", "repo_root": "/abs/x", "lead_name": "claude-subagent-deadbeef"},
        cortex=cortex,
    )
    assert r1["ok"] is False and "project key" in (r1["error"] or "").lower()
    assert r2["ok"] is False and "transient" in (r2["error"] or "").lower()
    assert cortex.calls == []


@pytest.mark.asyncio
async def test_register_project_degraded_write_mentions_admin_token():
    """A degraded create_project (None — admin token missing / Cortex down) is a
    friendly `ok=false` nudging the admin-token requirement (it's admin-gated)."""
    cortex = FakeCortex(create_project_result=None)
    result = await reg.register_project_route(
        {"project_key": "demo", "repo_root": "/abs/demo"}, cortex=cortex
    )
    assert result["ok"] is False
    assert "admin" in (result["error"] or "").lower()


# ===========================================================================
#  Router wiring
# ===========================================================================


def test_router_exposes_registration_routes():
    """The module's APIRouter exposes registration writes and pack discovery (so main can
    include_router them additively)."""
    posts = {
        r.path
        for r in reg.router.routes
        if "POST" in getattr(r, "methods", set())
    }
    assert "/agents/{project}/register" in posts
    assert "/agents/{project}/{agent}/deregister" in posts
    assert "/projects/register" in posts
    assert "/project-packs/extensions" in posts
    gets = {
        r.path
        for r in reg.router.routes
        if "GET" in getattr(r, "methods", set())
    }
    assert "/project-packs" in gets


# ===========================================================================
#  POST /projects/register → creates an initial lead in the project payload
# ===========================================================================


@pytest.mark.asyncio
async def test_register_project_seeds_named_lead_with_scope_persona():
    """The project register seeds the first lead worker NAMED by the operator (`lead_name`) and
    gives it a persona brief built from the project SCOPE (`description`) — so the lead's role
    comes from the scope on day one. kaidera so it runs with zero extra config."""
    cortex = FakeCortex(
        create_project_result={"project_key": "acme"},
        create_agent_result={"registered": True},
    )
    result = await reg.register_project_route(
        {
            "project_key": "acme",
            "display_name": "Acme",
            "repo_root": "/srv/acme",
            "description": "Marketing automation — plan + publish content.",
            "lead_name": "Nova",
        },
        cortex=cortex,
    )
    assert result["ok"] is True
    assert result["lead_seeded"] is True
    assert result["lead_name"] == "nova"
    create_project = next(c for c in cortex.calls if c[0] == "create_project")
    seed = create_project[5][0]
    assert seed["name"] == "nova"
    assert "Marketing automation" in seed["capabilities"]["persona_brief"]
    assert seed["capabilities"]["harness"] == "kaidera"


@pytest.mark.asyncio
async def test_register_project_lead_defaults_to_neutral_lead_without_a_name():
    """No `lead_name` → the neutral role-based lead is created; no scope → no
    persona brief."""
    cortex = FakeCortex(
        create_project_result={"project_key": "acme"},
        create_agent_result={"registered": True},
    )
    result = await reg.register_project_route({"project_key": "acme"}, cortex=cortex)
    assert result["lead_name"] == "lead"
    create_project = next(c for c in cortex.calls if c[0] == "create_project")
    seed = create_project[5][0]
    assert seed["name"] == "lead"
    assert "persona_brief" not in seed["capabilities"]
