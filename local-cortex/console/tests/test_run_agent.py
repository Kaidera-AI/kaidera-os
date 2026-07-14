import pytest
from app.run_agent import run_one, RunResult
from tests.conftest import FakeRunner


@pytest.mark.asyncio
async def test_happy_path_claims_runs_completes(fake_cortex, routing_stub):
    runner = FakeRunner([
        {"type": "delta", "text": "Working"},
        {"type": "result", "text": "", "tokens_in": 10, "tokens_out": 5, "cost_usd": 0.001},
        {"type": "done"},
    ])
    res = await run_one(
        "bob", "h-123", "kaidera-os",
        cortex=fake_cortex, runner=runner, routing=routing_stub,
        task_summary="write the file",
    )
    assert isinstance(res, RunResult)
    assert res.status == "completed"
    kinds = [c[0] for c in fake_cortex.calls]
    assert kinds[0] == "claim"
    assert "complete" in kinds
    # The worker must forward the routed harness + reasoning level to the harness;
    # routing_stub returns ("pi", "gpt-5.3-codex-spark", "high"), so reasoning must
    # arrive as "high" (it used to be dropped → pi ran at its provider default).
    assert runner.last_call["harness"] == "pi"
    assert runner.last_call["reasoning"] == "high"
    assert runner.last_call["run_context"] == "autonomous"


@pytest.mark.asyncio
async def test_cannot_claim_skips(routing_stub):
    from tests.conftest import FakeCortex, FakeRunner
    cortex = FakeCortex(claim_ok=False)
    res = await run_one("bob", "h-1", "kaidera-os", cortex=cortex, runner=FakeRunner([]),
                        routing=routing_stub, task_summary="x")
    assert res.status == "skipped"
    assert ("complete", {"handoff_id": "h-1"}) not in cortex.calls


@pytest.mark.asyncio
async def test_harness_error_fails_no_complete(fake_cortex, routing_stub):
    from tests.conftest import FakeRunner
    runner = FakeRunner([{"type": "error", "message": "model not available"}, {"type": "done"}])
    res = await run_one("bob", "h-2", "kaidera-os", cortex=fake_cortex, runner=runner,
                        routing=routing_stub, task_summary="x")
    assert res.status == "failed"
    assert res.error == "model not available"
    assert ("complete", {"handoff_id": "h-2"}) not in fake_cortex.calls


@pytest.mark.asyncio
async def test_narrates_thinking_and_tools_as_ordered_steps(fake_cortex, routing_stub):
    """The spawned worker must narrate its thinking + tool calls to Cortex as ordered
    STEP rows (so the console pane can show the work live, not just status). Thinking
    deltas aggregate into ONE step per thought; tools get their own step."""
    from tests.conftest import FakeRunner
    runner = FakeRunner([
        {"type": "thinking", "text": "Let me think "},
        {"type": "thinking", "text": "about the file."},
        {"type": "tool", "name": "Bash", "text": "Bash(write file)"},
        {"type": "delta", "text": "Done."},
        {"type": "result", "text": "", "tokens_in": 1, "tokens_out": 1, "cost_usd": 0.0},
        {"type": "done"},
    ])
    res = await run_one("bob", "h-step", "kaidera-os", cortex=fake_cortex, runner=runner,
                        routing=routing_stub, task_summary="x")
    assert res.status == "completed"
    logs = [c[1]["summary"] for c in fake_cortex.calls if c[0] == "log"]
    think_steps = [s for s in logs if "STEP h-step" in s and " think " in s]
    tool_steps = [s for s in logs if "STEP h-step" in s and " tool " in s]
    assert len(think_steps) == 1                      # two thinking deltas → one aggregated thought
    assert "about the file" in think_steps[0]
    assert "#001" in think_steps[0]                   # ordered
    assert len(tool_steps) == 1
    assert "Bash" in tool_steps[0]
    assert "#002" in tool_steps[0]                    # tool came after the thought
    assert any("TRANSCRIPT h-step" in s and "Done." in s for s in logs)  # reply still persisted


@pytest.mark.asyncio
async def test_logs_checkin_and_transcript(fake_cortex, routing_stub):
    from tests.conftest import FakeRunner
    runner = FakeRunner([
        {"type": "tool", "name": "write", "text": "write(file)"},
        {"type": "delta", "text": "done"},
        {"type": "result", "text": "", "tokens_in": 1, "tokens_out": 1, "cost_usd": 0.0},
        {"type": "done"},
    ])
    res = await run_one("bob", "h-9", "kaidera-os", cortex=fake_cortex, runner=runner,
                        routing=routing_stub, task_summary="x")
    logs = [c for c in fake_cortex.calls if c[0] == "log"]
    assert any("checkin" in c[1]["event_type"] or "decision" == c[1]["event_type"] for c in logs)
    assert any("transcript" in c[1]["summary"].lower() or "TRANSCRIPT" in c[1]["summary"] for c in logs)


@pytest.mark.asyncio
async def test_result_echo_is_not_duplicated_in_worker_reply(fake_cortex, routing_stub):
    runner = FakeRunner([
        {"type": "delta", "text": "HEAL "},
        {"type": "delta", "text": "OK"},
        {"type": "result", "text": "HEAL OK", "tokens_in": 1, "tokens_out": 1, "cost_usd": 0.0},
        {"type": "done"},
    ])

    res = await run_one(
        "bob", "h-dedupe", "kaidera-os",
        cortex=fake_cortex, runner=runner, routing=routing_stub,
        task_summary="x",
    )

    assert res.status == "completed"
    assert res.text == "HEAL OK"
    transcripts = [
        c[1]["summary"] for c in fake_cortex.calls
        if c[0] == "log" and "TRANSCRIPT h-dedupe" in c[1]["summary"]
    ]
    assert transcripts and "HEAL OKHEAL OK" not in transcripts[-1]
