"""Project-profile loader — the harness LOADS a dropped-in project's profile to RUN it.

Locks the v0.1.114 contract:
  * a project profile is ONE declarative DATA bundle (``<project>.profile.json``) the
    harness loads from a configured profiles dir (default the shipped examples; env
    ``KAIDERA_PROFILES_DIR`` overrides the dir),
  * its values (designation seed, default-project, continuous, portal worker+persona)
    feed the existing consumers,
  * PRECEDENCE is explicit env knob (override) > profile value > built-in empty default,
  * the SHIPPED examples dir is project-AGNOSTIC (v0.1.119 cleanup): it carries only the
    fill-in-the-blank ``example.profile.json`` TEMPLATE, declares NO default project, and
    seeds NO designations — a fresh install opens with no project, no seeded agents,
  * a missing/invalid profile degrades to empty and NEVER raises (boot-safe).

The harness names NO project and NO worker (§ pure-runtime / zero AI Workers) — every
project/agent string lives in the DATA fixtures here, never in the loader code.
"""

from __future__ import annotations

import json
from pathlib import Path

from app import project_profile as pp

# Repo-root path to the SHIPPED reference profiles, resolved from this test's location
# (tests/ -> console/ -> local-cortex/ -> repo root), so the clean-package tests read the
# real examples dir the redistributable ships.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_SHIPPED_EXAMPLES = _REPO_ROOT / "redistributable" / "examples"


# ---------------------------------------------------------------------------
#  CLEAN PACKAGE scenario (v0.1.119): the shipped examples dir is project-agnostic
#  — NO default project, NO seeded designations, only the example.profile.json TEMPLATE.
#  (Replaces the old fresh-install-kaidera-os tests; the project-specific kaidera-os /
#  customer package profiles no longer ship — see the redist package-cleanup audit.)
# ---------------------------------------------------------------------------


def test_shipped_examples_declare_no_default_project(monkeypatch):
    """The SHIPPED `redistributable/examples/` declares NO default project.

    Root cause of the old default-project=kaidera-os bug (audit B.1): a shipped
    `kaidera-os.profile.json` declared ``default_project: kaidera-os``. The harness now ships
    only the `example.profile.json` TEMPLATE (``default_project: ""``), so a greenfield box
    with no env/setting opens with NO project and the operator creates their own."""
    monkeypatch.setenv(pp.PROFILES_DIR_ENV, str(_SHIPPED_EXAMPLES))
    assert pp.discover_default_project() == ""
    assert pp.continuous_projects() == ()


def test_shipped_examples_seed_no_designations(monkeypatch):
    """The SHIPPED examples seed NO agent designations (no kai/ren, no any project).

    The project-specific kaidera-os profile that seeded kai + ren INTERACTIVE no longer ships;
    the package is project-agnostic, so the designation seed for any key is empty."""
    monkeypatch.setenv(pp.PROFILES_DIR_ENV, str(_SHIPPED_EXAMPLES))
    assert pp.designation_seed("kaidera-os") == {}
    assert pp.designation_seed("anything") == {}


def test_shipped_example_profile_template_is_inert(monkeypatch):
    """The shipped `example.profile.json` is a fill-in-the-blank TEMPLATE: it loads as a
    dict but declares an empty default_project and (being keyed to a placeholder) seeds
    nothing for a real project key — so it never auto-configures a fresh install."""
    monkeypatch.setenv(pp.PROFILES_DIR_ENV, str(_SHIPPED_EXAMPLES))
    template_path = _SHIPPED_EXAMPLES / f"example{pp.PROFILE_SUFFIX}"
    assert template_path.is_file()  # the one shipped profile is the template
    data = json.loads(template_path.read_text(encoding="utf-8"))
    assert data.get("default_project", "") == ""  # never declares itself the default
    # No real-project key resolves to it, so a fresh install gets nothing auto-seeded.
    assert pp.default_project("kaidera-os", None) == ""


# ---------------------------------------------------------------------------
#  PRECEDENCE: explicit env knob (override) > profile value > empty default.
# ---------------------------------------------------------------------------


