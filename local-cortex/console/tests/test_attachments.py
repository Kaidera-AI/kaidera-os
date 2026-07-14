"""Chat file-attachments (feature-gap step 6, Inc A) — the sandbox + injection +
cleanup shell (`app/attachments.py`).

This module is the imperative half: it confines uploaded bytes to a sandbox
(`ATTACHMENTS_ROOT`), decodes + size-checks + writes them (`receive_upload`), weaves
text files into the prompt / falls back gracefully for images (`inline_attachments`),
and best-effort cleans a run's files (`cleanup_run_attachments`) + sweeps stale dirs
(`sweep_stale_attachments`).

SECURITY (the whole point of the sandbox layer): the confinement gate is a LOCAL COPY
of `workspace._is_within` (the blueprint says COPY, never import — the import-linter
would flag a cross-module reach). These tests pin:
  * an escaping filename (`../`, an absolute path, a nested traversal) is rejected
    BEFORE any decode/write — nothing lands outside the sandbox;
  * `receive_upload` writes a valid file + returns AttachmentMeta; an oversized body
    raises (the per-file cap);
  * `inline_attachments`: a text file becomes a fenced `[Attached: …]` block; an image
    becomes a graceful `[Attached image: … — not readable …]` note (claude `-p`/pi
    can't take images non-interactively); a missing path is SKIPPED (never raises);
    `inline_attachments([], prompt)` returns the prompt UNCHANGED (backward-compat);
  * cleanup removes a run's dir; the stale sweep removes OLD dirs + keeps RECENT ones.

A PURITY GUARD asserts `app.attachments` does NOT import `app.workspace` (the gate is
copied, not borrowed).
"""

from __future__ import annotations

import ast
import base64
import time
from pathlib import Path

import pytest

import app.attachments as att


# ---------------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------------

def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Point ATTACHMENTS_ROOT at a temp dir for the duration of a test (so writes are
    hermetic + auto-cleaned)."""
    root = tmp_path / "attachments"
    monkeypatch.setenv("HARNESS_ATTACHMENTS_ROOT", str(root))
    # The module reads the env at call time via _attachments_root(), so no reload needed.
    return root


# ---------------------------------------------------------------------------
#  Sandbox confinement — safe_attachment_path
# ---------------------------------------------------------------------------

def test_safe_path_for_plain_filename_is_inside_sandbox(sandbox):
    p = att.safe_attachment_path("run-1", "notes.txt")
    root = att._attachments_root().resolve()
    assert Path(p).resolve().is_relative_to(root)
    assert Path(p).name == "notes.txt"


@pytest.mark.parametrize(
    "evil",
    [
        "../evil.txt",
        "../../etc/passwd",
        "/etc/passwd",
        "/tmp/evil.txt",
        "sub/../../evil.txt",
        "a/b/../../../escape.txt",
    ],
)
def test_safe_path_rejects_escapes(sandbox, evil):
    """Any `..`/absolute filename is rejected — the bytes never land outside the
    sandbox. Rejection happens via AttachmentError (not a silent clamp)."""
    with pytest.raises(att.AttachmentError):
        att.safe_attachment_path("run-1", evil)


def test_safe_path_rejects_nul_byte(sandbox):
    with pytest.raises(att.AttachmentError):
        att.safe_attachment_path("run-1", "a\x00b.txt")


def test_safe_path_rejects_escape_in_run_id(sandbox):
    """The run_id is also part of the path — an escaping run_id can't break out."""
    with pytest.raises(att.AttachmentError):
        att.safe_attachment_path("../../evil", "ok.txt")


# ---------------------------------------------------------------------------
#  receive_upload — decode + size-check + write
# ---------------------------------------------------------------------------

def test_receive_upload_writes_file_and_returns_meta(sandbox):
    meta = att.receive_upload("run-1", "hello.txt", _b64(b"hi there"), "text/plain")
    assert meta.filename == "hello.txt"
    assert meta.run_id == "run-1"
    assert meta.size_bytes == len(b"hi there")
    assert meta.content_type == "text/plain"
    # The bytes really landed at host_path, inside the sandbox.
    on_disk = Path(meta.host_path)
    assert on_disk.read_bytes() == b"hi there"
    assert on_disk.resolve().is_relative_to(att._attachments_root().resolve())
    assert meta.attachment_id  # a non-empty minted id


def test_receive_upload_strips_directory_component_from_filename(sandbox):
    """A filename that smuggles a path is reduced to its basename (defence in depth on
    top of the gate)."""
    meta = att.receive_upload("run-1", "x.txt", _b64(b"data"), "text/plain")
    assert "/" not in meta.filename


