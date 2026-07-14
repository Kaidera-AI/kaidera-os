"""Console settings store for the Kaidera OS Harness console (R4a; E007 app-DB).

The load/save layer for ALL console settings: the System-config page values
(Cortex connection, provider API keys, harness paths/flags, app preferences), the
operator-added custom providers, and the per-agent harness/model/reasoning/
designation/role overrides.

STORAGE (E007 / DATA_SEPARATION) — moved into the app-DB:
  * The SOURCE OF TRUTH is now the app-DB (`harness-appdb`): System fields + the
    custom-providers + seed-marker side blobs live in `app_settings`, and the
    per-agent overrides live in `agent_settings` (see appdb.SettingsDB +
    .agents/data/appdb/2026-06-01-settings.sql). Agent harness/model routing is
    OPERATIONAL config, so it belongs in the app-DB, NOT in Cortex (Cortex stays
    pure agent memory).
  * `config/settings.local.json` is kept as a FALLBACK/SEED only: reads fall back
    to it and writes use it ONLY when the app-DB is down (psycopg2 missing /
    container stopped), so the console NEVER crashes if the operational store is
    unavailable. On startup `migrate_json_to_appdb()` does a ONE-TIME idempotent
    import of an existing JSON file into the app-DB.
  * The PUBLIC API of this module is unchanged — every caller (main.py, the
    templates) keeps working; only the backing store moved.

IMPORTANT — what this is NOT:
  * This is NOT the real Cortex/system `.env`. It never reads or writes any real
    `.env`, secret file, or environment variable. The System page is deliberately
    a console-owned store so the harness UI can be exercised without ever touching
    real secrets. (Local, single-user; see the security note below.)

Design:
  * A declarative SCHEMA (groups → fields) drives BOTH the rendered form and the
    accepted POST keys. Adding a field is a one-line schema edit — the route and
    template iterate the schema, so nothing else changes.
  * `field_type` is one of: "text" (plain), "secret" (masked in the UI; the raw
    value never leaves the server except on an explicit reveal the user can't do
    here — the page shows "•••• set"), "number", "bool", "readonly".
  * Defaults live in the schema. `load()` always returns a complete dict (every
    schema key present), tolerating an empty/partial store (app-DB or file).
  * Writes go to the app-DB when it is reachable; the JSON file is written only
    as a degraded fallback. Only known schema keys are persisted (unknown POST
    keys ignored), and a blank secret on save is treated as "leave the stored
    secret unchanged" so an operator can edit non-secret fields without re-typing
    keys.

Security note: secrets live in the app-DB (local loopback Postgres); the
gitignored JSON file can hold them only when running in degraded DB-less fallback
mode or as a legacy seed. This is a local single-user harness — there is no
at-rest encryption and no server-side access control. Secrets are ALWAYS masked
in any HTML/serialization (the raw value never reaches a template). Do not put
production secrets here; it is a dogfood/dev store.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from . import appdb as _appdb
from .domain import designation as _designation

# The app-DB settings backend (sync, lazy, graceful-degrade). Reads prefer it;
# when it reports appdb.UNAVAILABLE we fall back to the local JSON file so the
# console keeps working with the app-DB container down.
_db = _appdb.settings_db
_UNAVAILABLE = _appdb.UNAVAILABLE

# DE-FORK (Track A settings carve): the PURE config helpers (`normalize_designation`
# / `_override_store_key` / `_clean_override`) + their value constants were
# DUPLICATED here ("Behaviour-identical to settings_module.service…") AND in
# `agents/service.py`. They now live in ONE place — `app.domain.designation` (the
# inward functional core) — and this legacy facade DELEGATES to it.
#
# WHY THE DOMAIN, not `app.settings_module`: `app.settings` is reachable from three
# import-linter `modules-are-independent` members (via `app.providers -> app.settings`
# and `app.dispatch.api -> app.settings`), so ANY edge `app.settings ->
# app.settings_module` — even a lazy in-function import (grimp graphs those too) —
# would be a transitive member→member edge and BREAK the contract. `app.domain` is
# the ONE inward target every module + this facade may depend on (arrows point
# inward), so the helpers live there and everyone delegates to them. The values stay
# behaviour-identical; `settings_module.service` re-exports the SAME names, so its
# callers are untouched.

# ---------------------------------------------------------------------------
#  Store location — console/config/settings.local.json (gitignored)
# ---------------------------------------------------------------------------

# app/settings.py -> app/ -> console/  (parents[1] is the console dir)
CONSOLE_DIR = Path(__file__).resolve().parents[1]
CONFIG_DIR = CONSOLE_DIR / "config"
SETTINGS_PATH = CONFIG_DIR / "settings.local.json"

# Sentinel rendered (and accepted on POST) for a secret that already has a value
# but is shown masked. A submitted secret equal to this means "unchanged".
MASK_PLACEHOLDER = "•••• set"


# ---------------------------------------------------------------------------
#  Schema — groups → fields. Drives the form AND the accepted POST keys.
# ---------------------------------------------------------------------------
#
# Each group:  {id, title, sub, icon, open, fields:[...]}
# Each field:  {
#     key:        storage key (also the form input name)
#     label:      human label
#     field kind: text | secret | number | bool | readonly
#     default:    default value (str | bool | int)
#     hint:       optional helper text under the control
#     placeholder optional input placeholder (non-secret)
#   }
#
# Icons are inline SVG path-sets (stroke="currentColor") matching the prototype
# `.env-group` look (console-v2.html). Kept here so the template stays logic-free.

_IC_PLUG = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">'
    '<path d="M9 2v6M15 2v6M6 8h12v3a6 6 0 0 1-12 0z"/><path d="M12 17v5"/></svg>'
)
_IC_KEY = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">'
    '<circle cx="8" cy="15" r="4"/><path d="M10.8 12.2 20 3M16 7l3 3M14 9l2 2"/></svg>'
)
_IC_TERM = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">'
    '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 9l3 3-3 3M13 15h4"/></svg>'
)
_IC_SLIDERS = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">'
    '<path d="M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3"/>'
    '<path d="M1 14h6M9 8h6M17 16h6"/></svg>'
)

SCHEMA: list[dict[str, Any]] = [
    {
        "id": "cortex",
        "title": "Cortex connection",
        "sub": "How the console reaches the local Cortex API",
        "icon": _IC_PLUG,
        "open": True,
        "fields": [
            {
                "key": "cortex_base_url",
                "label": "Base URL",
                "type": "readonly",
                "default": "http://localhost:8501",
                "hint": "The Cortex API the console talks to — set at install via the "
                        "CORTEX_BASE_URL environment variable. Shown live, read-only "
                        "(the live connection + health are on the Cortex tab).",
            },
            {
                "key": "cortex_default_project",
                "label": "Default project",
                "type": "select",
                "default": "",
                # Options are the REGISTERED projects, resolved dynamically (the SPA
                # fills them from the live /projects list). Empty = first project.
                "options_source": "projects",
                "hint": "The project selected when the console first opens — pick one of "
                        "your registered projects (empty = the first one).",
            },
        ],
    },
    # NOTE (canonicalization — Track 2): the provider API-key fields USED to live
    # here as a `providers` group, but that triple-duplicated them (System + the
    # read-only Providers tab + the raw editor). The Providers tab is now the ONE
    # control surface for provider keys/config — a provider key is set/edited ONLY
    # there. The KEYS the providers surface owns are enumerated in
    # PROVIDER_SECRET_KEYS below (kept so the raw editor + any filter can exclude
    # them in one place). The underlying STORE is unchanged — only the owning
    # SURFACE moved. System keeps the NON-provider settings (Cortex-connection,
    # harness, app preferences).
    {
        "id": "harness",
        "title": "Harness",
        "sub": "Local harness paths + behaviour flags (placeholders)",
        "icon": _IC_TERM,
        "open": False,
        "fields": [
            {
                "key": "harness_default",
                "label": "Default harness",
                "type": "select",
                "default": "kaidera",  # fitness:allow-literal "kaidera" is the HARNESS/product name (the native workhorse runtime), not a project key
                # The shipped harness integrations. The JSON API filters this list
                # through the edition/license entitlement seam before rendering it.
                "options": ["claude-code", "codex", "kaidera", "pi"],  # fitness:allow-literal harness identifiers (product), not project keys
                "hint": "Harness a NEW agent uses when it declares none. kaidera uses "
                        "connected provider APIs; Claude Code, Codex, and PI use their "
                        "installed subscription CLIs.",
            },
            {
                "key": "model_default",
                "label": "Default model (kaidera)",  # fitness:allow-literal "kaidera" = the HARNESS name in the field label, not a project key
                "type": "text",
                "default": "",
                # Read live by harness.harness_default_model("kaidera"). The out-of-the-box  # fitness:allow-literal "kaidera" harness name in a code-reference comment, not a project key
                # default model PER DEPLOYMENT (each project ships its own); a per-agent pick
                # always wins. Empty → the built-in default (kaidera-manifold/ollama-cloud/minimax-m3).
                "hint": "Model a NEW kaidera agent uses when it declares none — e.g. "
                        "kaidera-manifold/ollama-cloud/minimax-m3. Empty = the built-in default. This is the "
                        "out-of-the-box default for this deployment; a per-agent model always wins.",
            },
            {
                "key": "harness_autostart",
                "label": "Auto-start the autonomy engine",
                "type": "bool",
                "default": False,
                "hint": "Run the deterministic autonomy engine when the console boots. "
                        "Each project is still gated by its Dashboard project autonomous "
                        "dispatch and propose-mode controls; takes effect on the next "
                        "console restart.",
            },
        ],
    },
]


# ---------------------------------------------------------------------------
#  Schema helpers
# ---------------------------------------------------------------------------

def _all_fields() -> list[dict[str, Any]]:
    """Flatten every field across all groups (schema iteration helper)."""
    return [f for g in SCHEMA for f in g["fields"]]


def _field_index() -> dict[str, dict[str, Any]]:
    """Map storage key -> field spec, for typed coercion on save."""
    return {f["key"]: f for f in _all_fields()}


def defaults() -> dict[str, Any]:
    """The complete default settings dict (every schema key → its default)."""
    return {f["key"]: f["default"] for f in _all_fields()}


def is_secret(key: str) -> bool:
    """True if `key` is a secret field (masked in the UI, never echoed)."""
    spec = _field_index().get(key)
    return bool(spec and spec.get("type") == "secret")


# ---------------------------------------------------------------------------
#  Provider-credential keys — the canonical DE-DUP set (Track 2 canonicalization)
# ---------------------------------------------------------------------------
#
# These provider-credential storage keys USED to be System SCHEMA fields (the old
# `providers` group). They now live ONLY in the Providers tab (the single control
# surface). They are STILL valid storage keys (the underlying store is unchanged —
# the Providers tab writes them via the same secret write), they just no longer
# appear in the System form NOR in the raw App-settings editor (no triple-
# duplication). Enumerated HERE in ONE place so any surface that must exclude them
# (the raw editor; a future filter) reads a single source.
#
# The 12 canonical provider API keys + the remaining provider credential fields
# (account ids + the SigV4 pair + the extra provider secrets) — everything the
# Providers tab owns.
PROVIDER_SECRET_KEYS: tuple[str, ...] = (
    # Kaidera AI Manifold credentials are platform-minted by license login. They still
    # live in the provider-owned side store so System saves do not drop them.
    "kaidera_manifold_api_key",
    "kaidera_manifold_base_url",
    "kaidera_manifold_project_id",
    "anthropic_api_key",
    "openai_api_key",
    "openrouter_api_key",
    "fireworks_api_key",
    "groq_api_key",
    "siliconflow_api_key",
    "dashscope_api_key",
    "alibaba_cloud_api_key",
    "deepseek_api_key",
    "together_api_key",
    "cohere_api_key",
    "nvidia_api_key",
    "inception_api_key",
    "moonshot_api_key",
    # Ollama Cloud — an OpenAI-compatible hosted API (base https://ollama.com, the
    # /v1 OpenAI-compat path + a Bearer key). Owned by the Providers tab (the
    # canonical home), NOT a System-schema field (no duplicate surface).
    "ollama_cloud_api_key",
    # the remaining provider-credential fields the Providers tab also owns
    "fireworks_account_id",
    "perplexity_api_key",
    "xai_api_key",
    "aws_access_key_id",
    "aws_secret_access_key",
    "aws_region",
    # codex (OpenAI/ChatGPT) subscription OAuth token bundle — a JSON blob the app owns via the
    # in-app device-code login (app/codex_oauth.py), NOT a pasted key. Listed here so the raw
    # App-settings editor never surfaces the tokens. (Inc 4b — eliminates the Pi CLI.)
    "codex_oauth",
)


def provider_secret_keys() -> tuple[str, ...]:
    """The provider-credential storage keys the Providers tab owns (the canonical
    DE-DUP set). A surface that must exclude provider keys reads THIS in one place
    rather than re-listing them — so the keys live in exactly one place now."""
    return PROVIDER_SECRET_KEYS


HARNESS_MODEL_OVERRIDES_KEY = "harness_model_overrides"


def _clean_harness_model_overrides(raw: Any) -> dict[str, list[dict[str, str]]]:
    """Normalize the operator-managed harness model catalog sidecar.

    Shape in app_settings:
        {"claude-code": [{"value": "fable", "label": "Fable 5"}]}

    Stored outside the System schema because this is an operational catalog sidecar,
    not a user-facing scalar setting. Be permissive on read so older/manual rows do
    not break config rendering.
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    if not isinstance(raw, dict):
        return {}

    out: dict[str, list[dict[str, str]]] = {}
    for harness, rows in raw.items():
        key = str(harness or "").strip().lower()
        if not key:
            continue
        if isinstance(rows, (str, bytes)):
            rows_iter: list[Any] = [rows]
        elif isinstance(rows, list):
            rows_iter = rows
        else:
            continue

        seen: set[str] = set()
        cleaned: list[dict[str, str]] = []
        for row in rows_iter:
            if isinstance(row, dict):
                value = str(row.get("value") or row.get("id") or row.get("model") or "").strip()
                label = str(row.get("label") or row.get("display_name") or value).strip()
            else:
                value = str(row or "").strip()
                label = value
            if not value or "\n" in value or "\r" in value or len(value) > 200:
                continue
            if value in seen:
                continue
            seen.add(value)
            cleaned.append({"value": value, "label": label or value})
        if cleaned:
            out[key] = cleaned
    return out


