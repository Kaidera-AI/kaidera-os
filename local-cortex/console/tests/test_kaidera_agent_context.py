"""Context-window management for the kaidera agent (app/kaidera_agent.py).

These guard the fix for the two production bugs Wren hit on a big task:
  * ModelHTTPError "prompt is too long" — the agent.iter() loop accumulated every
    tool result until the context window overflowed. `_fit_context` (a ProcessHistory
    capability) now compacts old tool results and drops the middle to fit a char budget.
  * UsageLimitExceeded request_limit=50 — wired `usage_limits` with a higher cap.

The history helpers are duck-typed (getattr + the `part_kind` discriminator), so we
exercise them with light stand-in objects — no real pydantic-ai message classes needed.
"""
from types import SimpleNamespace

from app import kaidera_agent as ea


def _part(kind, content=None, args=None):
    return SimpleNamespace(part_kind=kind, content=content, args=args)


def _req(*parts):   # ModelRequest-like: user prompt / tool returns
    return SimpleNamespace(parts=list(parts), kind="request")


def _resp(*parts):  # ModelResponse-like: assistant text / tool calls
    return SimpleNamespace(parts=list(parts), kind="response")


def test_int_env(monkeypatch):
    monkeypatch.setenv("KAIDERA_TEST_X", "123")
    assert ea._int_env("KAIDERA_TEST_X", 9) == 123
    monkeypatch.setenv("KAIDERA_TEST_X", "not-a-number")
    assert ea._int_env("KAIDERA_TEST_X", 9) == 9           # malformed → default
    monkeypatch.setenv("KAIDERA_TEST_X", "0")
    assert ea._int_env("KAIDERA_TEST_X", 9) == 9           # non-positive → default
    monkeypatch.delenv("KAIDERA_TEST_X", raising=False)
    assert ea._int_env("KAIDERA_TEST_X", 9) == 9           # absent → default


def test_part_and_msg_size_count_content_and_args():
    assert ea._part_size(_part("text", content="hello world")) == len("hello world")
    assert ea._part_size(_part("tool-call", args={"command": "ls -la"})) > 0
    msg = _resp(_part("text", content="abc"), _part("tool-call", args={"k": "v"}))
    assert ea._msg_size(msg) >= 3


def test_fit_context_under_budget_is_unchanged(monkeypatch):
    monkeypatch.setattr(ea, "_CONTEXT_CHAR_BUDGET", 10_000)
    msgs = [_req(_part("user-prompt", content="do the thing")),
            _resp(_part("text", content="done"))]
    assert ea._fit_context(msgs) is msgs                    # short history → returned as-is


def test_fit_context_over_budget_drops_middle_keeps_head_and_recent(monkeypatch):
    monkeypatch.setattr(ea, "_CONTEXT_CHAR_BUDGET", 800)
    monkeypatch.setattr(ea, "_HISTORY_TOOL_RESULT_CAP", 100)
    head = _req(_part("user-prompt", content="TASK"))
    msgs = [head]
    for i in range(25):                                     # 25 fat tool exchanges
        msgs.append(_resp(_part("tool-call", args={"i": i})))
        msgs.append(_req(_part("tool-return", content="X" * 300)))
    out = ea._fit_context(msgs)
    assert out[0] is head                                   # the task is never dropped
    assert len(out) < len(msgs)                             # the middle was dropped
    # The kept window must not start on an orphaned tool-return (its call was dropped).
    assert len(out) == 1 or not ea._has_tool_return(out[1])
    # Total stays bounded (head + at most one over-budget tail message).
    assert sum(ea._msg_size(m) for m in out) <= 800 + ea._msg_size(msgs[-1]) + ea._msg_size(head)


def test_compact_leaves_the_most_recent_result_rich(monkeypatch):
    monkeypatch.setattr(ea, "_HISTORY_TOOL_RESULT_CAP", 50)
    old = _req(_part("tool-return", content="A" * 500))
    new = _req(_part("tool-return", content="B" * 500))
    msgs = [_req(_part("user-prompt", content="t")),
            _resp(_part("tool-call", args={})), old,
            _resp(_part("tool-call", args={})), new]
    ea._compact_old_tool_returns(msgs)
    assert len(old.parts[0].content) < 500                  # an OLD result is compacted
    assert old.parts[0].content.startswith("A" * 50)
    assert new.parts[0].content == "B" * 500                # the newest result is untouched


def test_compact_is_idempotent(monkeypatch):
    monkeypatch.setattr(ea, "_HISTORY_TOOL_RESULT_CAP", 50)
    old = _req(_part("tool-return", content="A" * 500))
    msgs = [old, _req(_part("user-prompt", content="x"))]   # old is not last → eligible
    ea._compact_old_tool_returns(msgs)
    once = old.parts[0].content
    ea._compact_old_tool_returns(msgs)
    assert old.parts[0].content == once                     # re-running does not shrink further


def test_loop_bound_defaults_are_sane():
    assert ea._REQUEST_LIMIT >= 100                         # well above pydantic-ai's default 50
    assert ea._CONTEXT_CHAR_BUDGET >= 100_000               # real working context
    assert ea._WEB_FETCH_CAP <= 100_000                     # a single page can't dominate the window