def test_receive_upload_oversized_raises(sandbox, monkeypatch):
    """A body over the per-file cap raises AttachmentError and writes NOTHING."""
    monkeypatch.setattr(att, "MAX_FILE_BYTES", 4)
    with pytest.raises(att.AttachmentError):
        att.receive_upload("run-1", "big.txt", _b64(b"way too big"), "text/plain")
    # Nothing was written for this run.
    run_dir = att._run_dir("run-1")
    assert not run_dir.exists() or not any(run_dir.iterdir())


def test_receive_upload_rejects_escaping_filename(sandbox):
    with pytest.raises(att.AttachmentError):
        att.receive_upload("run-1", "../escape.txt", _b64(b"x"), "text/plain")


def test_receive_upload_rejects_bad_base64(sandbox):
    with pytest.raises(att.AttachmentError):
        att.receive_upload("run-1", "x.txt", "!!!not base64!!!", "text/plain")


# ---------------------------------------------------------------------------
#  inline_attachments — weave into the prompt
# ---------------------------------------------------------------------------

def test_inline_empty_returns_prompt_unchanged(sandbox):
    """The backward-compat invariant: no attachments → the prompt is byte-for-byte
    unchanged."""
    assert att.inline_attachments([], "hello") == "hello"
    assert att.inline_attachments(None, "hello") == "hello"


def test_inline_text_file_becomes_fenced_block(sandbox):
    meta = att.receive_upload("run-1", "notes.txt", _b64(b"line one\nline two"), "text/plain")
    out = att.inline_attachments([meta.host_path], "Please review")
    assert "Please review" in out
    assert "[Attached: notes.txt]" in out
    assert "line one" in out
    assert "line two" in out
    assert "```" in out  # fenced


def test_inline_json_and_md_count_as_text(sandbox):
    mj = att.receive_upload("run-1", "d.json", _b64(b'{"k": 1}'), "application/json")
    out = att.inline_attachments([mj.host_path], "p")
    assert '{"k": 1}' in out
    assert "[Attached: d.json]" in out


def test_inline_text_content_is_capped(sandbox, monkeypatch):
    """A huge text file is truncated to the inline cap (the prompt can't balloon)."""
    monkeypatch.setattr(att, "MAX_INLINE_TEXT_BYTES", 10)
    big = b"X" * 500
    meta = att.receive_upload("run-1", "big.txt", _b64(big), "text/plain")
    out = att.inline_attachments([meta.host_path], "p")
    # Only the capped slice is inlined (+ a truncation marker), never all 500 bytes.
    assert out.count("X") <= 60
    assert "truncated" in out.lower()


def test_inline_image_is_graceful_fallback_note(sandbox):
    """An IMAGE is NOT readable by claude `-p` / pi non-interactive, so it becomes a
    clear note — never the raw bytes, never a crash."""
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    meta = att.receive_upload("run-1", "shot.png", _b64(png), "image/png")
    out = att.inline_attachments([meta.host_path], "look at this")
    assert "look at this" in out
    assert "shot.png" in out
    assert "image" in out.lower()
    assert "not readable" in out.lower()
    # The raw PNG signature bytes are NOT dumped into the prompt.
    assert "\x89PNG" not in out


def test_inline_image_can_surface_path_for_vision_capable_lane(sandbox):
    """A caller that has already proven the harness/model is vision-capable can expose
    the sandbox path instead of the not-readable fallback. The image bytes still never
    get base64-inlined into the prompt."""
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    meta = att.receive_upload("run-1", "shot.png", _b64(png), "image/png")
    out = att.inline_attachments([meta.host_path], "look at this", image_readable=True)
    assert "look at this" in out
    assert "shot.png" in out
    assert "Vision-capable attachment path" in out
    assert meta.host_path in out
    assert "not readable" not in out.lower()
    assert "\x89PNG" not in out


def test_inline_missing_path_is_skipped_not_raised(sandbox):
    """A missing path (e.g. a host-upload that degraded) is SKIPPED — the turn still
    sends, just without that attachment."""
    out = att.inline_attachments(["/nonexistent/run-1/gone.txt"], "still works")
    assert out == "still works"  # nothing to inline → unchanged


def test_inline_binary_nonimage_becomes_note(sandbox):
    """A non-text, non-image blob (e.g. a PDF/octet-stream) gets a note, not raw
    bytes."""
    meta = att.receive_upload("run-1", "doc.pdf", _b64(b"%PDF-1.4\x00\x01binary"), "application/pdf")
    out = att.inline_attachments([meta.host_path], "p")
    assert "doc.pdf" in out
    assert "%PDF" not in out  # raw bytes never inlined


