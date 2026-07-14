"""Settings feature logic — app/system settings + per-agent config + flags, behind
the port.

The functional core of the `settings` module (Track A, the THIRD feature carve —
analytics was first, agents second). It owns the OPERATIONAL settings substance:

  1. APP / SYSTEM settings — the raw key→value rows behind the System page
     (`load_app_settings` / `upsert_app_settings`), the durable store the
     schema-driven System form persists into.
  2. PER-AGENT config — the console-local harness/model/reasoning/designation/role
     OVERRIDES keyed "{project}:{agent}": get one, resolve the effective
     designation (override-first), and save (merge — a blank field clears, an
     agent left empty is dropped). The save WRITES THROUGH THE PORT to wherever it
     writes today — this carve lifts the config LOGIC behind the port without
     changing the canonical-source semantics (the ConfigPort consolidation — making
     Cortex authoritative over the app-DB+JSON fork — is a NOTED follow-up, not
     this step).
  3. DESIGNATION — `normalize_designation` (coerce to interactive/autonomous or
     ""), the one-time non-destructive seed of the default roster designations,
     and the field-cleaning (`clean_override`).
  4. PROJECT FLAGS — the autonomy + propose-mode kill-switches (fail-safe OFF: a
     blank project or a down store reads OFF, so an outage can only ever turn a
     flag off, never on).

LAYER RULE (arrows point inward, ratified design §3): this module depends ONLY on
`domain.ports.OperationalStorePort` (the app-DB operational surface). It imports
NOTHING outward (no fastapi / httpx / subprocess / psycopg2 / asyncpg) and never
reaches back into `app.main`, the concrete `app.appdb` / `app.adapters`, or the
legacy `app.settings` facade. The schema/form rendering + JSON seed/fallback +
Manifold credential storage stays in `app.settings` (a UI/schema concern); this
module owns the port-backed config logic the
System page + the Configure card + the agents catalog all sit on.

The logic is lifted 1:1 from `settings.normalize_designation` /
`settings._clean_override` / `settings._override_store_key` /
`settings.get_agent_designation` / `settings.save_agent_override` /
`settings.is_project_autonomous` / `settings.is_propose_mode` (+ their setters),
so the carve is behaviour-preserving. The one-time designation SEED is NOT carried
here — it is PROJECT-SUPPLIED DATA loaded by the legacy facade
(`settings.seed_agent_overrides`); the harness names no worker (§ pure-runtime).

Graceful-degrade is the house law: every read returns its empty default and every
write returns False when the store is down (or absent) — exactly the SettingsDB
`UNAVAILABLE` → safe-default contract the adapter maps. It never raises.
"""

from __future__ import annotations

from typing import Any, Optional

from app.domain import designation as _designation
from app.domain.ports import CatalogModel, OperationalStorePort

# The masked placeholder a SET secret renders as in the System-schema JSON (the
# raw secret value NEVER leaves the server — only `is_set` + this marker). Kept
# identical to the legacy facade's `settings.MASK_PLACEHOLDER` so the JSON and
# the legacy HTML show the same "•••• set" affordance; defined here (not imported)
# so the pure service stays free of the `app.settings` dependency.
SECRET_MASK = "•••• set"

# The field types the System-schema JSON contract exposes. The raw schema may use
# any of these; an unrecognised raw type is coerced to "text" (safe default) so a
# hand-edited schema can never produce an out-of-contract type. "select" carries an
# `options` list (static) and/or an `options_source` (resolved client-side from live
# data, e.g. the registered projects).
SCHEMA_FIELD_TYPES = ("text", "number", "bool", "secret", "readonly", "select")

# Per-model provenance → a short human "freshness" label for the Providers JSON.
# Mirrors `providers._SOURCE_TAG` (kept here, not imported, so the service stays
# pure): "live" = fetched live. Historical provenance labels remain readable. Anything else -> the
# raw source string verbatim (honest, never fabricated).
_FRESHNESS_LABEL = {
    "live": "live",
    "merged": "live + supplement",
    "supplement": "supplement",
}

# The two valid designation values + the override-field tuple. DE-FORKED: these now
# come from `app.domain.designation` (the single owner) and are re-exported here so
# the module's public surface is unchanged — every caller of
# `settings_module.service.DESIGNATION_*` / `.AGENT_OVERRIDE_FIELDS` keeps working,
# and the values can never diverge from the legacy facade / the agents module again.
DESIGNATION_INTERACTIVE = _designation.DESIGNATION_INTERACTIVE
DESIGNATION_AUTONOMOUS = _designation.DESIGNATION_AUTONOMOUS
DESIGNATIONS = _designation.DESIGNATIONS
AGENT_OVERRIDE_FIELDS = _designation.AGENT_OVERRIDE_FIELDS

