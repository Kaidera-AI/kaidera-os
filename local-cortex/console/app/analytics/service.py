"""Analytics feature logic — usage + est.-cost rollups over the operational store.

The functional core of the `analytics` module: it computes the Analytics view's
substance (usage + estimated cost, broken down by model / model×provider / per
agent / cost-by-agent / cost-by-project) from the pre-aggregated `usage_events`
rollups the operational store exposes.

LAYER RULE (arrows point inward, ratified design §3): this module depends ONLY on
`domain.ports.OperationalStorePort` (the abstraction over the App-DB usage data) —
it imports NOTHING outward (no fastapi / httpx / subprocess / psycopg2 / asyncpg)
and never reaches back into `app.main`, the concrete `app.appdb`, or `app.adapters`.
The two presentation formatters it needs (an upstream-provider label + a cost
formatter) are INJECTED as plain callables (pure functions), so the service stays
free of the concrete `providers` module while still rendering the same labels —
the shell (`api.py`) / `main.py` pass the real `providers.provider_label` +
`providers.fmt_cost` when wiring; the defaults below keep it self-contained for
tests.

The logic is lifted 1:1 from `main._analytics_usage_cost` (+ its `_bar_rows`,
`_agent_display_map`, `_format_tokens` helpers) so the carve is behaviour-
preserving — `main.py` now delegates its usage/cost substance here, making this the
single source of that logic.

Graceful-degrade is the house law: when the store reports `available() == False`
(or returns empty rollups) every breakdown is empty and the result carries the
'store not connected' / 'no usage recorded yet' state — it never raises.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from app.domain.ports import OperationalStorePort

# Cap on per-agent / per-model / per-provider rows shown in the breakdowns
# (lifted from main._ANALYTICS_AGENT_MAX).
AGENT_MAX = 8


# ---------------------------------------------------------------------------
#  Pure formatters — defaults so the service is self-contained (no concrete dep)
# ---------------------------------------------------------------------------


def format_tokens(n: int) -> str:
    """Human token count: 1_240_000 -> '1.24M', 94_592 -> '94.6k' (lifted 1:1
    from `main._format_tokens`)."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _default_provider_label(provider: Optional[str]) -> str:
    """Fallback upstream-provider label when no concrete labeller is injected
    (mirrors `providers.provider_label`'s shape: blank → '—', else title-cased)."""
    if not provider:
        return "—"
    return provider.replace("-", " ").title()


def _default_fmt_cost(v: Optional[float]) -> str:
    """Fallback USD cost formatter when none is injected (mirrors
    `providers.fmt_cost`'s '$x.xx' shape)."""
    if v is None:
        return "n/a"
    return f"${v:,.2f}"


# ---------------------------------------------------------------------------
#  Shaping helpers (lifted from main.py — pure, no I/O)
# ---------------------------------------------------------------------------


def _bar_rows(pairs: list[tuple[str, int]], cap: int) -> list[dict]:
    """Shape (label, value) pairs into proportional bar rows for the templates.

    Sorted desc by value, capped, each row carrying a 0–100 `pct` relative to the
    largest value (so the top bar fills the track). Empty input → []. Lifted 1:1
    from `main._bar_rows`."""
    rows = [(lbl, val) for lbl, val in pairs if isinstance(val, int) and val > 0]
    rows.sort(key=lambda p: p[1], reverse=True)
    rows = rows[:cap]
    if not rows:
        return []
    top = rows[0][1] or 1
    return [
        {
            "label": lbl,
            "value": val,
            "value_h": format_tokens(val),
            "pct": round(val / top * 100),
        }
        for lbl, val in rows
    ]


def _agent_display_map(agents: list[dict]) -> dict[str, str]:
    """name(lower) -> display_name, for labelling App-DB usage rows (which store
    the bare agent name). Falls back to the stored name when no roster match.
    Lifted 1:1 from `main._agent_display_map`."""
    out: dict[str, str] = {}
    for a in agents:
        name = (a.get("name") or "").strip()
        if not name:
            continue
        display = (a.get("capabilities") or {}).get("display_name") or name
        out[name.lower()] = display
    return out