def load_harness_model_overrides() -> dict[str, list[dict[str, str]]]:
    """Return operator-added harness model catalog rows from raw app_settings.

    `load()` intentionally drops non-schema sidecars, so model catalog consumers use
    this explicit accessor instead of depending on private raw settings internals.
    """
    return _clean_harness_model_overrides(_read_raw().get(HARNESS_MODEL_OVERRIDES_KEY))


def filter_non_provider_settings(values: dict[str, Any] | None) -> dict[str, Any]:
    """Return `values` with every provider-credential key (PROVIDER_SECRET_KEYS)
    REMOVED — the surface filter for the raw App-settings editor.

    Provider keys have a canonical home in the Providers tab, so they must NOT be
    surfaced in the raw editor (a provider key is set/edited ONLY in Providers). The
    underlying STORE is untouched — this is a SURFACE filter only (the stored rows
    still exist; they're just not echoed into the raw key→value editor). Tolerates a
    None/non-dict input (→ {})."""
    src = values if isinstance(values, dict) else {}
    excluded = set(PROVIDER_SECRET_KEYS)
    return {k: v for k, v in src.items() if k not in excluded}


# Settings keys RETIRED from the SCHEMA over time. Kept here so the raw App-settings
# editor (and any "extra keys" surface) never re-surfaces a value a stale store still
# holds for a since-removed field — such a key is neither a current schema field nor
# an operator-added extra, so it would otherwise leak back into the raw editor.
_RETIRED_SETTING_KEYS: tuple[str, ...] = (
    "theme",                # the console SPA is always the glass-dark theme; no light/dark toggle
    "poll_interval_secs",   # the SPA uses fixed per-surface poll cadences, not one global knob
    "harness_scripts_path",  # worker scripts self-derive their path (E007 Inc 3); no longer read
)


