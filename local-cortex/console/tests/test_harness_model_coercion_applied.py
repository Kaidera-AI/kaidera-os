"""Feature #99 — the harness/model coercion is APPLIED at every resolution surface,
so a run NEVER spawns with an impossible pair AND the UI never displays one.

`harness.coerce_model` (covered pure in `test_harness_model_validity.py`) is the
corrector; this file proves it's WIRED into the three places harness+model resolve
for an agent:

  1. `main._chat_routing_for` — the runtime routing for a chat/dispatch turn. An
     agent STORED with `claude-code` + a Gemini model must RUN claude-code + the
     claude-code default (not the impossible Gemini model).
  2. `main._agents_resolve_config` (feeding `_agent_view` / the agents column) and
     `agents.api._harness_resolve_config` (the JSON catalog) — the card's displayed
     model is the COERCED one (never the impossible pair).
  3. `harness.agent_config_view` — the Configure card's effective model is coerced,
     and it surfaces a subtle `model_coerced` hint (was-invalid → using <default>) so
     the operator can see why the displayed model differs from the stored one.

The catalog lanes (kaidera) + unknown harnesses pass through unchanged (no fixed
list to coerce against) — asserted so we never wipe a valid catalog pick.

Written BEFORE the implementation (STRICT TDD).
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

from app import harness as h
from app.agents import api as agents_api


# An agent stored with an IMPOSSIBLE pair: claude-code harness, a Gemini model.
def _impossible_agent() -> dict:
    return {
        "name": "victim",
        "role": "full-stack-developer",
        "capabilities": {
            "harness": "claude-code",
            "model_preference": "gemini-3.1-pro-preview",
            "thinking": "high",
        },
    }


# ---------------------------------------------------------------------------
#  1. main._chat_routing_for — the RUNTIME routing coerces.
# ---------------------------------------------------------------------------


def test_chat_routing_coerces_an_impossible_registry_pair(monkeypatch):
    """An agent stored claude-code + a Gemini model RESOLVES to claude-code + the
    claude-code DEFAULT model — the run never spawns the impossible pair."""
    import app.main as main_mod

    # no console override — the impossible pair comes from the registry capabilities.
    monkeypatch.setattr(main_mod.settings_store, "get_agent_override", lambda p, a: {})

    harness, model, _reasoning = main_mod._chat_routing_for(_impossible_agent(), "kaidera-os")

    assert harness == "claude-code"
    # the Gemini model is GONE — coerced to the claude-code default.
    assert model != "gemini-3.1-pro-preview"
    assert model == h.HARNESS_MODELS["claude-code"][0]["value"]
    assert h.valid_model_for_harness(harness, model) is True


def test_chat_routing_coerces_an_impossible_override_pair(monkeypatch):
    """A stale CONSOLE OVERRIDE model (left over after a harness change) is coerced
    too — the override harness is claude-code but its model is a pi model."""
    import app.main as main_mod

    monkeypatch.setattr(
        main_mod.settings_store,
        "get_agent_override",
        lambda p, a: {"harness": "claude-code", "model": "gpt-5.5"},
    )
    agent = {"name": "x", "capabilities": {}}

    harness, model, _ = main_mod._chat_routing_for(agent, "kaidera-os")
    assert harness == "claude-code"
    assert model == h.HARNESS_MODELS["claude-code"][0]["value"]
    assert h.valid_model_for_harness(harness, model) is True


def test_chat_routing_keeps_a_valid_pair(monkeypatch):
    """A VALID stored pair is untouched (no spurious coercion)."""
    import app.main as main_mod

    monkeypatch.setattr(main_mod.settings_store, "get_agent_override", lambda p, a: {})
    agent = {"name": "x", "capabilities": {"harness": "pi", "model_preference": "gpt-5.4"}}

    harness, model, _ = main_mod._chat_routing_for(agent, "kaidera-os")
    assert harness == "pi"
    assert model == "gpt-5.4"  # unchanged — it's valid for pi


def test_chat_routing_catalog_lane_model_passes_through(monkeypatch):
    """An kaidera (catalog) lane keeps its catalog model — no fixed list to
    coerce against, so a valid catalog pick is never wiped."""
    import app.main as main_mod

    monkeypatch.setattr(main_mod.settings_store, "get_agent_override", lambda p, a: {})
    agent = {
        "name": "x",
        "capabilities": {"harness": "kaidera", "model_preference": "anthropic/claude-opus"},
    }
    harness, model, _ = main_mod._chat_routing_for(agent, "kaidera-os")
    assert harness == "kaidera"
    assert model == "anthropic/claude-opus"


def test_chat_routing_honors_explicit_extension_override(monkeypatch):
    """Project/customer workers are core extensions, not hardcoded imports. When
    an installed extension supplies a routing override, the agent pane uses it."""
    import app.main as main_mod

    class Extension:
        @staticmethod
        def registered_agent_routing_override(agent_name, project_key, model, reasoning):
            if agent_name == "customer-lead" and project_key == "customer-project":
                return "kaidera-no-tools", model, reasoning
            return None

    monkeypatch.setattr(
        main_mod.settings_store,
        "get_agent_override",
        lambda p, a: {
            "harness": "kaidera",
            "model": "openrouter/deepseek/deepseek-v4-pro",
            "reasoning": "high",
        },
    )
    monkeypatch.setattr(main_mod, "CONSOLE_EXTENSION_MODULES", [Extension])
    agent = {"name": "customer-lead", "capabilities": {}}

    harness, model, reasoning = main_mod._chat_routing_for(agent, "customer-project")

    assert harness == "kaidera-no-tools"
    assert model == "openrouter/deepseek/deepseek-v4-pro"
    assert reasoning == "high"


def test_console_extension_loader_imports_installed_pack_namespace(tmp_path, monkeypatch):
    """Installed project-pack code loads from KAIDERA_OS_EXTENSION_PATHS, not core app/."""
    import app.main as main_mod

    pack_root = tmp_path / ".kaidera-os" / "project-packs" / "basic-project-pack"
    module_dir = pack_root / "basic_project_pack"
    module_dir.mkdir(parents=True)
    (module_dir / "__init__.py").write_text("", encoding="utf-8")
    (module_dir / "example_worker.py").write_text(
        "def registered_agent_routing_override(agent_name, project_key, model, reasoning):\n"
        "    return ('kaidera-no-tools', model, reasoning)\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("KAIDERA_OS_EXTENSION_PATHS", str(pack_root))
    monkeypatch.setenv("KAIDERA_OS_EXTENSION_MODULES", "basic_project_pack.example_worker")
    sys.modules.pop("basic_project_pack", None)
    sys.modules.pop("basic_project_pack.example_worker", None)
    before_path = list(sys.path)
    try:
        modules = main_mod._load_console_extensions()
    finally:
        sys.path[:] = before_path
        sys.modules.pop("basic_project_pack", None)
        sys.modules.pop("basic_project_pack.example_worker", None)

    assert len(modules) == 1
    override = modules[0].registered_agent_routing_override("lead", "project", "model", "high")
    assert override == ("kaidera-no-tools", "model", "high")


def test_console_extension_hooks_register_router_and_public_paths(monkeypatch):
    """Installed project packs can expose ingress routes without core route knowledge."""
    from fastapi import APIRouter, FastAPI

    import app.main as main_mod
    from app import auth

    app = FastAPI()
    router = APIRouter()

    @router.get("/extensions/example/ping")
    async def _ping():
        return {"ok": True}

    def _register_public_paths(register):
        register("/extensions/example/callback")

    def _register_routers(target_app):
        target_app.state.extension_registered = True

    extension = SimpleNamespace(
        public_paths=["/extensions/example/event"],
        public_path_matchers=[lambda path: path.startswith("/extensions/example/ingress/")],
        register_public_paths=_register_public_paths,
        register_routers=_register_routers,
        router=router,
    )

    auth.clear_public_path_matchers()
    monkeypatch.setattr(main_mod, "CONSOLE_EXTENSION_MODULES", [extension])
    try:
        main_mod._register_console_extension_hooks(app)

        assert auth.is_public_path("/extensions/example/event")
        assert auth.is_public_path("/extensions/example/callback")
        assert auth.is_public_path("/extensions/example/ingress/hook")
        assert not auth.is_public_path("/extensions/example/admin")
        assert app.state.extension_registered is True
        assert "/extensions/example/ping" in {route.path for route in app.routes}
    finally:
        auth.clear_public_path_matchers()


# ---------------------------------------------------------------------------
#  2. the card/detail config resolvers coerce the DISPLAYED model.
# ---------------------------------------------------------------------------


def test_agents_resolve_config_coerces_displayed_model(monkeypatch):
    """`main._agents_resolve_config` (the agents-column card resolver) shows the
    COERCED model, so the card never displays an impossible pair."""
    import app.main as main_mod

    cfg = main_mod._agents_resolve_config(_impossible_agent(), {})
    assert cfg["harness"] == "claude-code"
    assert cfg["model"] == h.HARNESS_MODELS["claude-code"][0]["value"]
    assert h.valid_model_for_harness(cfg["harness"], cfg["model"]) is True


def test_agents_api_resolve_config_coerces_displayed_model():
    """The JSON agents-catalog resolver (`agents.api._harness_resolve_config`) coerces
    too — the SPA card matches the runner."""
    cfg = agents_api._harness_resolve_config(_impossible_agent(), {})
    assert cfg["harness"] == "claude-code"
    assert cfg["model"] == h.HARNESS_MODELS["claude-code"][0]["value"]


# ---------------------------------------------------------------------------
#  3. harness.agent_config_view — the Configure card's model is coerced + a hint.
# ---------------------------------------------------------------------------


def test_config_view_never_returns_an_impossible_pair():
    """`agent_config_view` resolves the EFFECTIVE model coerced to the harness, so the
    Configure card's controls never select an impossible pair."""
    view = h.agent_config_view(_impossible_agent(), {}, [], "autonomous")
    assert view["harness"] == "claude-code"
    assert view["model"] == h.HARNESS_MODELS["claude-code"][0]["value"]
    assert h.valid_model_for_harness(view["harness"], view["model"]) is True