def _write_profile(d: Path, key: str, body: dict) -> None:
    (d / f"{key}{pp.PROFILE_SUFFIX}").write_text(json.dumps(body), encoding="utf-8")


def test_precedence_env_overrides_profile_for_designation_seed(monkeypatch, tmp_path):
    """The `KAIDERA_DESIGNATION_SEED` env knob OVERRIDES the profile's designations block.

    A hermetic profile says agent `acme:bot` is autonomous; the env knob says interactive.
    The env wins (precedence env > profile), proving the existing override behaviour is
    preserved — the profile is only the DEFAULT source."""
    from app import settings as settings_store

    _write_profile(
        tmp_path, "acme",
        {"project": "acme", "designations": {"acme:bot": {"designation": "autonomous"}}},
    )
    monkeypatch.setenv(pp.PROFILES_DIR_ENV, str(tmp_path))
    monkeypatch.setattr(settings_store, "_seed_active_project_key", lambda: "acme")

    # profile alone → autonomous
    assert pp.designation_seed("acme")["acme:bot"]["designation"] == "autonomous"

    # env override present → env wins
    monkeypatch.setenv(
        "KAIDERA_DESIGNATION_SEED",
        '{"acme:bot": {"designation": "interactive", "role": "lead"}}',
    )
    seed = settings_store._load_designation_seed()
    assert seed["acme:bot"]["designation"] == "interactive"
    assert seed["acme:bot"]["role"] == "lead"


def test_precedence_env_overrides_profile_for_default_project(monkeypatch, tmp_path):
    """`default_project` precedence: env override > profile > empty."""
    _write_profile(tmp_path, "acme", {"project": "acme", "default_project": "acme"})
    monkeypatch.setenv(pp.PROFILES_DIR_ENV, str(tmp_path))

    # no env → profile value
    assert pp.default_project("acme", None) == "acme"
    assert pp.default_project("acme", "") == "acme"
    # env override → env wins
    assert pp.default_project("acme", "override-proj") == "override-proj"
    # no env + profile has no default_project → empty
    _write_profile(tmp_path, "bare", {"project": "bare"})
    assert pp.default_project("bare", None) == ""


def test_precedence_env_overrides_profile_for_continuous(monkeypatch, tmp_path):
    """`continuous` precedence: env override > profile > False."""
    _write_profile(tmp_path, "acme", {"project": "acme", "continuous": True})
    monkeypatch.setenv(pp.PROFILES_DIR_ENV, str(tmp_path))

    # no env → profile value (True)
    assert pp.continuous("acme") is True
    # env override "0" → False (env wins over the profile's True)
    assert pp.continuous("acme", "0") is False
    # env override "1" wins over a profile with no/False continuous
    _write_profile(tmp_path, "calm", {"project": "calm", "continuous": False})
    assert pp.continuous("calm") is False
    assert pp.continuous("calm", "1") is True


# ---------------------------------------------------------------------------
#  GRACEFUL DEGRADE: missing / invalid profile → empty, never raises.
# ---------------------------------------------------------------------------


def test_missing_profile_degrades_to_empty(monkeypatch, tmp_path):
    """A missing profile (no `<key>.profile.json`) degrades to the empty default for every
    accessor and NEVER raises — the boot-safe greenfield path."""
    monkeypatch.setenv(pp.PROFILES_DIR_ENV, str(tmp_path))  # empty dir, no profiles

    assert pp.load_profile("nope") == {}
    assert pp.designation_seed("nope") == {}
    assert pp.default_project("nope", None) == ""
    assert pp.continuous("nope") is False
    assert pp.portal_config("nope") == {}
    assert pp.portal_persona("nope") == ""
    assert pp.discover_default_project() == ""
    assert pp.continuous_projects() == ()


