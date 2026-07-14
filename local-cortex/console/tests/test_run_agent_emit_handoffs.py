"""App-side reliable handoff filing — the agent EMITS, the app FILES.

Covers the parse + body-map + file path that makes a planner's decomposition land
deterministically (via the unsandboxed parent), independent of the model's own
shell behavior. See run_agent.parse_emitted_handoffs / file_emitted_handoffs.
"""
import asyncio

import app.run_agent as r


def test_parse_extracts_specs():
    text = """preamble prose
===FILE-HANDOFFS===
[{"summary": "Do X", "to_role": "fsd", "wave": 0, "priority": "high"},
 {"summary": "Do Y", "to_role": "fsd", "wave": 1}]
===END-FILE-HANDOFFS===
trailing prose"""
    specs = r.parse_emitted_handoffs(text)
    assert len(specs) == 2
    assert specs[0]["summary"] == "Do X"
    assert specs[1]["wave"] == 1


def test_parse_tolerates_fenced_json():
    text = "===FILE-HANDOFFS===\n```json\n[{\"summary\": \"Z\", \"to_role\": \"fsd\"}]\n```\n===END-FILE-HANDOFFS==="
    specs = r.parse_emitted_handoffs(text)
    assert len(specs) == 1 and specs[0]["to_role"] == "fsd"


def test_parse_requires_summary_and_role():
    text = ('===FILE-HANDOFFS===\n'
            '[{"summary": "no role"}, {"to_role": "fsd"}, {"summary": "ok", "to_role": "fsd"}]\n'
            '===END-FILE-HANDOFFS===')
    specs = r.parse_emitted_handoffs(text)
    assert len(specs) == 1 and specs[0]["summary"] == "ok"


def test_parse_no_block_or_malformed_returns_empty():
    assert r.parse_emitted_handoffs("just prose, no block") == []
    assert r.parse_emitted_handoffs("===FILE-HANDOFFS===\nnot json at all\n===END-FILE-HANDOFFS===") == []
    assert r.parse_emitted_handoffs("") == []
    # a JSON object (not array) is rejected
    assert r.parse_emitted_handoffs('===FILE-HANDOFFS===\n{"summary":"x","to_role":"y"}\n===END-FILE-HANDOFFS===') == []


def test_parse_caps_at_max():
    items = ",".join('{"summary":"s%d","to_role":"fsd"}' % i for i in range(50))
    text = "===FILE-HANDOFFS===\n[" + items + "]\n===END-FILE-HANDOFFS==="
    assert len(r.parse_emitted_handoffs(text)) == r.MAX_EMITTED_HANDOFFS


def test_body_from_spec_maps_fields():
    body = r._handoff_body_from_spec(
        {"summary": "S", "to_role": "FSD", "wave": 2, "acceptance": "tests pass", "context": "scope"}
    )
    assert body["summary"] == "S"
    assert body["to_role"] == "fsd"            # lowercased
    assert body["priority"] == "medium"        # default
    assert body["verification"] == "tests pass"  # acceptance folded in
    assert "[wave 2]" in body["context"]       # wave sequencing folded into context


def test_file_emitted_handoffs_files_each_and_returns_ids():
    calls = []

    class FakeCortex:
        async def create_handoff(self, from_agent, body):
            calls.append((from_agent, body))
            return {"id": "h-%d" % len(calls)}

    text = ('===FILE-HANDOFFS===\n'
            '[{"summary":"A","to_role":"fsd"},{"summary":"B","to_role":"fsd"}]\n'
            '===END-FILE-HANDOFFS===')
    filed = asyncio.run(r.file_emitted_handoffs(FakeCortex(), "kai", text))
    assert filed == ["h-1", "h-2"]
    assert len(calls) == 2 and calls[0][0] == "kai"
    assert calls[0][1]["to_role"] == "fsd"     # real body passed, not a stub


def test_file_emitted_handoffs_skips_failures():
    class FakeCortex:
        async def create_handoff(self, from_agent, body):
            if "fail" in body["summary"]:
                return {"ok": False, "error": "nope"}
            return {"id": "ok-1"}

    text = ('===FILE-HANDOFFS===\n'
            '[{"summary":"fail this","to_role":"fsd"},{"summary":"good","to_role":"fsd"}]\n'
            '===END-FILE-HANDOFFS===')
    filed = asyncio.run(r.file_emitted_handoffs(FakeCortex(), "kai", text))
    assert filed == ["ok-1"]
