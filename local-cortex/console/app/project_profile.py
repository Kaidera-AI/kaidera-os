"""Project-profile loader — the harness LOADS a dropped-in project's profile to RUN it.

THE PRINCIPLE (foundational, CTO 2026-06-18): the harness is a PURE RUNTIME with ZERO
AI Workers. A *profile* is PROJECT DATA the harness LOADS — never harness code. "Drop in
a project -> the harness reads its profile -> runs + customizes it." This module names NO
project and NO worker: every project/agent string lives in the DATA file, never here.

WHAT A PROFILE IS
-----------------
ONE small, declarative DATA bundle per project, ``<project>.profile.json``, holding that
project's harness-config — the knobs that were previously UNWIRED from any profile (so a
fresh install couldn't auto-configure; Inc 2b flagged that seeded agents came up mis-designated
unless ``KAIDERA_DESIGNATION_SEED`` was set by hand). The shape::

    {
      "schema_version": "1.0",
      "project": "<key>",
      "default_project": "<key>",            # the console's default-project hint ("" = none)
      "continuous": false,                    # continuous-backlog (no-epics) UI hint
      "designations": {                        # the one-time designation/role seed
        "<project>:<agent>": {"designation": "interactive"|"autonomous", "role": "<label>"}
      },
      "portal": {                              # turnkey single-agent portal config (optional)
        "agent": "<agent>", "project": "<key>",
        "instance_name": "<public-name>",      # public instance name (the project's data, not here)
        "persona_file": "<file>",              # pointer to the persona text (NOT inline portal code)
        "persona": "<inline persona text>"     # OR inline (persona_file takes precedence if both)
      }
    }

WHERE PROFILES LIVE
-------------------
A configured *profiles dir* (default the shipped ``redistributable/examples/``; override the
dir via env ``KAIDERA_PROFILES_DIR``). The active project's profile is ``<key>.profile.json``
in that dir. The default dir is resolved from THIS file's location (never a hardcoded /Users
path), so it works in the repo and in a relocated install.

PRECEDENCE (backward-compat)
----------------------------
For every value the existing env knobs already drive, the rule is::

    explicit env knob (override)  >  profile value  >  built-in EMPTY default

The profile becomes the DEFAULT SOURCE the knobs override — the existing env-knob behaviour
is preserved exactly (an operator who sets the env still wins), but a FRESH install with NO
env now auto-configures from its project's profile.

GRACEFUL DEGRADE
----------------
Every accessor is tolerant by construction: a missing/invalid/empty profile, a down profiles
dir, or malformed JSON yields the EMPTY default (and a debug log) — it NEVER raises and NEVER
blocks startup. A greenfield install with no profile is a clean no-op (the harness stays
project-agnostic; the running project's config then comes from its own data + the registry
heuristic), exactly as before this loader existed.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

_log = logging.getLogger("console.project_profile")

# Env name a deployment uses to OVERRIDE the profiles directory. DEFAULT (unset) is the
# shipped examples dir resolved from this file's location — never a hardcoded host path.
PROFILES_DIR_ENV = "KAIDERA_PROFILES_DIR"

# Suffix that marks a project-profile DATA file. A project's profile is "<key>.profile.json".
PROFILE_SUFFIX = ".profile.json"


def _default_profiles_dir() -> Path:
    """The shipped profiles dir, resolved from THIS file's location (never hardcoded).

    In source checkouts this finds ``<repo>/redistributable/examples`` by walking
    upward. In the container image the examples directory is intentionally absent,
    so it falls back to ``/app/redistributable/examples`` and callers degrade to
    {} when it is missing."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "redistributable" / "examples"
        if candidate.is_dir():
            return candidate
    return Path("/app/redistributable/examples")


def profiles_dir() -> Path:
    """The active profiles dir — env ``KAIDERA_PROFILES_DIR`` override, else the shipped dir."""
    raw = (os.environ.get(PROFILES_DIR_ENV) or "").strip()
    if raw:
        return Path(raw).expanduser()
    return _default_profiles_dir()