# ---------------------------------------------------------------------------
#  The service
# ---------------------------------------------------------------------------


class AnalyticsService:
    """Compute the Analytics view's usage + est.-cost breakdowns over an
    `OperationalStorePort`.

    Construct with the port (the operational data source); the two presentation
    formatters default to self-contained pure functions and can be overridden with
    the concrete `providers.provider_label` / `providers.fmt_cost` at the shell so
    the labels match the rest of the UI exactly."""

    def __init__(
        self,
        *,
        store: Optional[OperationalStorePort] = None,
        provider_label: Callable[[Optional[str]], str] = _default_provider_label,
        fmt_cost: Callable[[Optional[float]], str] = _default_fmt_cost,
    ) -> None:
        # `store` is optional so callers that ALREADY hold the pre-aggregated rows
        # (e.g. main.py's HTML view, which fetches them concurrently alongside the
        # Cortex KPIs) can call `shape_usage_cost(...)` directly without a store;
        # `usage_cost(...)` requires the store (it does the fetch itself).
        self._store = store
        self._provider_label = provider_label
        self._fmt_cost = fmt_cost

    async def usage_cost(
        self, project: str, *, agents: Optional[list[dict]] = None
    ) -> dict[str, Any]:
        """Fetch the project's pre-aggregated usage rollups from the store and
        shape them into the usage + est.-cost view payload.

        `agents` is the roster (for the display-name map only). Reads the four
        usage rollups + the store-liveness flag from the port; returns the shaped
        dict (`shape_usage_cost`'s output). Never raises — a down/empty store
        yields the graceful empty state. Requires a `store` (set at construction)."""
        if self._store is None:
            raise ValueError(
                "AnalyticsService.usage_cost requires a store; construct with "
                "store=<OperationalStorePort> or call shape_usage_cost(...) with rows."
            )
        by_model = await self._store.usage_by_model(project)
        by_model_provider = await self._store.usage_by_model_provider(project)
        by_agent = await self._store.usage_by_agent(project)
        by_project = await self._store.usage_by_project(project)
        store_connected = bool(self._store.available())
        return self.shape_usage_cost(
            agents or [],
            by_model,
            by_model_provider,
            by_agent,
            by_project,
            store_connected,
        )

    def shape_usage_cost(
        self,
        agents: list[dict],
        by_model: list[dict],
        by_model_provider: list[dict],
        by_agent: list[dict],
        by_project: dict,
        store_connected: bool,
    ) -> dict[str, Any]:
        """The substance of the Analytics view: usage + est. cost.

        Reads the pre-aggregated App-DB `usage_events` rollups and shapes them into
          1. total usage BY MODEL          (bar + table)
          2. usage BY MODEL × PROVIDER     (grouped under each provider)
          3. MODEL USAGE PER AGENT         (agent · model · tokens table)
          4. est. API COST BY AGENT        (stored cost; 'n/a' when none)
          5. est. API COST BY PROJECT      (Σ stored cost)

        `agents` supplies the display-name map only (the roster). When the store is
        down/empty every list is empty and the view shows the graceful empty state
        — never a 500. Lifted 1:1 from `main._analytics_usage_cost`."""
        label = self._provider_label
        fmt_cost = self._fmt_cost
        display_map = _agent_display_map(agents)

        # ---- 1. total usage BY MODEL (bar rows + a small table) -------------
        model_pairs = [
            ((r.get("model") or "—"), r.get("tokens") or 0)
            for r in by_model
            if (r.get("tokens") or 0) > 0
        ]
        by_model_bars = _bar_rows(model_pairs, AGENT_MAX)
        by_model_table = [
            {
                "model": r.get("model") or "—",
                "provider": label(r.get("provider")),
                "tokens": r.get("tokens") or 0,
                "tokens_h": format_tokens(r.get("tokens") or 0),
            }
            for r in by_model
            if (r.get("tokens") or 0) > 0
        ]

        # ---- 2. usage BY MODEL × PROVIDER (provider totals + their models) --
        provider_tokens: dict[str, int] = {}
        provider_models: dict[str, list[dict]] = {}
        for r in by_model_provider:
            tok = r.get("tokens") or 0
            if tok <= 0:
                continue
            prov = r.get("provider") or "other"
            provider_tokens[prov] = provider_tokens.get(prov, 0) + tok
            provider_models.setdefault(prov, []).append(
                {"model": r.get("model") or "—", "tokens": tok, "tokens_h": format_tokens(tok)}
            )
        by_provider: list[dict] = []
        for prov, ptot in sorted(
            provider_tokens.items(), key=lambda kv: kv[1], reverse=True
        ):
            models = sorted(
                provider_models.get(prov, []), key=lambda m: m["tokens"], reverse=True
            )
            by_provider.append(
                {
                    "provider": prov,
                    "label": label(prov) if prov != "other" else "Other / unknown",
                    "tokens": ptot,
                    "tokens_h": format_tokens(ptot),
                    "models": models,
                }
            )
        by_provider_bars = _bar_rows(
            [
                (label(p) if p != "other" else "Other / unknown", t)
                for p, t in provider_tokens.items()
            ],
            AGENT_MAX,
        )

        # ---- 3 + 4. per-agent usage + est. cost rows -----------------------
        per_agent: list[dict] = []
        agents_with_usage = 0
        for r in by_agent:
            name = r.get("agent") or "—"
            tokens = r.get("tokens") or 0
            cost = r.get("cost")
            if isinstance(cost, (int, float)) and cost <= 0:
                cost = None  # a recorded-but-zero cost reads as "no cost" in the UI
            model_id = r.get("model")
            provider = r.get("provider")
            if tokens > 0:
                agents_with_usage += 1
            per_agent.append(
                {
                    "agent": name,
                    "display": display_map.get(name.lower(), name),
                    "model": model_id,
                    "model_known": bool(model_id),
                    "provider": label(provider) if provider else None,
                    "tokens": tokens or None,
                    "tokens_h": format_tokens(tokens) if tokens else None,
                    "input": r.get("tokens_in"),
                    "output": r.get("tokens_out"),
                    "priced": cost is not None,
                    # per-Mtok rate columns are not meaningful per-run in the App-DB
                    # model (cost is stored absolute), so the rate cells show '—'.
                    "price_in_h": "—",
                    "price_out_h": "—",
                    "cost": cost,
                    "cost_h": fmt_cost(cost) if cost is not None else "n/a",
                    "cost_na_reason": (
                        None if cost is not None
                        else ("no usage data" if not tokens else "cost not recorded")
                    ),
                }
            )

        cost_rows = sorted(
            per_agent,
            key=lambda r: (r["cost"] if r["cost"] is not None else -1),
            reverse=True,
        )

        total_tokens = by_project.get("tokens") or 0
        project_cost_raw = by_project.get("cost")
        project_cost = (
            project_cost_raw if (project_cost_raw and project_cost_raw > 0) else None
        )

        return {
            "rows": per_agent,                     # model-usage-per-agent table
            "agent_count": len(per_agent),
            "agents_with_usage": agents_with_usage,
            "total_tokens": total_tokens,
            "total_tokens_h": format_tokens(total_tokens) if total_tokens else None,
            "by_model_bars": by_model_bars,
            "by_model_table": by_model_table,
            "model_count": len(by_model_table),
            "by_provider": by_provider,
            "by_provider_bars": by_provider_bars,
            "provider_count": len(by_provider),
            "cost_rows": cost_rows,
            "project_cost": project_cost,
            "project_cost_h": fmt_cost(project_cost) if project_cost is not None else "n/a",
            "priced_agent_count": sum(1 for r in per_agent if r["cost"] is not None),
            # store liveness — the template shows a 'usage store not connected' note
            # when False, and a 'no usage recorded yet' empty state when connected-
            # but-empty (total_runs == 0).
            "store_connected": store_connected,
            "total_runs": by_project.get("runs") or 0,
        }


__all__ = ["AnalyticsService", "AGENT_MAX", "format_tokens"]
