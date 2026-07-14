"""E012 W2 — comprehensive Ed25519 issue -> validate -> enforce proof.

ONE real Ed25519 platform grant, flowing through EVERY enforcement gate in the PUBLIC
edition: license status + entitlements, the harness gate (harness.visible_harness_order),
the capacity gate (registration._capacity_block for workers + projects), and the provider
edition/runtime gates (visibility is edition-only; Manifold credentials require its signed atom). This is the
end-to-end teeth that the per-gate unit tests cover only in pieces / on the free-tier path.
"""
import asyncio

from app import edition
from app import harness
from app import license as lic
from app import providers
from app import registration_api as reg


class _FakeCortex:
    def __init__(self, roster=None, projects=None):
        self._roster = roster or []
        self._projects = projects or []

    async def get_roster(self, project):
        return self._roster

    async def get_projects(self):
        return self._projects


def test_w2_ed25519_grant_enforces_across_all_gates(monkeypatch, ed25519_public_license):
    monkeypatch.setenv("KAIDERA_OS_EDITION", "public")
    assert edition.is_public()

    # ISSUE — a real Ed25519 platform grant (the only signature that verifies in public)
    ed25519_public_license(
        "DXB", days=365,
        features=["harness:claude-code", "projects:2", "workers:10", "kaidera_os_max_users:3", "manifold_access"],
    )

    # VALIDATE — status + entitlements
    ent = lic.entitlements()
    assert ent.valid and ent.customer == "DXB"
    assert ent.limit_for("projects") == 2      # granted 2 > free floor 1
    assert ent.limit_for("workers") == 10      # granted 10 > free floor 4
    assert ent.limit_for("users") == 3         # granted 3 > free floor 1
    assert ent.has_advanced("manifold_access")
    assert ent.has_harness("claude-code") and ent.has_harness("kaidera")
    assert not ent.has_harness("codex")  # un-granted

    # ENFORCE 1 — harness gate: granted + always-free visible; un-granted hidden
    vis = harness.visible_harness_order()
    assert "claude-code" in vis and "kaidera" in vis
    assert "codex" not in vis

    # ENFORCE 2 — capacity gate (workers): allowed under the limit, blocked AT it
    nine = [{"name": f"w{i}"} for i in range(9)]
    ten = [{"name": f"w{i}"} for i in range(10)]
    assert asyncio.run(reg._capacity_block("workers", "p", _FakeCortex(roster=nine))) is None
    blocked_w = asyncio.run(reg._capacity_block("workers", "p", _FakeCortex(roster=ten)))
    assert blocked_w and "Worker limit" in blocked_w
    # an UPSERT of an existing worker is never blocked even at the limit
    assert asyncio.run(reg._capacity_block("workers", "p", _FakeCortex(roster=ten), subject="w0")) is None

    # ENFORCE 3 — capacity gate (projects): allowed under, blocked at the limit
    one = [{"project_key": "a"}]
    two = [{"project_key": "a"}, {"project_key": "b"}]
    assert asyncio.run(reg._capacity_block("projects", "p", _FakeCortex(projects=one))) is None
    blocked_p = asyncio.run(reg._capacity_block("projects", "p", _FakeCortex(projects=two)))
    assert blocked_p and "Project limit" in blocked_p

    # ENFORCE 4 — PUBLIC visibility remains Manifold-only; the signed atom authorizes
    # resolution of that already-visible provider and cannot expose another provider.
    assert providers.visible_providers() == ["kaidera-manifold"]
    assert providers._resolve_provider_key({"kaidera_manifold_api_key": "mf"}, "kaidera_manifold_api_key") == "mf"


def test_w2_no_grant_clamps_to_free_tier_floor(monkeypatch):
    """Contrast (the enforce-DOWN side): without a grant the same gates clamp to the floor."""
    monkeypatch.setenv("KAIDERA_OS_EDITION", "public")
    monkeypatch.delenv("KAIDERA_OS_LICENSE_KEY", raising=False)

    ent = lic.entitlements()
    assert ent.valid is False
    assert "claude-code" not in harness.visible_harness_order()   # locked without a grant
    assert "kaidera" in harness.visible_harness_order()           # kaidera still free
    # capacity clamps to the free floor: workers=4, projects=1
    four = [{"name": f"w{i}"} for i in range(4)]
    assert asyncio.run(reg._capacity_block("workers", "p", _FakeCortex(roster=four)))   # blocked at 4
    assert asyncio.run(reg._capacity_block("projects", "p", _FakeCortex(projects=[{"project_key": "a"}])))
    assert providers.visible_providers() == ["kaidera-manifold"]
    assert providers._resolve_provider_key({"kaidera_manifold_api_key": "mf"}, "kaidera_manifold_api_key") == ""
