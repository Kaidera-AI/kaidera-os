"""ModelCatalogPort adapter — wraps the `providers` catalog functions.

The imperative-shell adapter (`app/adapters/`) that IMPLEMENTS the pure
`ModelCatalogPort` Protocol (`app/domain/ports.py`) over the EXISTING `providers`
module. Arrows point inward (ratified design §3): the domain port stays pure; this
adapter is the boundary that fetches the live provider/model lists.

THIN + ADDITIVE: it does NOT reimplement any fetch/merge/cache/pricing logic — it
delegates to the existing `providers` functions and maps their dict shapes onto the
port's typed DTOs:
  * `list_models()` → `providers.get_catalog()` then `providers.view_catalog()`,
    flattening every group's render rows into `CatalogModel`s (the raw catalog row
    carries `id/display_name/type`; the view row carries the same plus the
    formatted strings — we read the catalog `groups[].models[]` for the full
    numeric fields and fall back to the view rows).
  * `price_for(id)` → `providers.pricing_index()` + `providers.resolve_model()`,
    mapped onto `ModelPrice`.
Both delegate to functions that NEVER raise (a network failure degrades to the
cached/empty catalog), so the adapter inherits that graceful-degrade contract.
"""

from __future__ import annotations

from typing import Any, Optional

from app import providers
from app.domain.ports import CatalogModel, ModelPrice


def _model_from_catalog_row(row: dict[str, Any]) -> CatalogModel:
    """Map ONE raw catalog model dict (`providers._model(...)` shape) → a
    `CatalogModel` DTO. The raw row carries the full numeric fields (prices,
    windows, reasoning levels), so this is the richest source."""
    return CatalogModel(
        provider=row.get("provider") or "",
        id=row.get("id") or "",
        display_name=row.get("display_name") or row.get("id") or "",
        type=row.get("type") or "chat",
        context_window=row.get("context_window"),
        max_output=row.get("max_output"),
        reasoning_levels=list(row.get("reasoning_levels") or []),
        price_in_per_mtok=row.get("price_in_per_mtok"),
        price_out_per_mtok=row.get("price_out_per_mtok"),
        source=row.get("source") or "live",
    )


def _model_from_view_row(provider: str, row: dict[str, Any]) -> CatalogModel:
    """Map ONE render-ready view row (`providers._row_view(...)` shape) → a
    `CatalogModel`. Used when only the view is available (the view carries
    formatted strings, not the raw numerics, so prices/windows stay None here)."""
    return CatalogModel(
        provider=provider,
        id=row.get("id") or "",
        display_name=row.get("display_name") or row.get("id") or "",
        type=row.get("type") or "chat",
        reasoning_levels=[] if not row.get("has_reasoning") else ["supported"],
        source=row.get("source") or "live",
    )


class ProvidersModelCatalog:
    """`ModelCatalogPort` over the `providers` module (thin pass-through).
    Satisfies the `ModelCatalogPort` Protocol structurally."""

    async def list_models(self) -> list[CatalogModel]:
        """The flattened catalog as typed `CatalogModel`s. Prefers the raw catalog
        `groups[].models[]` (full numeric fields); falls back to the view rows."""
        catalog = await providers.get_catalog()

        # The raw catalog carries the richest per-model fields (numeric prices /
        # windows / reasoning levels). Use it as the primary source.
        raw_groups = catalog.get("groups") if isinstance(catalog, dict) else None
        out: list[CatalogModel] = []
        if raw_groups:
            for group in raw_groups:
                for row in group.get("models", []) or []:
                    out.append(_model_from_catalog_row(row))
            if out:
                return out

        # Fall back to the render view (formatted) if the raw groups were empty.
        view = providers.view_catalog(catalog)
        for group in view.get("groups", []) or []:
            provider = group.get("provider") or ""
            for row in group.get("rows", []) or []:
                out.append(_model_from_view_row(provider, row))
        return out

    async def price_for(self, model_id: Optional[str]) -> ModelPrice:
        """Resolve a model id → its upstream provider + per-Mtok pricing via the
        existing `pricing_index` + `resolve_model`. Never raises."""
        try:
            catalog = await providers.get_catalog()
            index = providers.pricing_index(catalog)
        except Exception:
            index = {}
        resolved = providers.resolve_model(model_id, index)
        return ModelPrice(
            model_id=resolved.get("model_id"),
            provider=resolved.get("provider"),
            provider_label=resolved.get("provider_label") or "—",
            price_in_per_mtok=resolved.get("price_in_per_mtok"),
            price_out_per_mtok=resolved.get("price_out_per_mtok"),
            resolved=bool(resolved.get("resolved")),
            priced=bool(resolved.get("priced")),
        )


__all__ = ["ProvidersModelCatalog"]
