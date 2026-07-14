from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
API_MAIN = ROOT / ".agents" / "api" / "main.py"


def test_handoff_reads_and_claims_respect_direct_to_agent() -> None:
    source = API_MAIN.read_text(encoding="utf-8")

    assert "to_role, to_agent" in source
    assert "lower(split_part(COALESCE(to_agent, ''), '@', 1))" in source
    assert "lower(split_part(COALESCE(to_agent, ''), ':', 1))" not in source
    assert "COALESCE(to_agent, '') = ''" in source
    assert "AND lower(to_role) = ANY({roles}::text[])" in source


def test_stale_handoff_endpoint_exposes_to_agent_for_beat_filtering() -> None:
    source = API_MAIN.read_text(encoding="utf-8")
    stale_route = source.split('@app.get("/beat/handoffs/stale")', 1)[1]

    assert "to_agent" in stale_route.split("FROM handoffs", 1)[0]