def surface_extra_settings(values: dict[str, Any] | None) -> dict[str, Any]:
    """Return ONLY the genuinely-EXTRA app-settings keys — the surface filter the raw
    App-settings editor uses (the real fix for the "raw editor duplicates the System
    fields + leaks internal flags" papercut).

    The raw key→value editor is a FALLBACK for operator-added keys NOT covered by the
    typed System form. It must therefore EXCLUDE everything that already has a proper
    home or is internal:
      * the current typed SCHEMA fields  (shown in the System form — no duplication),
      * the provider secret keys         (owned by the Providers tab),
      * the RETIRED keys                 (since-removed fields a stale store may hold),
      * the structural / internal blobs  (the per-agent overrides, the custom-providers
                                          list, and any `_`-prefixed marker such as the
                                          `_designation_seed_applied` seed flag).
    What remains is only the keys a human genuinely added out-of-band — usually none,
    so the raw editor stays empty unless there's something real to show. The STORE is
    untouched — this is a SURFACE filter only. Tolerates a None/non-dict input (→ {})."""
    src = values if isinstance(values, dict) else {}
    excluded: set[str] = set(PROVIDER_SECRET_KEYS)
    excluded.update(f["key"] for f in _all_fields())   # current typed System fields
    excluded.update(_RETIRED_SETTING_KEYS)             # since-removed fields
    excluded.update({
        AGENT_OVERRIDES_KEY,
        CUSTOM_PROVIDERS_KEY,
        HARNESS_MODEL_OVERRIDES_KEY,
        SEED_MARKER_KEY,
    })
    return {
        k: v
        for k, v in src.items()
        if k not in excluded and not str(k).startswith("_")  # drop any internal marker
    }


# ---------------------------------------------------------------------------
#  Typed coercion
# ---------------------------------------------------------------------------

def _coerce(spec: dict[str, Any], raw: Any) -> Any:
    """Coerce a stored / submitted raw value to the field's declared type.

    Tolerant by design (this is a local sandbox store): a bad value falls back
    to the field default rather than raising, so a hand-edited file can never
    500 the page. bool accepts true/1/on/yes; number accepts ints (floats are
    truncated); everything else is stringified."""
    ftype = spec.get("type", "text")
    default = spec.get("default")
    if raw is None:
        return default
    if ftype == "bool":
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in ("1", "true", "on", "yes")
    if ftype == "number":
        try:
            return int(float(str(raw).strip()))
        except (TypeError, ValueError):
            return default
    # text / secret / readonly → string
    return str(raw)


def normalize(data: dict[str, Any] | None) -> dict[str, Any]:
    """Return a complete, typed settings dict: every schema key present, each
    value coerced to its declared type. Unknown keys in `data` are dropped.

    This is the single funnel every read goes through, so callers always get a
    uniform shape regardless of how partial / stale the on-disk file is."""
    src = data if isinstance(data, dict) else {}
    idx = _field_index()
    out: dict[str, Any] = {}
    for key, spec in idx.items():
        out[key] = _coerce(spec, src.get(key, spec.get("default")))
    return out


# ---------------------------------------------------------------------------
#  Load / save — app-DB FIRST (source of truth), JSON file as fallback/seed.
#
#  Everything in this module funnels reads through `_read_raw()` (returns the
#  full JSON-shaped dict) and writes through `_atomic_write(payload)` (takes the
#  full JSON-shaped dict). Centralising the app-DB swap HERE means every System /
#  custom-provider / per-agent helper transparently uses the app-DB, with the
#  JSON file as a fallback read/write only when the app-DB cannot answer — no
#  caller changed.
#
#  The JSON-shaped dict <-> two-table mapping:
#    * agent_overrides blob  <->  agent_settings table
#    * everything else        <->  app_settings table (one key→JSON row each:
#                                   System fields + custom_providers + seed marker)
# ---------------------------------------------------------------------------


def _file_read_raw() -> dict[str, Any]:
    """Read the raw JSON object from the local file, or {} if absent / unreadable
    / not an object. Never raises (a corrupt file degrades to {})."""
    try:
        text = SETTINGS_PATH.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return {}
    try:
        obj = json.loads(text or "{}")
    except ValueError:
        return {}
    return obj if isinstance(obj, dict) else {}


def _db_read_raw() -> dict[str, Any] | None:
    """Reconstruct the full JSON-shaped settings dict from the app-DB, or None
    when the app-DB can't answer (caller then falls back to the file).

    Merges `app_settings` (System fields + custom_providers + seed marker) with
    the per-agent overrides from `agent_settings` under AGENT_OVERRIDES_KEY — the
    exact shape the JSON file held."""
    app = _db.load_app_settings()
    if app is _UNAVAILABLE:
        return None
    overrides = _db.load_agent_overrides()
    if overrides is _UNAVAILABLE:
        return None
    raw: dict[str, Any] = dict(app)  # type: ignore[arg-type]
    if overrides:
        raw[AGENT_OVERRIDES_KEY] = overrides
    return raw


def _db_write_raw(payload: dict[str, Any]) -> bool:
    """Write the full JSON-shaped `payload` into the app-DB (app_settings +
    agent_settings), making the DB match `payload` exactly. Returns True on
    success, False when the app-DB is down (caller then writes the JSON fallback).

    Full-replace semantics (same as the JSON file): app_settings keys absent from
    `payload` and agent_settings rows absent from the overrides blob are DELETED,
    so a cleared override / removed provider actually disappears."""
    # Split the payload: the agent-overrides blob goes to agent_settings; every
    # other top-level key is one app_settings row.
    overrides = payload.get(AGENT_OVERRIDES_KEY)
    overrides = overrides if isinstance(overrides, dict) else {}
    app_items = {k: v for k, v in payload.items() if k != AGENT_OVERRIDES_KEY}

    # 1. app_settings — upsert present keys, delete keys no longer present.
    existing_app = _db.load_app_settings()
    if existing_app is _UNAVAILABLE:
        return False
    if not _db.upsert_app_settings(app_items):
        return False
    for stale in set(existing_app.keys()) - set(app_items.keys()):  # type: ignore[union-attr]
        _db.delete_app_setting(stale)

    # 2. agent_settings — upsert present agents, delete agents no longer present.
    existing_ov = _db.load_agent_overrides()
    if existing_ov is _UNAVAILABLE:
        return False
    # Clean every entry to known string fields before writing (defensive; the
    # callers already clean, but a direct payload could carry junk).
    cleaned_blob = {
        str(k): _clean_override(v) for k, v in overrides.items()
    }
    cleaned_blob = {k: v for k, v in cleaned_blob.items() if v}
    if not _db.replace_all_agent_overrides(cleaned_blob):
        return False
    for stale_key in set(existing_ov.keys()) - set(cleaned_blob.keys()):  # type: ignore[union-attr]
        proj, _, name = stale_key.partition(":")
        _db.save_agent_override(proj, name, {})  # empty entry → DELETE row
    return True


