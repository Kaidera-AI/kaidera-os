"""Designation / per-agent-override PURE helpers — the de-forked single owner.

These three pure helpers + their two value constants were DUPLICATED in THREE
places before this carve:
  * `app.settings` (the legacy facade) — `normalize_designation`,
    `_override_store_key`, `_clean_override` (annotated "Behaviour-identical to
    settings_module.service…"),
  * `app.settings_module.service` (the carved config module) — `normalize_designation`,
    `override_store_key`, `clean_override` (annotated "Lifted 1:1 from settings.py"),
  * `app.agents.service` (the roster catalog) — `_override_store_key`,
    `_normalize_designation`.
Three stores of truth silently diverge. This module is the ONE owner; every other
site delegates here.

WHY THE DOMAIN (and not `settings_module.service`, the obvious owner): the
`.importlinter` `modules-are-independent` contract pins the FIVE feature modules
(`app.analytics`, `app.agents`, `app.settings_module`, `app.dispatch`, `app.runs`)
as independent of EACH OTHER. `app.settings` is reachable from three of those
members (including `app.dispatch.api -> app.settings`),
so ANY edge `app.settings -> app.settings_module` — even a lazy in-function import —
becomes a transitive member→member edge and BREAKS the contract. Likewise a direct
`app.agents -> app.settings_module` edge is forbidden. The ONE inward target every
module (members AND the legacy facade) may depend on is `app.domain` (the
arrows-point-inward rule). So the pure helpers live HERE, and `settings.py` /
`settings_module/service.py` / `agents/service.py` all delegate INWARD to this
domain module — the same shape `dispatch.service` already uses for
`app.domain.roles`. `settings_module.service` re-exports these names unchanged, so
its public surface (and every caller of `settings_module.service.normalize_designation`
etc.) is untouched.

PURE: stdlib only (no httpx / fastapi / subprocess / psycopg2 / asyncpg, no reach
into `app.*`). The domain-purity import-linter contract + the per-module ast guard
keep it that way. The logic is lifted 1:1 — behaviour-identical to the prior copies.
"""

from __future__ import annotations

from typing import Any, Optional

# The valid designation values. Anything else is treated as "no designation
# override" → the caller falls back to the registry heuristic (which only ever
# yields interactive/autonomous). Two capabilities flow from the designation:
#   interactive   — a Lead you chat with:    chat box ✓  +  LLM/model config ✓
#   autonomous    — a non-interactive AI worker: chat box ✗ +  LLM/model config ✓
#   deterministic — a pure-code "mini" agent:    chat box ✗ +  LLM/model config ✗ (not an AI worker)
DESIGNATION_INTERACTIVE = "interactive"
DESIGNATION_AUTONOMOUS = "autonomous"
DESIGNATION_DETERMINISTIC = "deterministic"
DESIGNATIONS = (DESIGNATION_INTERACTIVE, DESIGNATION_AUTONOMOUS, DESIGNATION_DETERMINISTIC)

# The override fields a per-agent Configure row can set. Stored as plain strings; an
# empty/missing value means "no override — fall back to the registry value".
#   harness/model/reasoning : the execution config.
#   designation             : "interactive" / "autonomous" — WINS over the
#                             registry-derived classification when set (validated).
#   role                    : a free-text role-label override.
#   role_aliases            : comma-separated secondary dispatch roles.
#   auto_dispatch           : "true"/"false"; when true, an interactive lead may
#                             also be auto-dispatched by the project scheduler.
# (Lifted 1:1 from the duplicated `AGENT_OVERRIDE_FIELDS` in settings.py /
# settings_module.service.)
AGENT_OVERRIDE_FIELDS = (
    "harness", "model", "reasoning", "designation", "role", "role_aliases",
    "auto_dispatch",
)

_TRUE_VALUES = {"1", "true", "yes", "on", "enabled"}
_FALSE_VALUES = {"0", "false", "no", "off", "disabled"}