def test_blank_key_and_missing_dir_degrade(monkeypatch, tmp_path):
    """A blank project key, and a profiles dir that doesn't exist, both degrade cleanly."""
    # blank key → empty regardless of dir contents
    monkeypatch.setenv(pp.PROFILES_DIR_ENV, str(tmp_path))
    assert pp.load_profile("") == {}
    assert pp.designation_seed("") == {}

    # non-existent dir → discovery + load tolerate it (no raise)
    monkeypatch.setenv(pp.PROFILES_DIR_ENV, str(tmp_path / "does" / "not" / "exist"))
    assert pp.discover_default_project() == ""
    assert pp.continuous_projects() == ()
    assert pp.load_profile("anything") == {}


def test_malformed_profile_degrades_to_empty(monkeypatch, tmp_path):
    """A malformed (non-JSON) or non-object profile degrades to empty without raising."""
    (tmp_path / f"bad{pp.PROFILE_SUFFIX}").write_text("{not json", encoding="utf-8")
    (tmp_path / f"list{pp.PROFILE_SUFFIX}").write_text("[1, 2, 3]", encoding="utf-8")
    monkeypatch.setenv(pp.PROFILES_DIR_ENV, str(tmp_path))

    assert pp.load_profile("bad") == {}
    assert pp.load_profile("list") == {}
    assert pp.designation_seed("bad") == {}
    # discovery skips the malformed files rather than raising
    assert pp.discover_default_project() == ""


# ---------------------------------------------------------------------------
#  PORTAL + DISCOVERY — hermetic fixtures (the project-specific reference
#  profile no longer ships; v0.1.119 cleanup). The portal worker/persona/rename contract
#  is validated against a tmp_path profile, not shipped project DATA.
# ---------------------------------------------------------------------------


def test_portal_profile_exposes_worker_persona_and_rename(monkeypatch, tmp_path):
    """A profile's `portal` block exposes the worker, the public rename (instance_name),
    and the persona resolved from its `persona_file` pointer (deployment DATA, not code)."""
    (tmp_path / "turnkey-persona.txt").write_text(
        "You are Sample, the public assistant.", encoding="utf-8"
    )
    _write_profile(
        tmp_path, "turnkey",
        {"project": "turnkey",
         "portal": {"agent": "sample-worker", "project": "turnkey", "instance_name": "sample",
                    "persona_file": "turnkey-persona.txt"}},
    )
    monkeypatch.setenv(pp.PROFILES_DIR_ENV, str(tmp_path))

    cfg = pp.portal_config("turnkey")
    assert cfg["agent"] == "sample-worker"
    assert cfg["instance_name"] == "sample"
    assert cfg["persona_file"] == "turnkey-persona.txt"

    persona = pp.portal_persona("turnkey")
    assert persona.strip()  # non-empty persona text was read from the pointed file
    assert "Sample" in persona  # the persona file names the public instance


def test_inline_persona_takes_precedence_over_file(monkeypatch, tmp_path):
    """An inline `persona` in the portal block is used verbatim (over a `persona_file`)."""
    _write_profile(
        tmp_path, "demo",
        {"project": "demo", "portal": {"agent": "bot", "persona": "INLINE PERSONA",
                                        "persona_file": "missing.txt"}},
    )
    monkeypatch.setenv(pp.PROFILES_DIR_ENV, str(tmp_path))
    assert pp.portal_persona("demo") == "INLINE PERSONA"


def test_discover_default_and_continuous_scan_profiles_dir(monkeypatch, tmp_path):
    """Discovery scans the dir: the first profile declaring `default_project` wins (sorted);
    every profile with `continuous: true` is collected by its own project key."""
    _write_profile(tmp_path, "aaa", {"project": "aaa", "continuous": True})
    _write_profile(tmp_path, "bbb", {"project": "bbb", "default_project": "bbb",
                                     "continuous": True})
    _write_profile(tmp_path, "ccc", {"project": "ccc"})  # neither flag
    monkeypatch.setenv(pp.PROFILES_DIR_ENV, str(tmp_path))

    # sorted by filename → "bbb" is the first (and only) one declaring a default_project
    assert pp.discover_default_project() == "bbb"
    # continuous set = the two profiles flagged true, by their own project key
    assert pp.continuous_projects() == ("aaa", "bbb")
