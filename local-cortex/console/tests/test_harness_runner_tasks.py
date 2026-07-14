"""Tasks + sub-agent structured events (the chat "N/total tasks · ⛓ sub-agents"
indicator).

claude-code streams its task list via the ``TodoWrite`` tool and its sub-agent
spawns via the ``Task`` tool. ``harness_runner`` surfaces these as STRUCTURED
``tasks`` / ``subagent`` events (in ADDITION to the normal ``tool`` event, so the
feed still shows the raw activity). These tests pin:

  * a ``TodoWrite`` tool_use yields a ``tasks`` event with the parsed items + the
    normal ``tool`` event (so the feed keeps the activity);
  * the done/total can be derived from the items (completed vs the rest);
  * a ``Task`` tool_use yields a ``subagent`` event (label) + the ``tool`` event;
  * a MALFORMED tool_input degrades to JUST the ``tool`` event and never raises —
    the house law: an unexpected payload must NEVER break the chat stream.
"""

from __future__ import annotations

from app import harness_runner as hr


def _kinds(events):
    return [e.get("type") for e in events]


def test_tool_extra_events_todowrite_parses_items_and_counts():
    items_ev = hr._tool_extra_events(
        "TodoWrite",
        {"todos": [
            {"content": "a", "status": "completed"},
            {"content": "b", "status": "pending"},
            {"content": "c", "status": "in_progress", "activeForm": "Doing c"},
        ]},
    )
    assert len(items_ev) == 1
    ev = items_ev[0]
    assert ev["type"] == "tasks"
    items = ev["items"]
    assert [i["content"] for i in items] == ["a", "b", "c"]
    assert [i["status"] for i in items] == ["completed", "pending", "in_progress"]
    # activeForm is forwarded only when present.
    assert items[2]["activeForm"] == "Doing c"
    assert "activeForm" not in items[0]
    # The UI derives done/total: 1 completed of 3.
    done = sum(1 for i in items if i["status"] == "completed")
    assert (done, len(items)) == (1, 3)


def test_tool_extra_events_task_yields_subagent_label():
    ev = hr._tool_extra_events(
        "Task",
        {"description": "Investigate flake", "subagent_type": "general-purpose",
         "prompt": "look into the failing test"},
    )
    assert len(ev) == 1
    assert ev[0]["type"] == "subagent"
    # Prefers the human-readable description.
    assert ev[0]["label"] == "Investigate flake"


def test_tool_extra_events_task_falls_back_to_subagent_type_then_generic():
    by_type = hr._tool_extra_events("Task", {"subagent_type": "code-reviewer"})
    assert by_type[0]["label"] == "code-reviewer"
    generic = hr._tool_extra_events("Task", {})
    assert generic[0]["label"] == "sub-agent"


def test_tool_extra_events_ignores_other_tools():
    assert hr._tool_extra_events("Bash", {"command": "ls"}) == []
    assert hr._tool_extra_events("Read", {"file_path": "/x"}) == []


def test_assistant_blocks_malformed_frame_returns_empty_list():
    assert hr._events_from_assistant_blocks({}) == []
    assert hr._events_from_assistant_blocks({"message": {"content": "bad"}}) == []


def test_assistant_blocks_emit_both_tool_and_tasks_for_todowrite():
    """The complete-assistant-frame path emits the normal ``tool`` event AND the
    structured ``tasks`` event for a ``TodoWrite`` block (counts derivable)."""
    frame = {
        "type": "assistant",
        "message": {"content": [
            {"type": "tool_use", "name": "TodoWrite", "input": {"todos": [
                {"content": "a", "status": "completed"},
                {"content": "b", "status": "pending"},
            ]}},
        ]},
    }
    events = hr._events_from_assistant_blocks(frame)
    assert "tool" in _kinds(events)
    assert "tasks" in _kinds(events)
    tasks_ev = next(e for e in events if e["type"] == "tasks")
    done = sum(1 for i in tasks_ev["items"] if i["status"] == "completed")
    assert (done, len(tasks_ev["items"])) == (1, 2)


def test_assistant_blocks_emit_both_tool_and_subagent_for_task():
    frame = {
        "type": "assistant",
        "message": {"content": [
            {"type": "tool_use", "name": "Task", "input": {"description": "do x"}},
        ]},
    }
    events = hr._events_from_assistant_blocks(frame)
    assert _kinds(events) == ["tool", "subagent"]
    assert events[1]["label"] == "do x"


def test_malformed_todowrite_degrades_to_plain_tool_event_no_raise():
    """A malformed tool_input must NOT raise and must NOT yield a tasks event — the
    plain ``tool`` event (emitted by the caller) still carries the activity."""
    # tool_input is not a dict.
    assert hr._tool_extra_events("TodoWrite", "garbage") == []
    # `todos` is not a list.
    assert hr._tool_extra_events("TodoWrite", {"todos": "nope"}) == []
    # `todos` items aren't dicts → skipped, empty (but valid) list.
    only_bad = hr._tool_extra_events("TodoWrite", {"todos": ["x", 1, None]})
    assert only_bad == [{"type": "tasks", "items": []}]
    # None input.
    assert hr._tool_extra_events("TodoWrite", None) == []

    # End-to-end through the assistant-frame path: a malformed TodoWrite block still
    # yields the plain tool event (so the feed shows the activity) and no tasks event,
    # and the whole call never raises.
    frame = {
        "type": "assistant",
        "message": {"content": [
            {"type": "tool_use", "name": "TodoWrite", "input": "not-a-dict"},
        ]},
    }
    events = hr._events_from_assistant_blocks(frame)
    assert _kinds(events) == ["tool"]


def test_todowrite_empty_list_yields_empty_tasks_event():
    """A cleared task list (empty todos) is still a valid ``tasks`` event with no
    items — the UI shows 0/0 (or hides it), it does not error."""
    ev = hr._tool_extra_events("TodoWrite", {"todos": []})
    assert ev == [{"type": "tasks", "items": []}]


def test_todowrite_unknown_status_kept_but_not_counted_done():
    ev = hr._tool_extra_events(
        "TodoWrite", {"todos": [{"content": "x", "status": "blocked"}]},
    )
    item = ev[0]["items"][0]
    assert item["status"] == "blocked"  # verbatim, not dropped
    done = sum(1 for i in ev[0]["items"] if i["status"] == "completed")
    assert done == 0  # an unexpected status is never credited as done


def test_parse_frame_assistant_todowrite_end_to_end():
    """The public-ish ``_parse_frame`` path (what stream_chat iterates) surfaces both
    events for a TodoWrite assistant frame."""
    frame = {
        "type": "assistant",
        "message": {"content": [
            {"type": "tool_use", "name": "TodoWrite", "input": {"todos": [
                {"content": "a", "status": "completed"},
            ]}},
        ]},
    }
    events = hr._parse_frame(frame)
    assert set(_kinds(events)) == {"tool", "tasks"}