def normalize_designation(raw: Any) -> str:
    """Coerce a submitted/stored designation to a known value or "".

    Returns "interactive" / "autonomous" for a recognised value (case-insensitive),
    else "" (no override — classify via the registry heuristic). Tolerant by design
    so a hand-edited value can never raise. The single owner of this logic."""
    val = (str(raw).strip().lower() if raw is not None else "")
    return val if val in DESIGNATIONS else ""


def is_ai_worker(designation: Any) -> bool:
    """True when the (effective) designation has an LLM/model attached — an
    `interactive` Lead or an `autonomous` AI worker. ONLY `deterministic` is a
    pure-code agent with no model. An empty/unknown value defaults to True: the
    registry heuristic only ever yields interactive/autonomous, both AI workers.
    The single owner of the "does this agent get a model" rule."""
    return normalize_designation(designation) != DESIGNATION_DETERMINISTIC


def is_chat_enabled(designation: Any) -> bool:
    """True when the agent exposes an interactive chat box — ONLY `interactive`.
    `autonomous` + `deterministic` agents run without a chat input (their run
    transcripts still stream in the app). Pass the EFFECTIVE designation (override
    or registry-resolved); a blank/unknown value is treated as NOT chat-enabled.
    The single owner of the "does this agent show a chat box" rule."""
    return normalize_designation(designation) == DESIGNATION_INTERACTIVE


def normalize_boolish(raw: Any) -> str:
    """Coerce an optional form/capability boolean to "true", "false", or "".

    The settings store persists strings, while registry capabilities may carry a
    real bool. Keeping one normalizer avoids hidden truthiness differences between
    app-DB overrides and Cortex capabilities."""
    if raw is None:
        return ""
    if isinstance(raw, bool):
        return "true" if raw else "false"
    val = str(raw).strip().lower()
    if val in _TRUE_VALUES:
        return "true"
    if val in _FALSE_VALUES:
        return "false"
    return ""


def is_auto_dispatch_enabled(raw: Any) -> bool:
    """True only when an explicit auto-dispatch flag is enabled."""
    return normalize_boolish(raw) == "true"


def override_store_key(project: Optional[str], agent: str) -> str:
    """Compose the "{project}:{agent}" override-store key (lower-cased, blank-safe).
    The single owner of this pure string composition."""
    proj = (project or "").strip().lower()
    name = (agent or "").strip().lower()
    return f"{proj}:{name}"


def clean_override(raw: Any) -> dict[str, str]:
    """Coerce one stored/submitted override entry into a clean {field: str} dict,
    keeping only the known AGENT_OVERRIDE_FIELDS with non-empty string values.

    The `designation` field is additionally validated to a known value
    (interactive/autonomous); an unrecognised designation is dropped (→ falls back
    to the registry heuristic). The `role_aliases` field is normalised to a lowercase,
    comma-separated slug list (whitespace cleaned) so it can be parsed consistently by
    the dispatch resolver. Other fields keep any non-empty string. The single owner of
    this cleaning logic."""
    out: dict[str, str] = {}
    if not isinstance(raw, dict):
        return out
    for field in AGENT_OVERRIDE_FIELDS:
        val = raw.get(field)
        if val is None:
            continue
        if field == "designation":
            sval = normalize_designation(val)
        elif field == "role_aliases":
            sval = ",".join(
                p.strip().lower()
                for p in str(val).split(",")
                if p.strip()
            )
        elif field == "auto_dispatch":
            sval = normalize_boolish(val)
        else:
            sval = str(val).strip()
        if sval:
            out[field] = sval
    return out


__all__ = [
    "DESIGNATION_INTERACTIVE",
    "DESIGNATION_AUTONOMOUS",
    "DESIGNATION_DETERMINISTIC",
    "DESIGNATIONS",
    "AGENT_OVERRIDE_FIELDS",
    "normalize_designation",
    "normalize_boolish",
    "is_ai_worker",
    "is_chat_enabled",
    "is_auto_dispatch_enabled",
    "override_store_key",
    "clean_override",
]