# NOTE: the designation/role SEED is NOT app code. The harness names NO worker — it is
# a pure runtime and "agents" are AI Workers that belong to PROJECTS (CTO 2026-06-18,
# pure-runtime / zero-AI-Workers principle). The one-time seed is PROJECT-SUPPLIED DATA,
# loaded (env-driven, EMPTY by default) by the legacy facade `settings.seed_agent_overrides`
# / `settings._load_designation_seed` — this module carries no hardcoded seed.


# ---------------------------------------------------------------------------
#  Pure helpers — designation / key / cleaning (lifted 1:1 from settings.py)
# ---------------------------------------------------------------------------


# DE-FORKED: the three pure helpers now DELEGATE to `app.domain.designation` (the
# single owner). They keep their module-level names + signatures so this module's
# public surface is unchanged — `settings_module.service.normalize_designation` /
# `.override_store_key` / `.clean_override` still resolve, now to the one canonical
# implementation that `app.settings` + `app.agents.service` also delegate to.


def normalize_designation(raw: Any) -> str:
    """Coerce a submitted/stored designation to a known value or "".

    Returns "interactive" / "autonomous" for a recognised value (case-insensitive),
    else "" (no override — classify via the registry heuristic). DELEGATES to
    `app.domain.designation.normalize_designation` (the single owner)."""
    return _designation.normalize_designation(raw)


def override_store_key(project: Optional[str], agent: str) -> str:
    """Compose the "{project}:{agent}" storage key (lower-cased, blank-safe).
    DELEGATES to `app.domain.designation.override_store_key` (the single owner)."""
    return _designation.override_store_key(project, agent)


def clean_override(raw: Any) -> dict[str, str]:
    """Coerce one stored/submitted override entry into a clean {field: str} dict,
    keeping only the known AGENT_OVERRIDE_FIELDS with non-empty string values.

    The `designation` field is additionally validated to a known value
    (interactive/autonomous); an unrecognised designation is dropped (→ falls back
    to the registry heuristic). Other fields keep any non-empty string. DELEGATES to
    `app.domain.designation.clean_override` (the single owner)."""
    return _designation.clean_override(raw)


# ---------------------------------------------------------------------------
#  System-schema JSON contract (pure) — the typed System form, secrets masked.
#
#  The SPA's System tab needs the form as JSON: groups → typed fields, each with
#  its CURRENT value EXCEPT secrets, which return ONLY `is_set` + a masked
#  placeholder (the raw secret value NEVER leaves the server — the load-bearing
#  contract). This is the pure transform; the api shell sources the raw SCHEMA
#  (from the legacy `app.settings` facade, a UI/schema concern) + the current values
#  (from the store via the service) and calls this. Mirrors the masking
#  `settings.view_groups` does for the HTML, lifted into a JSON shape.
# ---------------------------------------------------------------------------


def _coerce_field_type(raw_type: Any) -> str:
    """Coerce a raw schema field type to one of SCHEMA_FIELD_TYPES (default
    "text"). Keeps the JSON contract closed so the SPA can switch on a known set."""
    t = str(raw_type or "text").strip().lower()
    return t if t in SCHEMA_FIELD_TYPES else "text"