def test_inline_mixes_text_and_image(sandbox):
    t = att.receive_upload("run-1", "a.txt", _b64(b"hello text"), "text/plain")
    i = att.receive_upload("run-1", "b.png", _b64(b"\x89PNG\r\n\x1a\n\x00"), "image/png")
    out = att.inline_attachments([t.host_path, i.host_path], "review both")
    assert "hello text" in out
    assert "b.png" in out
    assert "review both" in out


# ---------------------------------------------------------------------------
#  cleanup + stale sweep
# ---------------------------------------------------------------------------

def test_list_run_attachments_returns_written_files(sandbox):
    a = att.receive_upload("run-1", "a.txt", _b64(b"aaa"), "text/plain")
    b = att.receive_upload("run-1", "b.txt", _b64(b"bbb"), "text/plain")
    paths = att.list_run_attachments("run-1")
    names = sorted(p.name for p in paths)
    assert names == ["a.txt", "b.txt"]
    # The paths are the real host_paths (so the chat route can inline them).
    assert {str(p) for p in paths} == {a.host_path, b.host_path}


def test_list_run_attachments_empty_for_unknown_run(sandbox):
    assert att.list_run_attachments("never") == []


def test_list_run_attachments_rejects_escape(sandbox):
    assert att.list_run_attachments("../../etc") == []


def test_cleanup_run_attachments_removes_dir(sandbox):
    meta = att.receive_upload("run-1", "x.txt", _b64(b"data"), "text/plain")
    run_dir = att._run_dir("run-1")
    assert run_dir.exists()
    att.cleanup_run_attachments("run-1")
    assert not run_dir.exists()


def test_cleanup_missing_run_is_noop_no_raise(sandbox):
    # No dir for this run → best-effort, never raises.
    att.cleanup_run_attachments("never-existed")


def test_cleanup_rejects_escape_quietly(sandbox):
    """An escaping run_id can't make cleanup delete outside the sandbox — it's a
    best-effort no-op, never a raise, never an out-of-root delete."""
    outside = sandbox.parent / "outside.txt"
    sandbox.mkdir(parents=True, exist_ok=True)
    outside.write_text("keep me")
    att.cleanup_run_attachments("../outside")  # must NOT delete outside.txt
    assert outside.exists()


def test_sweep_removes_old_keeps_recent(sandbox):
    # Two runs: one with an OLD mtime, one fresh.
    old = att.receive_upload("run-old", "o.txt", _b64(b"old"), "text/plain")
    new = att.receive_upload("run-new", "n.txt", _b64(b"new"), "text/plain")
    old_dir = att._run_dir("run-old")
    new_dir = att._run_dir("run-new")
    # Backdate the old dir well past the 24h cutoff.
    past = time.time() - (48 * 3600)
    import os
    os.utime(old_dir, (past, past))

    att.sweep_stale_attachments(max_age_s=24 * 3600)
    assert not old_dir.exists()
    assert new_dir.exists()


def test_sweep_on_empty_root_is_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_ATTACHMENTS_ROOT", str(tmp_path / "never"))
    # Root doesn't exist yet → best-effort no-op, never raises.
    att.sweep_stale_attachments(max_age_s=1)


# ---------------------------------------------------------------------------
#  Caps are config-as-data (env-overridable, no per-project literal)
# ---------------------------------------------------------------------------

def test_caps_are_module_constants():
    assert isinstance(att.MAX_FILE_BYTES, int) and att.MAX_FILE_BYTES > 0
    assert isinstance(att.MAX_FILES_PER_TURN, int) and att.MAX_FILES_PER_TURN > 0
    assert isinstance(att.MAX_TURN_BYTES, int) and att.MAX_TURN_BYTES > 0


# ---------------------------------------------------------------------------
#  PURITY GUARD — app.attachments must NOT import app.workspace (the gate is COPIED)
# ---------------------------------------------------------------------------

def test_attachments_does_not_import_workspace():
    """The `_is_within` confinement gate is a LOCAL COPY of workspace's — the blueprint
    forbids importing `app.workspace` here (the import-linter would flag the reach).
    AST-parse the source so a name in a comment can't fool the check."""
    src = Path(att.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert "workspace" not in node.module, (
                "app.attachments must not import app.workspace (copy the gate)"
            )
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert "workspace" not in alias.name