def _read_raw() -> dict[str, Any]:
    """The full JSON-shaped settings dict — app-DB FIRST, JSON file as fallback.

    Returns {} only when BOTH stores are empty/unreadable. This is the single
    read chokepoint the whole module uses, so the app-DB swap is invisible to
    every caller. Never raises."""
    db = _db_read_raw()
    if db is not None:
        return db
    return _file_read_raw()


def _atomic_write(payload: dict[str, Any]) -> None:
    """Persist the full JSON-shaped `payload`.

    The app-DB is the canonical store. When the app-DB write succeeds, the save is
    complete and no JSON mirror is updated. If the app-DB is down, the JSON file is
    used as the degraded local fallback. The file write stays atomic: temp file in
    the same dir, fsync, os.replace."""
    db_ok = False
    try:
        db_ok = _db_write_raw(payload)
    except Exception:  # pragma: no cover - the DB layer already swallows, belt+braces
        db_ok = False
    if db_ok:
        return

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    fd, tmp_path = tempfile.mkstemp(
        prefix=".settings.", suffix=".tmp", dir=str(CONFIG_DIR)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, SETTINGS_PATH)
    except BaseException:
        # Clean up the temp file on any failure so we never leak .settings.*.tmp.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# The two valid designation values. DE-FORKED: re-exported from the single owner
# `app.domain.designation` so the several call sites that reference them keep working,
# and the values can never drift from the canonical copy. (See the de-fork note near
# the top of this module.)
DESIGNATION_INTERACTIVE = _designation.DESIGNATION_INTERACTIVE
DESIGNATION_AUTONOMOUS = _designation.DESIGNATION_AUTONOMOUS
DESIGNATIONS = _designation.DESIGNATIONS


def normalize_designation(raw: Any) -> str:
    """Coerce a submitted/stored designation to a known value or "".

    Returns "interactive" / "autonomous" for a recognised value (case-insensitive),
    else "" (meaning: no override — classify via the registry heuristic). Tolerant
    by design so a hand-edited file can never 500 the page.

    DELEGATES to `app.domain.designation.normalize_designation` — the single owner
    of this pure helper (de-fork; see the module note above). Behaviour-identical."""
    return _designation.normalize_designation(raw)


# Storage key for the per-agent override blob.
# Kept OUTSIDE the SCHEMA so the schema-driven System form never touches it; the
# Configure page reads/writes it through the dedicated agent-override API below.
AGENT_OVERRIDES_KEY = "agent_overrides"

# Storage key for operator-added CUSTOM providers (name + base URL + masked API
# key). A list of {id, name, base_url, api_key}. Kept OUTSIDE the SCHEMA (the
# fixed provider-key fields in SCHEMA are built-ins; this is the open-ended set
# the "+ Add provider" affordance manages) and preserved across every System
# save, exactly like AGENT_OVERRIDES_KEY. See the custom-provider helpers below.
CUSTOM_PROVIDERS_KEY = "custom_providers"

# Marker key recording that the one-time designation
# seed has run. Once set we NEVER re-seed, so an operator who clears a seeded
# designation (e.g. demotes a worker back to autonomous) keeps that choice across
# restarts. Kept outside SCHEMA (System form never touches it).
SEED_MARKER_KEY = "_designation_seed_applied"

# Env name a deployment points at its PROJECT-SUPPLIED designation seed. The harness
# itself names NO worker — it is a pure runtime, and "agents" are AI Workers that
# belong to PROJECTS (CTO 2026-06-18, pure-runtime / zero-AI-Workers principle). A
# project supplies its own designation policy as DATA; the harness only LOADS it.
DESIGNATION_SEED_ENV = "KAIDERA_DESIGNATION_SEED"


def _seed_active_project_key() -> str:
    """The project key whose PROFILE supplies the designation seed when no env is set.

    Resolved from CONFIG only (no project literal, no import of main): the app-DB
    ``cortex_default_project`` setting, else env ``KAIDERA_DEFAULT_PROJECT`` — the same
    default-project resolution the console uses elsewhere. Returns "" when nothing is
    configured (then there is no profile to read and the seed is empty). Never raises."""
    try:
        val = (load().get("cortex_default_project") or "").strip()
        if val:
            return val
    except Exception:
        pass  # settings store unavailable → fall through to env
    return (os.environ.get("KAIDERA_DEFAULT_PROJECT") or "").strip()