def build_system_schema(
    schema: list[dict[str, Any]], values: dict[str, Any]
) -> dict[str, Any]:
    """Shape the System form as JSON: `{groups:[{key,label,fields:[{key,label,type,
    group,help,...}]}]}` with `type ∈ text|number|bool|secret|readonly`.

    For each field the CURRENT value is returned EXCEPT a SECRET field, which
    returns `is_set` (bool — whether a secret is stored) + a masked placeholder
    (SECRET_MASK / "") and NEVER the raw secret value. A bool field's value is
    coerced to bool; everything else passes the stored value through (the store
    already typed it). `group` echoes the owning group's key so the SPA can render
    a flat field list grouped, and `help` carries the field hint.

    PURE: takes the raw schema + a values map, returns the JSON dict. No I/O, no
    `app.settings` import — the caller injects both. The secret-masking here is the
    contract the providers/keys tab depends on."""
    vals = values if isinstance(values, dict) else {}
    groups_out: list[dict[str, Any]] = []
    for g in schema or []:
        group_key = g.get("id") or g.get("key") or ""
        fields_out: list[dict[str, Any]] = []
        for f in g.get("fields", []) or []:
            key = f.get("key")
            if not key:
                continue
            ftype = _coerce_field_type(f.get("type"))
            entry: dict[str, Any] = {
                "key": key,
                "label": f.get("label") or key,
                "type": ftype,
                "group": group_key,
                "help": f.get("hint") or f.get("help") or "",
                "placeholder": f.get("placeholder", ""),
            }
            if ftype == "secret":
                # NEVER the raw secret — only presence + a masked marker.
                has = bool(str(vals.get(key) or "").strip())
                entry["is_set"] = has
                entry["value"] = ""                       # secrets never seed an input
                entry["placeholder"] = SECRET_MASK if has else ""
            elif ftype == "bool":
                entry["value"] = bool(vals.get(key, f.get("default")))
            elif ftype == "select":
                # the stored value (or default) + the choosable options. STATIC
                # options come from the schema; a DYNAMIC `options_source` (e.g.
                # "projects") is a hint the SPA resolves from live data (the
                # registered-projects list) so the dropdown is never hardcoded.
                entry["value"] = vals.get(key, f.get("default"))
                entry["options"] = [str(o) for o in (f.get("options") or [])]
                src = f.get("options_source")
                if src:
                    entry["options_source"] = str(src)
            else:
                # text / number / readonly — pass the stored (already-typed) value,
                # falling back to the schema default when absent.
                entry["value"] = vals.get(key, f.get("default"))
            fields_out.append(entry)
        groups_out.append(
            {
                "key": group_key,
                "label": g.get("title") or g.get("label") or group_key,
                "fields": fields_out,
            }
        )
    return {"groups": groups_out}


# ---------------------------------------------------------------------------
#  Providers JSON contract (pure) — the live model catalog grouped by provider.
#
#  The SPA's Providers&Models tab needs the catalog grouped by provider with the
#  per-model fields it renders. The api shell fetches the live catalog via the
#  `ModelCatalogPort` (which never raises — degrades to []), then this pure
#  transform groups the flat `CatalogModel`s into the documented shape.
# ---------------------------------------------------------------------------


def _freshness_for(source: Optional[str]) -> str:
    """Map a model's provenance `source` to a short human freshness label
    (live / live + supplement / supplement), else the raw source verbatim."""
    s = (source or "live").strip().lower()
    return _FRESHNESS_LABEL.get(s, s)


def build_providers_config(built_ins: list[dict[str, Any]]) -> dict[str, Any]:
    """Shape the Manifold configuration view without exposing its credential.

    MASKING CONTRACT (load-bearing): the builder emits ONLY the documented
    presence/label/ref fields — a raw key on an input dict is dropped, never echoed
    (a test asserts no raw key ever appears)."""
    rows: list[dict[str, Any]] = []

    for b in built_ins or []:
        name = str(b.get("name") or "")
        key_field = b.get("key_field")
        row: dict[str, Any] = {
            "name": name,
            "label": str(b.get("label") or name.title() or name),
            "key_is_set": bool(b.get("key_is_set")),
            "is_custom": False,
            "testable": bool(b.get("testable")),
            # the canonical write/test target for a built-in is its secret-key field
            "provider_ref": str(key_field) if key_field else name,
        }
        if key_field:
            row["key_field"] = str(key_field)
        # Pre-filled endpoint (read-only) so the operator sees the URL is already
        # built in and only needs to paste a key. Absent for SDK/non-compat providers.
        b_base = b.get("base_url")
        if b_base:
            row["base_url"] = str(b_base)
        project_id = b.get("project_id")
        if project_id:
            row["project_id"] = str(project_id)
        rows.append(row)

    return {"providers": rows}


def group_catalog_models(models: list[CatalogModel]) -> dict[str, Any]:
    """Group a flat `CatalogModel` list by provider into the Providers JSON:
    `{providers:[{name, models:[{model,type,reasoning_tiers,input_price_per_mtok,
    output_price_per_mtok,context_window,source,freshness}]}]}`.

    Provider order follows first-seen (the adapter already emits a stable per-
    provider order). PURE: no fetch, no raise — an empty list yields
    `{providers: []}` (the graceful-degrade target shape)."""
    by_provider: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for m in models or []:
        name = getattr(m, "provider", "") or ""
        if name not in by_provider:
            by_provider[name] = {"name": name, "models": []}
            order.append(name)
        by_provider[name]["models"].append(
            {
                "model": getattr(m, "id", "") or "",
                "type": getattr(m, "type", "chat") or "chat",
                "reasoning_tiers": list(getattr(m, "reasoning_levels", []) or []),
                "input_price_per_mtok": getattr(m, "price_in_per_mtok", None),
                "output_price_per_mtok": getattr(m, "price_out_per_mtok", None),
                "context_window": getattr(m, "context_window", None),
                "source": getattr(m, "source", "live") or "live",
                "freshness": _freshness_for(getattr(m, "source", "live")),
            }
        )
    return {"providers": [by_provider[name] for name in order]}


