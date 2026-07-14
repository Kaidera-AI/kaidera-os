"""Unit tests for Cortex ingest write-side transforms (E2 + E4)."""

from __future__ import annotations

import pytest
from ingest.chat_distill import distill_message, is_always_keep
from ingest.symbolic_compact import compact_text, is_compactable

# --- E2 Chat Distill Tests ---

def test_is_always_keep_floor():
    """Always-keep floor must protect critical signals."""
    assert is_always_keep("/usr/local/bin/python") is True
    assert is_always_keep("verify: results must match") is True
    assert is_always_keep("to_role: full-stack-developer") is True
    assert is_always_keep("```python\nprint('hi')\n```") is True
    assert is_always_keep('{"key": "value"}') is True
    assert is_always_keep("Just some normal chatter.") is False

def test_distill_message_extracts_commitments():
    """Commitment extraction should find decisions and facts."""
    text = "I decided to use zlib. The sky is blue. I really like pizza."
    commitments = distill_message(text)

    # "I decided to use zlib" -> decision
    # "The sky is blue" -> fact
    # "I really like pizza" -> dropped (no keyword, not a naive fact)

    assert len(commitments) == 2
    assert commitments[0]["metadata"]["commitment_type"] == "decision"
    assert commitments[1]["metadata"]["commitment_type"] == "fact"

# --- E4 Symbolic Compact Tests ---

def test_is_compactable():
    """Compaction pre-check for efficiency."""
    assert is_compactable("Too short.") is False
    assert is_compactable("This is a much longer string that has redundant whitespace and polite filler words basically.") is True

def test_compact_text_reduces_size():
    """Compaction must shrink text without dropping protected tokens."""
    text = "Please kindly check the path /app/main.py. It is basically essential in order to run the system."
    compact, changed, savings = compact_text(text)

    assert changed is True
    assert savings > 0
    assert "/app/main.py" in compact # Protected
    assert "essential" in compact # Protected (capitalised/important-ish)
    assert "kindly" not in compact.lower() # Dropped filler (lowercase)
    assert "basically" not in compact.lower() # Dropped filler
    assert "in order to" not in compact.lower() # Compacted tense


# --- Recall-gate benchmark (E2+E4) ---

def test_recall_gate_passes_with_zero_commitment_loss():
    """The E2+E4 recall-gate benchmark must pass: >=95% protected-token recall
    and 0 commitment loss over the representative corpus."""
    from ingest.recall_gate import run_benchmark

    res = run_benchmark()
    assert res.passed is True, (
        f"recall gate failed: recall={res.mean_protected_recall} "
        f"lost={res.total_commitments_lost}"
    )
    assert res.mean_protected_recall >= 0.95
    assert res.total_commitments_lost == 0


def test_recall_gate_protected_tokens_survive_compaction():
    """Every protected token (path/uuid/code) in an always-keep message must
    survive E4 compaction verbatim."""
    from ingest.recall_gate import run_benchmark

    res = run_benchmark()
    for case in res.cases:
        assert case.protected_recall == 1.0, (
            f"{case.label}: a protected token was lost"
        )