def profile_path(project_key: str) -> Path:
    """The expected path of ``<project_key>.profile.json`` in the active profiles dir.

    The key is lower-cased + stripped (matching the project-key convention); a blank key
    yields a path that simply won't exist (so the caller degrades to the empty default)."""
    key = (project_key or "").strip().lower()
    return profiles_dir() / f"{key}{PROFILE_SUFFIX}"


def load_profile(project_key: str) -> dict[str, Any]:
    """Load + parse the active project's profile DATA, or {} when there is none.

    Locates ``<project_key>.profile.json`` in the configured profiles dir and returns its
    parsed object. Tolerant by construction: a blank key, a missing file, an unreadable
    file, malformed JSON, or a non-object top level all yield {} (with a debug log) — NEVER
    raises, NEVER blocks startup. This is the single read chokepoint every accessor uses."""
    if not (project_key or "").strip():
        return {}
    path = profile_path(project_key)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        # No profile for this project (the greenfield/no-op case) — silent at debug.
        _log.debug("no project profile at %s (degrading to empty)", path)
        return {}
    try:
        data = json.loads(text or "{}")
    except ValueError as exc:
        _log.warning("project profile at %s is malformed JSON (%s) — ignoring", path, exc)
        return {}
    if not isinstance(data, dict):
        _log.warning("project profile at %s is not a JSON object — ignoring", path)
        return {}
    return data


# ---------------------------------------------------------------------------
#  Typed accessors — each applies the precedence  env > profile > empty default.
#  Callers pass the env value they already read (or None) so this module never
#  has to know the consumer's env-var name; precedence stays in ONE place.
# ---------------------------------------------------------------------------


def designation_seed(project_key: str) -> dict[str, dict[str, str]]:
    """The profile's one-time designation/role seed (the ``designations`` block), or {}.

    Same shape as the legacy ``KAIDERA_DESIGNATION_SEED`` payload:
    ``{"<project>:<agent>": {"designation": ..., "role": ...}}``. Keeps only well-shaped
    "project:agent" -> {field: str} entries (a junk entry is dropped, never raised on).
    This is the profile-sourced DEFAULT; the env knob overrides it (see
    ``settings._load_designation_seed``)."""
    block = load_profile(project_key).get("designations")
    if not isinstance(block, dict):
        return {}
    seed: dict[str, dict[str, str]] = {}
    for key, entry in block.items():
        if isinstance(key, str) and ":" in key and isinstance(entry, dict):
            seed[key] = {str(k): str(v) for k, v in entry.items()}
    return seed


def default_project(profile_for: str, env_value: str | None) -> str:
    """Resolve the default-project hint with precedence  env > profile > "".

    ``profile_for`` is the project key whose profile to read for its ``default_project``
    field (the bootstrap key — typically the env value, else the harness's notion of the
    active project). ``env_value`` is the already-read env override (``KAIDERA_DEFAULT_PROJECT``
    or the app-DB ``cortex_default_project`` — whatever the caller resolved). Returns the
    first non-empty of: the env override, the profile's ``default_project``, "" (empty)."""
    env = (env_value or "").strip()
    if env:
        return env
    val = load_profile(profile_for).get("default_project")
    if isinstance(val, str) and val.strip():
        return val.strip()
    return ""