# ---------------------------------------------------------------------------
#  The service
# ---------------------------------------------------------------------------


class SettingsService:
    """The operational settings surface: app/system settings, per-agent config, and
    the project autonomy/propose-mode flags — all behind an `OperationalStorePort`.

    Construct with the port (the operational data source). `store` is OPTIONAL so a
    caller that only needs the pure helpers (`normalize_designation` /
    `clean_override` / `override_store_key`, all module-level) can use the module
    without a store; the instance read surfaces then degrade to their empty defaults
    and the writes return False (the same graceful-degrade as a down store)."""

    def __init__(self, *, store: Optional[OperationalStorePort] = None) -> None:
        self._store = store

    # -- app / system settings --------------------------------------------------

    def load_app_settings(self) -> dict[str, Any]:
        """The raw app_settings key→value map (the durable rows behind the System
        page). {} when no store / a down store (the caller falls back to the JSON
        seed). Never raises."""
        if self._store is None:
            return {}
        try:
            return self._store.load_app_settings() or {}
        except Exception:  # pragma: no cover - the port degrades; belt-and-braces
            return {}

    def upsert_app_settings(self, items: dict[str, Any]) -> bool:
        """Upsert many app_settings key→value rows (the System-save durable write).
        True on success, False when no store / down / the write failed. Never
        raises."""
        if self._store is None or not items:
            return False
        try:
            return bool(self._store.upsert_app_settings(items))
        except Exception:  # pragma: no cover - the port degrades; belt-and-braces
            return False

    # -- per-agent config -------------------------------------------------------

    def load_overrides(self) -> dict[str, dict[str, str]]:
        """The whole per-agent override map ({"{project}:{agent}": {field: str}}).
        Every entry is re-cleaned to known string fields. {} when no store / down.
        Lifted from `settings.load_agent_overrides`."""
        if self._store is None:
            return {}
        try:
            raw = self._store.load_agent_overrides() or {}
        except Exception:  # pragma: no cover - the port degrades; belt-and-braces
            return {}
        out: dict[str, dict[str, str]] = {}
        for key, entry in raw.items():
            cleaned = clean_override(entry)
            if cleaned:
                out[str(key)] = cleaned
        return out

    def get_override(self, project: Optional[str], agent: str) -> dict[str, str]:
        """One agent's console-local override ({} if none / no store / down). Keys
        are a subset of AGENT_OVERRIDE_FIELDS, all non-empty strings. Lifted from
        `settings.get_agent_override`."""
        if self._store is None:
            return {}
        try:
            return clean_override(self._store.get_agent_override(project or "", agent))
        except Exception:  # pragma: no cover - the port degrades; belt-and-braces
            return {}

    def resolve_designation(
        self,
        project: Optional[str],
        agent: str,
        overrides: Optional[dict[str, dict[str, str]]] = None,
    ) -> str:
        """The console designation override for one agent: "interactive",
        "autonomous", or "" (no override — caller falls back to the registry
        heuristic). `overrides` is the pre-loaded map (pass it to avoid a per-agent
        store read in a loop). Lifted 1:1 from `settings.get_agent_designation`."""
        if overrides is not None:
            entry = overrides.get(override_store_key(project, agent), {})
            return normalize_designation(entry.get("designation"))
        return normalize_designation(self.get_override(project, agent).get("designation"))

    def save_override(
        self, project: Optional[str], agent: str, override: dict[str, Any]
    ) -> dict[str, str]:
        """Persist (MERGE) a console-local override for one agent through the port.

        `override` carries any of harness/model/reasoning/designation/role. A blank
        value CLEARS that field (falls back to the registry value); a non-blank
        value sets it (designation is validated to interactive/autonomous). An agent
        left with no overrides is DROPPED (an empty entry → the port DELETEs the
        row). Returns the agent's post-save effective override dict ({} when no
        store / down / cleared). Lifted 1:1 from `settings.save_agent_override`'s
        merge semantics.

        CANONICAL-SOURCE NOTE: this writes THROUGH THE PORT to wherever the store
        writes today — the carve lifts the merge LOGIC behind the port without
        changing where the bytes land (the ConfigPort consolidation is a follow-up,
        not this step)."""
        # Merge the submitted fields over the agent's current entry.
        entry = self.get_override(project, agent)
        for field in AGENT_OVERRIDE_FIELDS:
            if field not in override:
                continue
            val = override[field]
            if field == "designation":
                sval = normalize_designation(val)  # only a known value persists
            else:
                sval = "" if val is None else str(val).strip()
            if sval:
                entry[field] = sval
            else:
                entry.pop(field, None)  # blank → clear this override

        if self._store is None:
            return {}
        try:
            # An empty entry is a DELETE (the port drops the row); a non-empty entry
            # is the COMPLETE replacement for that agent. A down store returns False
            # (the write didn't land) → report {} so the caller never shows a
            # persisted override that wasn't actually saved.
            ok = self._store.save_agent_override(project or "", agent, entry)
        except Exception:  # pragma: no cover - the port degrades; belt-and-braces
            return {}
        return entry if ok else {}

    # -- project flags (autonomy + propose-mode; fail-safe OFF) -----------------

    def is_project_autonomous(self, project: Optional[str]) -> bool:
        """True only if autonomous dispatch is explicitly ON for this project. A
        blank project, no row, OR a down store → False (fail-safe OFF — an outage
        can never enable autonomy it can't read). Lifted 1:1 from
        `settings.is_project_autonomous`."""
        key = (project or "").strip().lower()
        if not key or self._store is None:
            return False
        try:
            return bool(self._store.is_project_autonomous(key))
        except Exception:  # pragma: no cover - the port degrades; belt-and-braces
            return False

    def set_project_autonomy(
        self, project: Optional[str], enabled: bool, updated_by: Optional[str] = None
    ) -> bool:
        """Flip the autonomous-dispatch switch for one project. True on a successful
        persist, False if no store / blank project / the write failed. Lifted 1:1
        from `settings.set_project_autonomous`."""
        key = (project or "").strip().lower()
        if not key or self._store is None:
            return False
        try:
            return bool(self._store.set_project_autonomy(key, bool(enabled), updated_by))
        except Exception:  # pragma: no cover - the port degrades; belt-and-braces
            return False

    def list_autonomous_projects(self) -> list[str]:
        """The set of projects with autonomy ON (lower-cased). [] when none / a down
        store (fail-safe: the loop idles). Lifted 1:1 from
        `settings.autonomous_projects`."""
        if self._store is None:
            return []
        try:
            return list(self._store.list_autonomous_projects() or [])
        except Exception:  # pragma: no cover - the port degrades; belt-and-braces
            return []

    def is_propose_mode(self, project: Optional[str]) -> bool:
        """True only if propose-mode (training-wheels approval gate) is explicitly
        ON for this project. A blank project, no row, OR a down store → False
        (fail-safe: auto-spawn, existing behaviour). Lifted 1:1 from
        `settings.is_propose_mode`."""
        key = (project or "").strip().lower()
        if not key or self._store is None:
            return False
        try:
            return bool(self._store.is_propose_mode(key))
        except Exception:  # pragma: no cover - the port degrades; belt-and-braces
            return False

    def set_propose_mode(
        self, project: Optional[str], enabled: bool, updated_by: Optional[str] = None
    ) -> bool:
        """Flip the propose-mode gate for one project. True on a successful persist,
        False if no store / blank project / the write failed. Lifted 1:1 from
        `settings.set_propose_mode`."""
        key = (project or "").strip().lower()
        if not key or self._store is None:
            return False
        try:
            return bool(self._store.set_propose_mode(key, bool(enabled), updated_by))
        except Exception:  # pragma: no cover - the port degrades; belt-and-braces
            return False


__all__ = [
    "SettingsService",
    "DESIGNATION_INTERACTIVE",
    "DESIGNATION_AUTONOMOUS",
    "DESIGNATIONS",
    "AGENT_OVERRIDE_FIELDS",
    "SECRET_MASK",
    "SCHEMA_FIELD_TYPES",
    "normalize_designation",
    "override_store_key",
    "clean_override",
    "build_system_schema",
    "group_catalog_models",
    "build_providers_config",
]