def _load_designation_seed() -> dict[str, dict[str, str]]:
    """The PROJECT-SUPPLIED one-time designation/role seed — DATA, never app code.

    The harness ships project-agnostic (§ pure-runtime): the DEFAULT is an EMPTY seed,
    so a greenfield install with no project profile + no env stamps NO worker names into
    app data. A deployment supplies its project's designation policy as DATA in TWO ways,
    with PRECEDENCE  env knob (override) > project profile > empty:

      1. env ``KAIDERA_DESIGNATION_SEED`` — inline JSON or a path to a ``.json`` file,
         shaped ``{"<project>:<agent>": {"designation": "interactive"|"autonomous",
         "role": "<label>"}}``. This OVERRIDE is preserved exactly (an operator who sets
         it still wins).
      2. the active project's PROFILE ``designations`` block (the DEFAULT SOURCE) — loaded
         by ``project_profile`` from the configured profiles dir for the resolved default
         project (a dropped-in ``<project>.profile.json``; the shipped package carries only
         the project-agnostic ``redistributable/examples/example.profile.json`` template, so
         a deployment supplies its own). This lets a project that ships a profile with a
         designations block auto-configure with NO manual ``KAIDERA_DESIGNATION_SEED``.

    The eventual source of truth is the Cortex ``roster_policy`` (E006 Inc04); until then a
    deployment carries this as project data, not a hardcoded harness literal.

    Tolerant by design: a missing env, unreadable file, malformed JSON, or absent profile
    yields an EMPTY seed (the registry heuristic then classifies) — never raises, never
    blocks boot."""
    raw = (os.environ.get(DESIGNATION_SEED_ENV) or "").strip()
    if not raw:
        # No env override → the profile is the default source (env > profile > empty).
        from . import project_profile as _profile
        return _profile.designation_seed(_seed_active_project_key())
    try:
        if raw.endswith(".json") and os.path.exists(raw):
            with open(raw, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        else:
            data = json.loads(raw)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    # Keep only well-shaped "project:agent" -> {field: str} entries.
    seed: dict[str, dict[str, str]] = {}
    for key, entry in data.items():
        if isinstance(key, str) and ":" in key and isinstance(entry, dict):
            seed[key] = {str(k): str(v) for k, v in entry.items()}
    return seed


def _store_initialized() -> bool:
    """True if the SOURCE-OF-TRUTH store already holds settings.

    Prefers the app-DB (≥1 app_settings row); falls back to "the JSON file
    exists" when the app-DB is down. Drives the first-run materialise + the
    one-time JSON import (both no-ops once the store is populated)."""
    probe = _db.has_any_app_settings()
    if probe is not _UNAVAILABLE:
        return bool(probe)
    return SETTINGS_PATH.exists()


def migrate_json_to_appdb() -> bool:
    """ONE-TIME idempotent import of an existing JSON store into the app-DB.

    If the app-DB is reachable but EMPTY (no app_settings rows yet) and a
    `config/settings.local.json` file exists, import its System config + custom
    providers + seed marker + agent overrides into the app-DB so the app-DB
    becomes the source of truth on first run after this upgrade. A no-op when the
    app-DB already has settings, when the app-DB is down, or when no JSON file
    exists. Never raises (best-effort). Returns True if an import was performed."""
    probe = _db.has_any_app_settings()
    if probe is _UNAVAILABLE or probe:  # DB down, or already populated → no import
        return False
    file_raw = _file_read_raw()
    if not file_raw:
        return False  # nothing to import
    try:
        # _db_write_raw makes the app-DB match the (file) payload exactly.
        return _db_write_raw(file_raw)
    except Exception:  # pragma: no cover - belt+braces; DB layer already swallows
        return False


def seed_agent_overrides() -> None:
    """Apply the PROJECT-SUPPLIED one-time designation/role seed (idempotent + non-destructive).

    First runs the one-time JSON→app-DB import (so an existing file populates the
    app-DB before we seed), materialises the store from defaults if it's still
    empty, then — only if the seed has not run before (SEED_MARKER_KEY) — layers the
    seed LOADED from project-supplied data (`_load_designation_seed`, env-driven,
    EMPTY by default) onto the override blob WITHOUT overwriting any agent entry that
    already exists. Sets the marker so it never re-seeds (an operator-cleared
    designation stays cleared across restarts).

    Project-agnostic by construction: the harness names no worker — with no
    project-supplied seed (the greenfield default) this is a pure no-op that stamps
    nothing into app data; the running project's designations come from its own data
    + the Cortex registry heuristic. Safe to call on every app start."""
    # Import an existing JSON file into the app-DB on the first run after upgrade.
    migrate_json_to_appdb()

    seed = _load_designation_seed()
    if not seed:
        return  # no project-supplied seed → nothing to stamp (greenfield default)

    raw = _read_raw()
    if not raw:
        raw = defaults()
    if raw.get(SEED_MARKER_KEY):
        return  # already seeded — respect any later operator edits

    blob = raw.get(AGENT_OVERRIDES_KEY)
    blob = dict(blob) if isinstance(blob, dict) else {}
    for store_key, seed_entry in seed.items():
        if store_key in blob:
            continue  # never clobber an existing (operator/registry) entry
        cleaned = _clean_override(seed_entry)
        if cleaned:
            blob[store_key] = cleaned

    payload = dict(load() if _store_initialized() else defaults())
    # carry any pre-existing custom-provider blob across the seed write
    customs = raw.get(CUSTOM_PROVIDERS_KEY)
    if isinstance(customs, list) and customs:
        payload[CUSTOM_PROVIDERS_KEY] = customs
    if blob:
        payload[AGENT_OVERRIDES_KEY] = blob
    payload[SEED_MARKER_KEY] = True
    _atomic_write(payload)


def ensure_store() -> dict[str, Any]:
    """Guarantee the store is materialised (app-DB first run → defaults), run the
    one-time JSON import, and apply the one-time designation seed. Returns the
    normalized settings dict. Idempotent."""
    if not _store_initialized():
        # Try importing an existing JSON file first; if there was none, lay down
        # defaults so the store is never empty on first paint.
        if not migrate_json_to_appdb():
            _atomic_write(defaults())
    seed_agent_overrides()
    return load()


def load() -> dict[str, Any]:
    """Load the current settings as a complete, typed dict (defaults fill any
    gap). Tolerates a missing / corrupt / partial file — always usable.

    NOTE: this returns ONLY the schema-driven System settings (normalize drops
    unknown keys — INCLUDING every provider API key, which lives outside the
    schema). A provider-key CONSUMER must use load_with_secrets(), not load(),
    or the saved key is silently lost. The per-agent override blob lives under
    AGENT_OVERRIDES_KEY and is read separately via load_agent_overrides()."""
    return normalize(_read_raw())


def load_with_secrets() -> dict[str, Any]:
    """Normalized System settings WITH the provider-credential secrets merged back.

    `load()` funnels through `normalize()`, which keeps only System-schema keys and
    DROPS everything else — including every provider API key, because provider keys
    deliberately live OUTSIDE the schema (the Providers tab owns them; see
    PROVIDER_SECRET_KEYS). Correct for the System form, but every PROVIDER-KEY
    CONSUMER — the live catalog fetch, the kaidera key resolution, the
    per-provider Test — needs the real values. They are persisted raw by the
    Providers write (upsert_app_settings), so this overlays the raw provider-secret
    rows on the normalized base: ONE cfg carrying BOTH the typed System settings and
    the provider keys.

    Reading bare `load()` in a provider-key resolver silently loses every saved key —
    the root cause of a freshly-saved key testing/authenticating as "not set"."""
    raw = _read_raw()
    out = normalize(raw)
    for key in PROVIDER_SECRET_KEYS:
        val = raw.get(key)
        if val is not None and str(val).strip():
            out[key] = val
    return out


def save(updates: dict[str, Any]) -> dict[str, Any]:
    """Merge `updates` over the current stored settings and persist atomically.

    Only known schema keys are applied (unknown keys ignored). Values are typed
    via the schema. SECRET handling: a submitted secret that is blank OR equal to
    the mask placeholder means "leave the stored secret unchanged" — so an
    operator can save non-secret edits without re-entering every key. A non-blank
    secret replaces the stored one.

    The per-agent override blob (AGENT_OVERRIDES_KEY) is PRESERVED across a
    System save — normalize() would otherwise drop it, so we re-attach the raw
    blob before writing. Returns the new normalized settings dict (the post-save
    System state; the override blob is not echoed back)."""
    current = load()
    idx = _field_index()
    merged = dict(current)

    for key, spec in idx.items():
        if key not in updates:
            continue
        raw = updates[key]
        if spec.get("type") == "secret":
            submitted = "" if raw is None else str(raw).strip()
            # Blank or still-masked → keep whatever is already stored.
            if submitted == "" or submitted == MASK_PLACEHOLDER:
                continue
            merged[key] = submitted
        else:
            merged[key] = _coerce(spec, raw)

    merged = normalize(merged)
    _persist_with_overrides(merged)
    return merged


# ---------------------------------------------------------------------------
#  Per-agent overrides (R4c) — console-local harness/model/reasoning + profile
# ---------------------------------------------------------------------------
#
# Console-local OVERRIDES of an agent's effective harness/model/reasoning AND its
# profile (designation/role), keyed by "{project}:{agent}". These overlay the
# registry value the Configure page reads from /projects/{key}/runtime; they are NOT
# written back to Cortex on save (promotion to the registry is an explicit action).
#
# CONSOLE-LOCAL BY DESIGN (feature-gap #81, the CTO's reversed decision): the Cortex
# agent registry (`capabilities`, E006 Inc04) stays the source of truth, and a config
# SAVE writes ONLY this console-local blob — it does NOT push to the registry. The local
# blob is the fast display/routing + classification overlay. Committing the config to the
# registry is an EXPLICIT, on-demand gesture: the "Promote to registry" action calls
# `registry_sync.promote_agent_to_registry` (`POST /agents` UPSERT, the conflict-update
# jsonb-MERGES `capabilities`) only when the operator chooses to. (designation/role
# classification still
# layer locally; roster_policy designation remains a future enrichment.)

# The override fields a Configure row can set. Stored as plain strings; an
# empty/missing value means "no override — fall back to the registry value".
#   harness/model/reasoning : the execution config (R4c).
#   designation             : "interactive" (lead you chat with) or
#                             "autonomous" (worker). When set, it WINS over the
#                             registry-derived Interactive/Autonomous heuristic
#                             (main._is_interactive). Validated on save.
#   role                    : a free-text role label override (the registry role
#                             string is the default; this lets the operator tag,
#                             e.g., a lead without a registry edit).
# DE-FORKED: re-exported from the single owner `app.domain.designation` so this
# facade's helpers + the System save path use the one canonical field set.
AGENT_OVERRIDE_FIELDS = _designation.AGENT_OVERRIDE_FIELDS


def _carry_side_blobs(raw: dict[str, Any], payload: dict[str, Any]) -> None:
    """Re-attach the non-SCHEMA side blobs (per-agent overrides, custom providers,
    seed marker) from `raw` onto `payload` so a System write never drops them.

    normalize()/the SCHEMA only know about the System fields, so anything stored
    OUTSIDE the schema has to be carried across explicitly on every write."""
    blob = raw.get(AGENT_OVERRIDES_KEY)
    if isinstance(blob, dict) and blob:
        payload[AGENT_OVERRIDES_KEY] = blob
    customs = raw.get(CUSTOM_PROVIDERS_KEY)
    if isinstance(customs, list) and customs:
        payload[CUSTOM_PROVIDERS_KEY] = customs
    if raw.get(SEED_MARKER_KEY):
        payload[SEED_MARKER_KEY] = True
    # Provider API keys (PROVIDER_SECRET_KEYS) ALSO live outside the System schema — the
    # Providers surface owns them, normalize() drops them. Without carrying them here, every
    # System/agent-config save (which funnels through _persist_with_overrides → full-replace
    # _atomic_write) WIPES every stored provider key — data-loss: a configured key silently
    # vanishes on the next settings save, and the harness then fails `provider_not_configured`.
    # Carry each non-empty provider secret across exactly like the other side blobs.
    for _pkey in PROVIDER_SECRET_KEYS:
        _pval = raw.get(_pkey)
        if _pval is not None and (not isinstance(_pval, str) or _pval.strip()):
            payload[_pkey] = _pval


def _persist_with_overrides(system_settings: dict[str, Any]) -> None:
    """Write the System settings back to disk WITHOUT clobbering the side blobs
    (per-agent overrides, custom providers) or the one-time seed marker. Reads the
    current raw store, swaps in the new System values, re-attaches the side blobs,
    and writes atomically."""
    raw = _read_raw()
    payload = dict(system_settings)
    _carry_side_blobs(raw, payload)
    _atomic_write(payload)


def _override_store_key(project: str | None, agent: str) -> str:
    """Compose the "{project}:{agent}" storage key (lower-cased, blank-safe).

    DELEGATES to `app.domain.designation.override_store_key` — the single owner of
    this pure helper (de-fork; see the module note above). Behaviour-identical."""
    return _designation.override_store_key(project, agent)


def _clean_override(raw: Any) -> dict[str, str]:
    """Coerce one stored override entry into a clean {field: str} dict, keeping
    only the known AGENT_OVERRIDE_FIELDS with non-empty string values.

    The `designation` field is additionally validated to a known value
    (interactive/autonomous); an unrecognised designation is dropped (→ falls
    back to the registry heuristic). Other fields keep any non-empty string.

    DELEGATES to `app.domain.designation.clean_override` — the single owner of this
    pure helper (de-fork; see the module note above). Behaviour-identical."""
    return _designation.clean_override(raw)


def load_agent_overrides() -> dict[str, dict[str, str]]:
    """Return the whole per-agent override map: {"{project}:{agent}": {harness,
    model, reasoning}}. Tolerates a missing / malformed blob (→ {}). Every entry
    is cleaned to known string fields only."""
    blob = _read_raw().get(AGENT_OVERRIDES_KEY)
    if not isinstance(blob, dict):
        return {}
    out: dict[str, dict[str, str]] = {}
    for key, entry in blob.items():
        cleaned = _clean_override(entry)
        if cleaned:
            out[str(key)] = cleaned
    return out


def get_agent_override(project: str | None, agent: str) -> dict[str, str]:
    """The console-local override for one agent ({} if none). Keys are a subset
    of AGENT_OVERRIDE_FIELDS, all non-empty strings."""
    return load_agent_overrides().get(_override_store_key(project, agent), {})


def get_agent_designation(
    project: str | None, agent: str, overrides: dict[str, dict[str, str]] | None = None
) -> str:
    """The console designation override for one agent: "interactive",
    "autonomous", or "" (no override — caller falls back to the registry
    heuristic). `overrides` is the pre-loaded map from load_agent_overrides();
    pass it to avoid re-reading the store per agent in a loop."""
    src = overrides if overrides is not None else load_agent_overrides()
    entry = src.get(_override_store_key(project, agent), {})
    return normalize_designation(entry.get("designation"))


def save_agent_override(
    project: str | None, agent: str, override: dict[str, Any]
) -> dict[str, str]:
    """Persist (merge) a console-local override for one agent, atomically.

    `override` carries any of harness/model/reasoning/designation/role. A blank
    value CLEARS that field's override (falls back to the registry value); a
    non-blank value sets it (designation is validated to interactive/autonomous).
    If an agent ends up with no overrides at all, its entry is dropped from the
    blob (keeps the store tidy). The System settings + seed marker are preserved.

    CONSOLE-LOCAL BY DESIGN (feature-gap #81, the CTO's reversed decision): this write
    is the fast console-local display/routing path AND the boundary — a config save does
    NOT push to the Cortex registry (the registry, `capabilities` per E006 Inc04, stays
    the source of truth). Committing the config to the registry is the explicit "Promote
    to registry" action via `registry_sync.promote_agent_to_registry` (`POST /agents`
    UPSERT). Returns the agent's post-save effective override dict (which the promote
    endpoint later reads to build its registry payload)."""
    raw = _read_raw()
    blob = raw.get(AGENT_OVERRIDES_KEY)
    blob = dict(blob) if isinstance(blob, dict) else {}

    store_key = _override_store_key(project, agent)
    entry = _clean_override(blob.get(store_key))

    for field in AGENT_OVERRIDE_FIELDS:
        if field not in override:
            continue
        val = override[field]
        if field == "designation":
            # only a known designation persists; "" / junk clears the override
            sval = normalize_designation(val)
        else:
            sval = "" if val is None else str(val).strip()
        if sval:
            entry[field] = sval
        else:
            entry.pop(field, None)  # blank → clear this override

    if entry:
        blob[store_key] = entry
    else:
        blob.pop(store_key, None)  # no overrides left → drop the agent entry

    # Re-attach onto the current SCHEMA values so we never lose System settings.
    payload = dict(load())
    _carry_side_blobs(raw, payload)
    # the override blob we just recomputed wins over the carried-through copy
    if blob:
        payload[AGENT_OVERRIDES_KEY] = blob
    else:
        payload.pop(AGENT_OVERRIDES_KEY, None)
    _atomic_write(payload)
    return entry


# ---------------------------------------------------------------------------
#  Per-project AUTONOMOUS-dispatch toggle — the master kill-switch for the
#  autonomous orchestrator (Cole's loop, E007 Phase 1). This is an OPERATIONAL
#  runtime flag, stored ONLY in the app-DB `project_autonomy` table (no JSON-file
#  fallback): it controls whether the background loop may auto-run agents, which
#  must be durable + single-sourced, not split across a fallback file.
#
#  SHIP-DARK / FAIL-SAFE contract: every read defaults to OFF. A project with no
#  row is OFF; if the app-DB is UNAVAILABLE the read is ALSO OFF and the
#  reconcile list is EMPTY. So the loop can never enable autonomy it can't read,
#  and an app-DB outage can only ever turn autonomy OFF, never on.
# ---------------------------------------------------------------------------

def is_project_autonomous(project: str | None) -> bool:
    """True only if autonomous dispatch is explicitly ON for this project. No row,
    a blank project, OR an unreachable app-DB → False (fail-safe OFF)."""
    key = (project or "").strip().lower()
    if not key:
        return False
    state = _db.get_project_autonomy(key)
    if state is _UNAVAILABLE:
        return False  # fail-safe: a degraded DB is treated as OFF
    return bool(state)


def set_project_autonomous(
    project: str | None, enabled: bool, updated_by: str | None = None
) -> bool:
    """Flip the autonomous-dispatch switch for one project. Returns True on a
    successful persist, False if the app-DB could not be written (blank project
    is rejected as False). The caller re-reads is_project_autonomous to render
    the authoritative post-write state."""
    key = (project or "").strip().lower()
    if not key:
        return False
    return _db.set_project_autonomy(key, bool(enabled), updated_by)


def autonomous_projects() -> list[str]:
    """The set of projects with autonomy ON, lower-cased. Empty when none are on
    OR the app-DB is unreachable (fail-safe: the loop idles). The orchestrator
    reconciles its active set against this."""
    state = _db.list_autonomous_projects()
    if state is _UNAVAILABLE:
        return []  # fail-safe: degraded DB → no autonomous projects → loop idle
    return list(state)


# ---------------------------------------------------------------------------
#  Per-project PROPOSE-MODE gate (PM Relentless Beat, Inc 1) — the
#  training-wheels safety switch. When ON, Dispatch parks each ready handoff
#  as "awaiting approval" (writes a `pending_approval` row) instead of
#  auto-spawning it. The human operator clicks Approve in the Dispatch view to
#  clear the record; the next sweep then spawns normally.
#
#  FAIL-SAFE contract: every read defaults to False (gate OFF = auto-spawn).
#  A project with no row is OFF; a degraded app-DB also reads OFF. So an
#  outage can never accidentally BLOCK a dispatch — it can only degrade to the
#  existing auto-spawn behaviour. Default False means existing (autonomous)
#  projects are completely unaffected.
# ---------------------------------------------------------------------------

def is_propose_mode(project: str | None) -> bool:
    """True only if propose-mode is explicitly ON for this project. No row,
    a blank project, OR an unreachable app-DB → False (fail-safe: auto-spawn,
    existing behaviour unchanged)."""
    key = (project or "").strip().lower()
    if not key:
        return False
    state = _db.get_project_propose_mode(key)
    if state is _UNAVAILABLE:
        return False  # fail-safe: a degraded DB is treated as OFF
    return bool(state)


def is_propose_mode_gate(project: str | None) -> bool:
    """Propose-mode for the AUTONOMOUS-dispatch gate — the FAIL-CLOSED variant.

    An explicit ON gates; an explicit OFF auto-spawns; but an UNREADABLE state (app-DB
    UNAVAILABLE) GATES (returns True), so the autonomous loop never auto-spawns
    unapproved work it couldn't confirm was un-gated on a DB hiccup. The interactive
    `is_propose_mode` above stays fail-safe-OFF (a user's explicit dispatch shouldn't be
    blocked by a transient read), but autonomous dispatch must err toward holding."""
    key = (project or "").strip().lower()
    if not key:
        return False
    state = _db.get_project_propose_mode(key)
    if state is _UNAVAILABLE:
        return True  # fail-CLOSED: can't confirm un-gated → require approval
    return bool(state)


def set_propose_mode(
    project: str | None, enabled: bool, updated_by: str | None = None
) -> bool:
    """Flip the propose-mode gate for one project. Returns True on a successful
    persist, False if the app-DB could not be written (blank project is
    rejected as False). The caller re-reads is_propose_mode to render the
    authoritative post-write state."""
    key = (project or "").strip().lower()
    if not key:
        return False
    return _db.set_project_propose_mode(key, bool(enabled), updated_by)


# ---------------------------------------------------------------------------
#  Awaiting-approval helpers (propose-mode pending_approval table) — the
#  per-handoff park/release record written by Dispatch when propose_mode is ON
#  and cleared by the approve route. Exposed here (alongside is_propose_mode)
#  so orchestrator.py and main.py share a single clean accessor surface.
# ---------------------------------------------------------------------------

def set_awaiting_approval(project: str | None, handoff_id: str) -> bool:
    """Park a handoff as awaiting human approval (UPSERT; safe to call twice).
    Returns True on success, False if the app-DB can't be written."""
    key = (project or "").strip().lower()
    hid = (handoff_id or "").strip()
    if not key or not hid:
        return False
    return _db.set_awaiting_approval(key, hid)


def clear_awaiting_approval(project: str | None, handoff_id: str) -> bool:
    """Clear the awaiting-approval record for one handoff (the approve action).
    Idempotent: a no-op when no record exists. Returns True on success
    (including the no-op case), False if the DB can't answer."""
    key = (project or "").strip().lower()
    hid = (handoff_id or "").strip()
    if not key or not hid:
        return False
    return _db.clear_awaiting_approval(key, hid)


def is_awaiting_approval(project: str | None, handoff_id: str) -> bool:
    """True if this handoff is currently parked awaiting approval. Falls back
    to False when the app-DB is unreachable (fail-safe: allows the next sweep
    to re-attempt dispatch rather than stranding the handoff)."""
    key = (project or "").strip().lower()
    hid = (handoff_id or "").strip()
    if not key or not hid:
        return False
    state = _db.is_awaiting_approval(key, hid)
    if state is _UNAVAILABLE:
        return False  # fail-safe: treat as not gated
    return bool(state)


def list_awaiting_approval(project: str | None) -> list[str]:
    """Handoff IDs currently awaiting approval (status='awaiting') for a project,
    oldest-first. Returns [] when none are pending OR the app-DB is unreachable.

    NOTE (operator/deploy): when the app-DB is unreachable this returns [] —
    the Approve queue in the Dispatch view goes empty and Approve buttons
    disappear. A DB hiccup also makes is_propose_mode() read False (auto-spawn).
    Bring the app-DB back up to restore the gate; monitor 'app-DB settings
    unreachable' log lines."""
    key = (project or "").strip().lower()
    if not key:
        return []
    state = _db.list_awaiting_approval(key)
    if state is _UNAVAILABLE:
        return []
    return list(state)


def get_approval_status(
    project: str | None, handoff_id: str
) -> str | None:
    """Return the approval status for one handoff: 'awaiting', 'approved', or
    None (no row — handoff has not been parked yet).

    Returns None when the app-DB is unreachable (fail-safe: the gate will retry
    on the next sweep rather than assuming 'awaiting' and stranding the handoff)."""
    key = (project or "").strip().lower()
    hid = (handoff_id or "").strip()
    if not key or not hid:
        return None
    state = _db.get_approval_status(key, hid)
    if state is _UNAVAILABLE:
        return None  # fail-safe: treat as not yet seen → gate retries next sweep
    return state  # 'awaiting', 'approved', or None


def set_approval_status(
    project: str | None, handoff_id: str, status: str
) -> bool:
    """Persist the approval status ('awaiting' or 'approved') for one handoff.
    UPSERT — safe to call multiple times (idempotent). Returns True on a
    successful persist, False if the app-DB can't be written (blank project or
    handoff_id is rejected as False)."""
    key = (project or "").strip().lower()
    hid = (handoff_id or "").strip()
    if not key or not hid:
        return False
    return _db.set_approval_status(key, hid, str(status))


# ---------------------------------------------------------------------------
#  Custom providers — operator-added provider credentials (name + base URL +
#  masked API key). The fixed provider-key fields in SCHEMA are built-ins; this
#  is the open-ended set the System page's "+ Add provider" affordance manages.
#  Stored as a list under CUSTOM_PROVIDERS_KEY, OUTSIDE the schema, and preserved
#  across every System save (see _carry_side_blobs).
#
# Each stored entry:  {id, name, base_url, api_key}
#   id       : short stable slug used to address the row for removal
#   name     : operator-facing provider name (required)
#   base_url : provider base URL (optional but expected)
#   api_key  : the secret — kept in the app-DB settings store (or the gitignored
#              fallback file only when degraded), masked in the UI exactly like
#              the built-in secret fields (never echoed).
# ---------------------------------------------------------------------------

# Bounds keep a hand-edited or pasted value from bloating the store / the page.
_CP_NAME_MAX = 80
_CP_URL_MAX = 300
_CP_KEY_MAX = 400


def _slugify_provider(name: str) -> str:
    """Lower-case alnum/dash slug from a provider name (fallback: "provider")."""
    out = []
    for ch in (name or "").strip().lower():
        if ch.isalnum():
            out.append(ch)
        elif ch in (" ", "-", "_", ".", "/"):
            out.append("-")
    slug = "".join(out).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug[:48] or "provider"


def _clean_custom_provider(raw: Any) -> dict[str, str] | None:
    """Coerce one stored custom-provider entry into a clean dict, or None if it
    has no usable name. Tolerant by design (a hand-edited file can never 500)."""
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("name") or "").strip()[:_CP_NAME_MAX]
    if not name:
        return None
    pid = str(raw.get("id") or "").strip() or _slugify_provider(name)
    return {
        "id": pid,
        "name": name,
        "base_url": str(raw.get("base_url") or "").strip()[:_CP_URL_MAX],
        "api_key": str(raw.get("api_key") or "").strip()[:_CP_KEY_MAX],
    }


