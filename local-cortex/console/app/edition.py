"""Edition flag — ``public`` (the redistributable AND the open-source build) vs
``dev`` (our own unrestricted local dogfood).

This is AXIS 2, orthogonal to ``deploy_mode`` (AXIS 1, host-file conveniences). It
owns ONE thing: the **provider lockdown**. In the PUBLIC edition the console exposes
only the Kaidera AI Manifold provider; DEV exposes the full provider catalog.

WHY a separate module and NOT a license feature (the whole point):
  A restriction a license can TOGGLE is a restriction a license can UN-toggle. The
  product rule is "providers are restricted ONLY programmatically" — so the provider
  gate must be structurally impossible to reach through any license/features branch.
  This module therefore MUST NOT import ``app.license`` (enforced by the fitness gate
  ``scripts/fitness/check-edition-not-license-gated.sh``). Harness unlocks + capacity
  caps are the LICENSE's job (``app.license.entitlements``); providers are NOT.

RESOLUTION (first match wins):
  1. The baked build constant ``_BAKED_EDITION`` — the public packaging step
     (install.sh / wheel build / redistributable tarball) rewrites it to ``"public"``.
  2. The env var ``KAIDERA_OS_EDITION`` (``public`` / ``dev``) — lets an OSS build or a
     test self-identify without rebuilding.
  3. Fallback: ``deploy_mode.is_selfcontained()`` ⇒ PUBLIC (the distributable is
     PUBLIC with zero extra wiring), else DEV.

Default is **DEV** — a plain local checkout is unrestricted, "kept as is". Only an
explicit signal (baked constant, env, or selfcontained deploy) flips it to PUBLIC, so
the dogfood can never be accidentally locked down.
"""

from __future__ import annotations

import os

PUBLIC = "public"
DEV = "dev"

#: The env var that selects the edition. Unset / unrecognised → fall through to deploy_mode.
ENV_VAR = "KAIDERA_OS_EDITION"

#: Baked at package time by the PUBLIC build. ``None`` in a source checkout. The public
#: packaging step rewrites this line to ``_BAKED_EDITION = "public"`` (hard build fact —
#: stronger than an env var an operator could flip back to dev to unlock everything).
_BAKED_EDITION: str | None = None
def edition() -> str:
    """Return the resolved edition: ``public`` or ``dev``. First match wins:
    baked constant → ``KAIDERA_OS_EDITION`` env → ``selfcontained`` deploy → DEV."""
    if _BAKED_EDITION in (PUBLIC, DEV):
        return _BAKED_EDITION  # type: ignore[return-value]
    raw = (os.environ.get(ENV_VAR, "") or "").strip().lower()
    if raw == PUBLIC:
        return PUBLIC
    if raw == DEV:
        return DEV
    try:
        from app import deploy_mode
        if deploy_mode.is_selfcontained():
            return PUBLIC
    except Exception:
        pass
    return DEV


def is_public() -> bool:
    """True for the redistributable AND the open-source build — providers locked to
    Manifold, harnesses/capacity license-gated."""
    return edition() == PUBLIC


def is_dev() -> bool:
    """True for our own local dogfood — fully unrestricted, kept as is."""
    return edition() == DEV


__all__ = ["PUBLIC", "DEV", "ENV_VAR", "edition", "is_public", "is_dev"]
