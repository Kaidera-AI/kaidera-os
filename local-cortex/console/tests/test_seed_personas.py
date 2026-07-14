"""seed_personas — the default Lead + AI-worker roster, idempotent app-config seeding."""

from __future__ import annotations

from app import deploy_mode, seed_personas


def test_roster_is_one_lead_plus_ai_workers():
    roster = seed_personas.DEFAULT_ROSTER
    leads = [r for r in roster if r["persona"] == "Lead"]
    workers = [r for r in roster if r["persona"] == "AI worker"]
    assert len(leads) == 1
    assert leads[0]["designation"] == "interactive" and leads[0]["role"] == "lead"
    assert len(workers) >= 2 and all(w["designation"] == "autonomous" for w in workers)
    # the old "CPO" persona/role is gone from the defaults
    assert not any("cpo" in (r["role"] + r["persona"]).lower() for r in roster)


def test_default_harness_follows_deploy_mode(monkeypatch):
    monkeypatch.setenv(deploy_mode.ENV_VAR, "selfcontained")
    assert seed_personas.default_harness() == "kaidera"
    monkeypatch.setenv(deploy_mode.ENV_VAR, "kaidera-os")
    assert seed_personas.default_harness() == "claude-code"


def test_seed_writes_each_persona(monkeypatch):
    saved: list[tuple] = []
    registered: list[str] = []
    monkeypatch.setattr(seed_personas.settings_store, "load_agent_overrides", lambda: {})
    res = seed_personas.seed_default_personas(
        "acme",
        harness="kaidera",
        save=lambda p, a, ov: saved.append((p, a, ov)),
        register=lambda a, meta: registered.append(a),
    )
    assert len(res) == len(seed_personas.DEFAULT_ROSTER)
    assert all(r["action"] == "seeded" for r in res)
    assert {a for _, a, _ in saved} == {"lead", "dev", "keeper", "qa"}
    assert all(ov["harness"] == "kaidera" and ov["designation"] and ov["role"] for _, _, ov in saved)
    assert set(registered) == {"lead", "dev", "keeper", "qa"}


def test_seed_is_idempotent(monkeypatch):
    # 'lead' already has an override → skipped; the rest are seeded.
    monkeypatch.setattr(
        seed_personas.settings_store, "load_agent_overrides",
        lambda: {"acme:lead": {"harness": "claude-code"}},
    )
    saved: list[str] = []
    res = seed_personas.seed_default_personas("acme", save=lambda p, a, ov: saved.append(a))
    actions = {r["agent"]: r["action"] for r in res}
    assert actions["lead"] == "exists"
    assert actions["dev"] == "seeded"
    assert "lead" not in saved


def test_seed_overwrite_forces_rewrite(monkeypatch):
    monkeypatch.setattr(
        seed_personas.settings_store, "load_agent_overrides",
        lambda: {"acme:lead": {"harness": "claude-code"}},
    )
    saved: list[str] = []
    res = seed_personas.seed_default_personas("acme", overwrite=True, save=lambda p, a, ov: saved.append(a))
    assert all(r["action"] == "seeded" for r in res)
    assert "lead" in saved