def load_custom_providers() -> list[dict[str, str]]:
    """Return the stored custom providers as a clean list (drops malformed /
    nameless entries). Tolerates a missing / non-list blob (→ [])."""
    blob = _read_raw().get(CUSTOM_PROVIDERS_KEY)
    if not isinstance(blob, list):
        return []
    out: list[dict[str, str]] = []
    for entry in blob:
        cleaned = _clean_custom_provider(entry)
        if cleaned:
            out.append(cleaned)
    return out


def _unique_provider_id(base: str, existing: set[str]) -> str:
    """Return `base`, or base-2/base-3/… if it collides with an existing id."""
    if base not in existing:
        return base
    n = 2
    while f"{base}-{n}" in existing:
        n += 1
    return f"{base}-{n}"


def add_custom_provider(name: str, base_url: str, api_key: str) -> dict[str, str]:
    """Append a custom provider (name + base URL + API key) and persist atomically.

    `name` is required (a blank name is a no-op that raises ValueError so the route
    can surface it). The id is a unique slug of the name. Reuses the same atomic
    write + side-blob preservation as every other store write. Returns the stored
    (cleaned) entry."""
    entry = _clean_custom_provider(
        {"name": name, "base_url": base_url, "api_key": api_key}
    )
    if entry is None:
        raise ValueError("a provider name is required")

    raw = _read_raw()
    current = load_custom_providers()
    entry["id"] = _unique_provider_id(entry["id"], {c["id"] for c in current})
    current.append(entry)

    payload = dict(load())  # the current SCHEMA System values
    _carry_side_blobs(raw, payload)
    payload[CUSTOM_PROVIDERS_KEY] = current  # the recomputed list wins
    _atomic_write(payload)
    return entry