def discover_default_project() -> str:
    """Scan the profiles dir for a profile that DECLARES itself the default project, or "".

    The bootstrap for the default-project hint when NO env/setting names one: a profile's
    ``default_project`` field is the project saying "I am the default." We read every
    ``*.profile.json`` in the profiles dir and return the first non-empty ``default_project``
    it declares (sorted by filename for determinism). This is the profile-sourced DEFAULT a
    fresh install uses so it opens on the right project with NO ``KAIDERA_DEFAULT_PROJECT``
    set; the env/setting override still wins (see ``main._default_project``). Tolerant: a
    missing dir or any unreadable/malformed profile is skipped — never raises, returns ""
    when nothing declares a default."""
    d = profiles_dir()
    try:
        names = sorted(p.name for p in d.iterdir() if p.name.endswith(PROFILE_SUFFIX))
    except (OSError, ValueError):
        return ""
    for name in names:
        try:
            data = json.loads((d / name).read_text(encoding="utf-8") or "{}")
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        val = data.get("default_project")
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def continuous(project_key: str, env_value: str | None = None) -> bool:
    """Whether the project runs a continuous (no-epics) backlog — env > profile > False.

    A UI hint only (drives "continuous · no epics" labelling). ``env_value`` is the
    already-resolved env override as a string ("1"/"true"/… → True) or None when the
    caller leaves it to the profile. The profile's ``continuous`` is a bool default."""
    env = (env_value or "").strip().lower()
    if env:
        return env in ("1", "true", "on", "yes")
    val = load_profile(project_key).get("continuous")
    return bool(val) if isinstance(val, bool) else False


def continuous_projects() -> tuple[str, ...]:
    """The project keys whose PROFILE declares ``continuous: true`` — the profile-sourced
    continuous set, "" when none.

    Scans every ``*.profile.json`` in the profiles dir and returns each profile's own
    ``project`` key when its ``continuous`` flag is true (sorted, de-duped). This is the
    DEFAULT continuous set a fresh install gets with no ``KAIDERA_CONTINUOUS_PROJECTS`` env;
    the caller UNIONs the env set over it (see ``main._continuous_projects``). Tolerant: a
    missing dir or any unreadable/malformed profile is skipped — never raises."""
    d = profiles_dir()
    try:
        names = sorted(p.name for p in d.iterdir() if p.name.endswith(PROFILE_SUFFIX))
    except (OSError, ValueError):
        return ()
    out: list[str] = []
    for name in names:
        try:
            data = json.loads((d / name).read_text(encoding="utf-8") or "{}")
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or data.get("continuous") is not True:
            continue
        key = data.get("project")
        if isinstance(key, str) and key.strip() and key.strip() not in out:
            out.append(key.strip())
    return tuple(out)


def portal_config(project_key: str) -> dict[str, Any]:
    """The profile's ``portal`` block (turnkey single-agent portal config), or {}.

    The portal is a SEPARATE standalone service configured by env (PORTAL_AGENT /
    PORTAL_PROJECT / persona). This exposes the profile-supplied values a deployment uses
    to DERIVE that env: ``agent``, ``project``, ``instance_name`` (the public instance
    name), and the persona — ``persona_file`` (a pointer, resolved relative to the profiles
    dir) and/or inline ``persona`` text. Returns a normalized dict; missing keys are absent."""
    block = load_profile(project_key).get("portal")
    if not isinstance(block, dict):
        return {}
    out: dict[str, Any] = {}
    for field in ("agent", "project", "instance_name", "persona", "persona_file"):
        val = block.get(field)
        if isinstance(val, str) and val.strip():
            out[field] = val.strip()
    return out


def portal_persona(project_key: str) -> str:
    """The portal agent's persona TEXT, resolved from the profile's ``portal`` block, or "".

    Precedence: inline ``persona`` (used verbatim), else read ``persona_file`` relative to
    the profiles dir. Tolerant: a missing/unreadable persona file yields "" (with a debug
    log). The persona is DEPLOYMENT DATA (the chatted agent's own system prompt), never
    portal code."""
    cfg = portal_config(project_key)
    inline = cfg.get("persona")
    if isinstance(inline, str) and inline.strip():
        return inline
    fname = cfg.get("persona_file")
    if not (isinstance(fname, str) and fname.strip()):
        return ""
    persona_path = profiles_dir() / fname
    try:
        return persona_path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        _log.debug("portal persona file not found at %s (degrading to empty)", persona_path)
        return ""