def test_config_view_surfaces_a_coerced_hint_when_it_corrected():
    """When the stored model was INVALID for the harness, the config-view flags it
    (`model_coerced=True` + the original stored value) so the UI can show a subtle
    'model was invalid for harness — using <default>' hint."""
    view = h.agent_config_view(_impossible_agent(), {}, [], "autonomous")
    assert view.get("model_coerced") is True
    # the original (impossible) stored value is carried for the hint copy.
    assert view.get("model_invalid_original") == "gemini-3.1-pro-preview"


def test_config_view_no_coerced_hint_for_a_valid_pair():
    """A VALID stored pair carries NO coercion hint (the dot/hint only shows when we
    actually corrected something)."""
    agent = {
        "name": "ok",
        "capabilities": {"harness": "pi", "model_preference": "gpt-5.4", "thinking": "low"},
    }
    view = h.agent_config_view(agent, {}, [], "autonomous")
    assert view["model"] == "gpt-5.4"
    assert not view.get("model_coerced")
    assert not view.get("model_invalid_original")


def test_config_view_catalog_lane_model_not_coerced():
    """An kaidera (catalog) effective model is NOT coerced (no fixed list) and
    carries no hint — a valid catalog pick is preserved."""
    agent = {
        "name": "c",
        "capabilities": {"harness": "kaidera", "model_preference": "anthropic/claude-opus"},
    }
    view = h.agent_config_view(agent, {}, [], "autonomous")
    assert view["model"] == "anthropic/claude-opus"
    assert not view.get("model_coerced")