def remove_custom_provider(provider_id: str) -> bool:
    """Remove the custom provider with `provider_id`. Returns True if one was
    removed. Persists atomically (side blobs preserved). A no-op (returns False)
    for an unknown / blank id — never raises."""
    pid = (provider_id or "").strip()
    if not pid:
        return False
    raw = _read_raw()
    current = load_custom_providers()
    kept = [c for c in current if c["id"] != pid]
    if len(kept) == len(current):
        return False  # nothing matched

    payload = dict(load())
    _carry_side_blobs(raw, payload)
    if kept:
        payload[CUSTOM_PROVIDERS_KEY] = kept
    else:
        payload.pop(CUSTOM_PROVIDERS_KEY, None)  # last one removed → drop the key
    _atomic_write(payload)
    return True


def view_custom_providers() -> list[dict[str, Any]]:
    """Render-ready custom-provider list for the System page. The raw api_key is
    NEVER placed in the output — only `has_key` (bool) + a masked display string,
    mirroring the built-in secret fields so a key can't leak into the HTML."""
    out: list[dict[str, Any]] = []
    for c in load_custom_providers():
        has_key = bool(c.get("api_key"))
        out.append(
            {
                "id": c["id"],
                "name": c["name"],
                "base_url": c.get("base_url", ""),
                "has_key": has_key,
                "key_display": MASK_PLACEHOLDER if has_key else "",
            }
        )
    return out


# ---------------------------------------------------------------------------
#  View model — schema + current values shaped for the template (no UI logic)
# ---------------------------------------------------------------------------

def view_groups(values: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Build the render-ready group/field list for the System page.

    Each field is augmented with `value` (the current stored value) and, for
    secrets, `has_value` (whether a secret is set) + a masked display string —
    the raw secret is NEVER placed in `display`/`value` for a secret field, only
    the boolean + placeholder, so it can't leak into the HTML. The template just
    iterates; it makes no decisions."""
    vals = normalize(values) if values is not None else load()
    groups: list[dict[str, Any]] = []
    for g in SCHEMA:
        fields_out: list[dict[str, Any]] = []
        secret_count = 0
        for f in g["fields"]:
            key = f["key"]
            ftype = f.get("type", "text")
            entry: dict[str, Any] = {
                "key": key,
                "label": f["label"],
                "type": ftype,
                "hint": f.get("hint"),
                "placeholder": f.get("placeholder", ""),
            }
            if ftype == "secret":
                secret_count += 1
                has = bool(str(vals.get(key) or "").strip())
                entry["has_value"] = has
                # masked display only — never the real secret
                entry["display"] = MASK_PLACEHOLDER if has else ""
                entry["value"] = ""  # secrets never seed the input value
            elif ftype == "bool":
                entry["value"] = bool(vals.get(key))
            else:
                entry["value"] = vals.get(key)
            fields_out.append(entry)
        groups.append(
            {
                "id": g["id"],
                "title": g["title"],
                "sub": g["sub"],
                "icon": g["icon"],
                "open": bool(g.get("open")),
                "secret_count": secret_count,
                "field_count": len(g["fields"]),
                "fields": fields_out,
            }
        )
    return groups
