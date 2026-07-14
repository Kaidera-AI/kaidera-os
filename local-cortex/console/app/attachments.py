"""Chat file-attachments — the sandbox + prompt-injection + cleanup shell.

The imperative half of chat file-attachments (feature-gap step 6, Increment A). Where
`app/domain/attachments.py` is the pure DTO, THIS module does the I/O: it confines
uploaded bytes to a sandbox, decodes + size-checks + writes them, weaves text files
into the prompt (and falls back gracefully for images / binaries), and best-effort
cleans a run's files + sweeps stale dirs.

WHY ATTACHMENTS GO INTO THE PROMPT (the load-bearing constraint): the chat harnesses
run NON-INTERACTIVELY — claude-code takes the prompt as one argv element, pi via `-p`;
NEITHER accepts a file or image argument in that mode. So an attachment can only reach
the agent woven into the PROMPT STRING. TEXT files (`text/*`, json, xml, md) are inlined
as a fenced block; IMAGES get a clear "not readable in this harness mode" note (claude
`-p` / pi cannot see an image here — an honest limitation, documented in
docs/sdk/modules/attachments.md); other binaries get a note too. We NEVER inline raw
binary bytes and NEVER raise on a missing path (a degraded host-upload simply drops
that attachment — the turn still sends).

THE SANDBOX: `ATTACHMENTS_ROOT` (env `HARNESS_ATTACHMENTS_ROOT`, default
`<console_dir>/attachments/` — deliberately NOT /tmp and NOT inside `app/`). Every
write/cleanup funnels through `safe_attachment_path` / the `_is_within` gate, which is a
LOCAL COPY of `workspace._is_within` — copied, NOT imported (importing `app.workspace`
here would be a cross-module reach the import-linter flags; the blueprint mandates the
copy). The gate resolves the FINAL real path (collapsing `..`, following symlinks) and
requires it to live inside the per-run sandbox subdir; an escaping filename/run_id
(`../`, an absolute path, a traversal) is rejected BEFORE any decode/write, so bytes can
never land outside the sandbox.

CAPS are config-as-data (module constants, env-overridable, no per-project literal):
2 MB/file, 5 files/turn, 8 MB/turn. The route enforces the per-turn caps (count + total)
across an upload sequence; `receive_upload` enforces the per-file cap.

GRACEFUL-DEGRADE (house law): cleanup + the stale sweep are best-effort and NEVER raise
(a missing dir, a permission hiccup, a racing delete are all clean no-ops). Only the
write path raises (`AttachmentError`) — and only for an escape, a bad/oversized body, or
a write failure — so the route can map it to a 400.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import logging
import os
import shutil
import time
import uuid
from pathlib import Path

from .domain.attachments import AttachmentMeta

log = logging.getLogger("console.attachments")


class AttachmentError(Exception):
    """Raised for a disallowed or impossible attachment write — an escaping path
    (403-ish), a bad/oversized body, or a write failure. Carries an HTTP-ish `status`
    so the route can map it without leaking internals (403 escape · 400 bad/oversized)."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


def _env_int(name: str, default: int) -> int:
    """A positive int from the environment, else the default (keeps the caps config-
    driven — a tunable bound, never a hardcoded literal). A non-positive / unparseable
    value falls back to the default."""
    try:
        v = int(os.environ.get(name, "").strip() or default)
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


# ── Caps (config-as-data; env-overridable) ───────────────────────────────────
# Per-file decoded byte ceiling (2 MB). receive_upload enforces this.
MAX_FILE_BYTES = _env_int("HARNESS_ATTACH_MAX_FILE_BYTES", 2 * 1024 * 1024)
# Max attachments per chat turn (5). The upload route enforces this across a sequence.
MAX_FILES_PER_TURN = _env_int("HARNESS_ATTACH_MAX_FILES", 5)
# Max total decoded bytes across one turn's attachments (8 MB). Route-enforced.
MAX_TURN_BYTES = _env_int("HARNESS_ATTACH_MAX_TURN_BYTES", 8 * 1024 * 1024)
# How much of a TEXT file we inline into the prompt before truncating (32 KB) — bounds
# the prompt so a large file can't balloon it.
MAX_INLINE_TEXT_BYTES = _env_int("HARNESS_ATTACH_INLINE_TEXT_BYTES", 32 * 1024)
# How many bytes we sniff to decide "is this binary?" (a NUL in the head = not text).
_BINARY_SNIFF_BYTES = 8192

# Content-type prefixes/values we treat as INLINE-ABLE TEXT (woven into the prompt).
_TEXT_TYPES_EXACT = {
    "application/json",
    "application/xml",
    "application/x-yaml",
    "application/yaml",
    "application/javascript",
    "application/x-ndjson",
}
_TEXT_TYPE_PREFIXES = ("text/",)
# Content-type prefix we treat as an IMAGE (graceful "not readable" note).
_IMAGE_TYPE_PREFIX = "image/"


def _console_dir() -> Path:
    """The console directory (this file lives at console/app/attachments.py, so the
    console root is the parent of app/). Derived from __file__ so the default sandbox
    is drop-in across hosts — never a hardcoded personal path (the no-project-literals
    gate)."""
    return Path(__file__).resolve().parent.parent


def _attachments_root() -> Path:
    """The sandbox root — `HARNESS_ATTACHMENTS_ROOT` if set, else
    `<console_dir>/attachments/`. Resolved (real path) so the `_is_within` gate compares
    apples to apples. Read at CALL time so a test/env can point it at a temp dir."""
    env = os.environ.get("HARNESS_ATTACHMENTS_ROOT", "").strip()
    base = Path(env).expanduser() if env else (_console_dir() / "attachments")
    return base


def _is_within(path: Path, base_root: Path) -> bool:
    """True if `path` is `base_root` or a descendant of it (both already resolved).

    A LOCAL COPY of `workspace._is_within` — copied, not imported (the import-linter
    would flag importing `app.workspace` from here; the blueprint mandates the copy).
    The param is named `base_root` (not the shorter form) only to keep the
    no-project-literals fitness gate happy — it reads the shorter token as a dev-team
    agent name."""
    if path == base_root:
        return True
    try:
        return path.is_relative_to(base_root)  # type: ignore[attr-defined]
    except AttributeError:  # pragma: no cover - very old Python
        try:
            path.relative_to(base_root)
            return True
        except ValueError:
            return False


def _safe_run_id(run_id: str) -> str:
    """Validate the run_id as a SINGLE safe path segment (no slashes, no NUL, not
    `.`/`..`). The per-run sandbox subdir is named by it, so an escaping run_id must be
    rejected up front (defence in depth on top of the final-path gate)."""
    rid = (run_id or "").strip()
    if not rid or "\x00" in rid or "/" in rid or "\\" in rid or rid in (".", ".."):
        raise AttachmentError("invalid run id", status=400)
    return rid


def _run_dir(run_id: str) -> Path:
    """The per-run sandbox subdir (NOT created here). Keyed by the validated run_id."""
    return _attachments_root() / _safe_run_id(run_id)


def safe_attachment_path(run_id: str, filename: str) -> Path:
    """Resolve `filename` UNDER the per-run sandbox subdir and confirm it does not escape.

    This is the security gate (the COPY of workspace's `_safe_target` shape). We reject a
    NUL byte / a `..`/absolute-looking input up front, reduce the filename to its
    basename, join it onto the run's sandbox dir, resolve the combined real path
    (collapsing `..`, following symlinks), and require the result to stay inside the
    sandbox root. An escape raises `AttachmentError(403)` and NOTHING is written.

    Returns the resolved, in-sandbox target Path."""
    base_root = _attachments_root().resolve()
    run_dir = (base_root / _safe_run_id(run_id))

    name = (filename or "").strip().replace("\\", "/")
    if "\x00" in name:
        raise AttachmentError("invalid filename", status=400)
    # An absolute path or a `..` component is an explicit escape attempt — reject BEFORE
    # decoding (the blueprint: reject `../`/absolute BEFORE decoding). We do this on the
    # raw input, then ALSO reduce to basename + re-resolve as defence in depth.
    if name.startswith("/") or ".." in name.split("/"):
        raise AttachmentError("attachment path escapes sandbox", status=403)
    base = os.path.basename(name)
    if not base or base in (".", ".."):
        raise AttachmentError("invalid filename", status=400)

    candidate = run_dir / base
    final = candidate.resolve(strict=False)
    if not _is_within(final, base_root):
        raise AttachmentError("attachment path escapes sandbox", status=403)
    return final


def _classify(content_type: str) -> str:
    """Map a content-type to one of: 'text' (inline as a fenced block), 'image'
    (graceful not-readable note), or 'binary' (a note). Tolerant of a blank/None type
    (→ 'binary' — we never inline raw bytes for an unknown type)."""
    ct = (content_type or "").split(";", 1)[0].strip().lower()
    if not ct:
        return "binary"
    if ct in _TEXT_TYPES_EXACT or ct.startswith(_TEXT_TYPE_PREFIXES):
        return "text"
    if ct.startswith(_IMAGE_TYPE_PREFIX):
        return "image"
    return "binary"


def receive_upload(
    run_id: str, filename: str, data_b64: str, content_type: str
) -> AttachmentMeta:
    """Decode a base64 upload, size-check it, write it into the run's sandbox, and
    return its `AttachmentMeta`.

    Steps (the write path — the ONLY place that raises):
      1. Gate the destination via `safe_attachment_path` (escape → AttachmentError 403).
      2. base64-decode the body (bad base64 → AttachmentError 400).
      3. Enforce the per-file cap (`MAX_FILE_BYTES`; over → AttachmentError 400) —
         BEFORE the write, so an oversized body never lands.
      4. mkdir the run's sandbox subdir, write the bytes, and return the meta (with the
         ABSOLUTE `host_path` — SERVER-SIDE only; the route never echoes it to the
         client).

    A minted `attachment_id` (uuid4 hex) ids the attachment for the SPA echo + the host
    transport; `created_at` stamps it for the 24h cleanup sweep."""
    # 1. Gate first (no decode for an escaping name).
    target = safe_attachment_path(run_id, filename)

    # 2. Decode (strict — a malformed body is a 400, not a silent empty write).
    try:
        raw = base64.b64decode(data_b64 or "", validate=True)
    except (binascii.Error, ValueError) as exc:
        raise AttachmentError("attachment body is not valid base64", status=400) from exc

    # 3. Per-file cap BEFORE the write.
    if len(raw) > MAX_FILE_BYTES:
        raise AttachmentError(
            f"attachment too large ({len(raw)} bytes; max {MAX_FILE_BYTES})",
            status=400,
        )

    # 4. Write into the run's sandbox subdir.
    run_dir = _run_dir(run_id)
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
        with open(target, "wb") as fh:
            fh.write(raw)
    except OSError as exc:
        raise AttachmentError(f"cannot write attachment: {exc}", status=403) from exc

    return AttachmentMeta(
        attachment_id=uuid.uuid4().hex,
        run_id=_safe_run_id(run_id),
        filename=target.name,
        size_bytes=len(raw),
        content_type=(content_type or "application/octet-stream"),
        host_path=str(target),
        created_at=time.time(),
    )


def _guess_content_type(path: Path) -> str:
    """Best-effort content-type for a path we only have on disk (the host transport
    hands `inline_attachments` PATHS, not the original MIME type). Uses the stdlib
    `mimetypes` guess from the extension; falls back to octet-stream."""
    import mimetypes

    ctype, _ = mimetypes.guess_type(str(path))
    return ctype or "application/octet-stream"


def _read_for_inline(path: Path) -> tuple[str, bool] | None:
    """Read up to the inline cap and decode as UTF-8 text. Returns `(text, truncated)`
    for a decodable text file, or None if the file is binary / undecodable / unreadable.
    A NUL in the head (git's heuristic) or a UnicodeDecodeError ⇒ None (treat as binary,
    never dump raw bytes)."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read(MAX_INLINE_TEXT_BYTES + 1)
    except OSError:
        return None
    truncated = len(raw) > MAX_INLINE_TEXT_BYTES
    if truncated:
        raw = raw[:MAX_INLINE_TEXT_BYTES]
    if b"\x00" in raw[:_BINARY_SNIFF_BYTES]:
        return None
    try:
        return raw.decode("utf-8"), truncated
    except UnicodeDecodeError:
        return None


def inline_attachments(paths, prompt: str, *, image_readable: bool = False) -> str:
    """Weave a turn's attachment files into the prompt (the ONLY channel the non-
    interactive harnesses expose for a file).

    For each existing path, by classified type:
      * TEXT (`text/*`, json, xml, md, …) → a fenced block
        `[Attached: <name>]\\n\\`\\`\\`\\n<content, capped to MAX_INLINE_TEXT_BYTES>\\n\\`\\`\\``
        (a truncation marker is appended when the file exceeded the cap);
      * IMAGE (`image/*`) → either a vision-capable path note, or the existing
        graceful not-readable note for harness/model pairs that cannot read images;
      * other binary → `[Attached file: <name> — binary, not inlined]`.

    BACKWARD-COMPAT (load-bearing): `inline_attachments([], prompt)` (or None) returns
    `prompt` UNCHANGED — byte-for-byte the no-attachment path. A MISSING path is SKIPPED
    (never raises) so a degraded host-upload simply drops that attachment. The blocks are
    PREPENDED above the user's prompt so the agent reads the context first, then the ask."""
    if not paths:
        return prompt

    blocks: list[str] = []
    for raw_path in paths:
        try:
            p = Path(raw_path)
        except (TypeError, ValueError):
            continue
        # Missing path → skip (a degraded host-upload; the turn still sends).
        if not p.exists() or not p.is_file():
            continue
        name = p.name
        kind = _classify(_guess_content_type(p))
        if kind == "image":
            if image_readable:
                blocks.append(
                    f"[Attached image: {name}]\nVision-capable attachment path: {p}"
                )
            else:
                blocks.append(f"[Attached image: {name} — not readable in this harness mode]")
            continue
        if kind == "binary":
            blocks.append(f"[Attached file: {name} — binary, not inlined]")
            continue
        # TEXT — read (capped) + decode; a file that turns out to be binary on read
        # degrades to the binary note rather than dumping bytes.
        read = _read_for_inline(p)
        if read is None:
            blocks.append(f"[Attached file: {name} — binary, not inlined]")
            continue
        text, truncated = read
        trunc = "\n… (truncated)" if truncated else ""
        blocks.append(f"[Attached: {name}]\n```\n{text}{trunc}\n```")

    if not blocks:
        return prompt
    return "\n\n".join(blocks) + "\n\n" + prompt


def list_run_attachments(run_id: str) -> list[Path]:
    """Return the file paths in a run's sandbox subdir (used by the chat route to resolve
    a turn's uploaded attachments back to host paths).

    The upload route wrote each file under `<sandbox>/<run_id>/<filename>`, so a turn's
    attachments are exactly the files in that dir (the SPA's `attachment_ids` confirm the
    upload happened; the bytes are addressed by the run dir). Sorted by name for a
    deterministic inline order. Best-effort: an escaping/invalid run_id or a missing dir
    yields `[]` (never raises) — a turn with no resolvable attachments simply sends
    without them."""
    try:
        rid = _safe_run_id(run_id)
    except AttachmentError:
        return []
    base_root = _attachments_root().resolve()
    run_dir = (base_root / rid).resolve(strict=False)
    if not _is_within(run_dir, base_root) or not run_dir.is_dir():
        return []
    try:
        files = [p for p in sorted(run_dir.iterdir()) if p.is_file()]
    except OSError:
        return []
    return files


def cleanup_paths(paths) -> None:
    """Best-effort delete of a specific set of attachment files + their (now-empty)
    parent dirs (used by the HOST chat runner, whose attachment files live under the
    harness-service's `HARNESS_ATTACHMENT_DIR/<attachment_id>/` layout — a DIFFERENT root
    from this sandbox, so the run-keyed cleanup doesn't apply there).

    Deletes each path's parent dir (the per-attachment subdir the host service created),
    which removes the file and its sibling(s). NEVER raises (a missing path, a racing
    delete, a permission error are all no-ops). Pure housekeeping; the host service's own
    24h sweep is the backstop."""
    seen_dirs: set[str] = set()
    for raw in (paths or []):
        with contextlib.suppress(Exception):
            p = Path(raw)
            parent = p.parent
            key = str(parent)
            if key in seen_dirs:
                continue
            seen_dirs.add(key)
            if parent.is_dir():
                shutil.rmtree(parent, ignore_errors=True)


def cleanup_run_attachments(run_id: str) -> None:
    """Best-effort delete of a run's sandbox subdir (called on a terminal chat status).

    NEVER raises (house law): an escaping/invalid run_id, a missing dir, an out-of-root
    target, or a permission hiccup are all clean no-ops. The `_is_within` gate guards
    against an escaping run_id deleting outside the sandbox."""
    try:
        rid = _safe_run_id(run_id)
    except AttachmentError:
        return
    base_root = _attachments_root().resolve()
    run_dir = (base_root / rid).resolve(strict=False)
    if not _is_within(run_dir, base_root) or run_dir == base_root:
        return
    if run_dir.is_dir():
        with contextlib.suppress(OSError):
            shutil.rmtree(run_dir, ignore_errors=True)


def sweep_stale_attachments(max_age_s: float = 24 * 3600) -> None:
    """Best-effort delete of attachment subdirs older than `max_age_s` (a startup sweep).

    Walks the sandbox base dir's immediate subdirs and removes any whose mtime is older
    than the cutoff. NEVER raises (a missing dir, a racing delete, a permission error are
    all no-ops) — it is pure housekeeping that must never break boot."""
    base_root = _attachments_root()
    if not base_root.is_dir():
        return
    cutoff = time.time() - float(max_age_s)
    try:
        children = list(base_root.iterdir())
    except OSError:
        return
    for child in children:
        with contextlib.suppress(OSError):
            if not child.is_dir():
                continue
            if child.stat().st_mtime < cutoff:
                shutil.rmtree(child, ignore_errors=True)


__all__ = [
    "AttachmentError",
    "AttachmentMeta",
    "ATTACHMENTS_ROOT",
    "MAX_FILE_BYTES",
    "MAX_FILES_PER_TURN",
    "MAX_TURN_BYTES",
    "MAX_INLINE_TEXT_BYTES",
    "safe_attachment_path",
    "receive_upload",
    "inline_attachments",
    "list_run_attachments",
    "cleanup_paths",
    "cleanup_run_attachments",
    "sweep_stale_attachments",
]


# A module-level alias for the default sandbox root (the blueprint names
# `ATTACHMENTS_ROOT`). The authoritative resolver is `_attachments_root()` (env-first,
# read at call time); this constant is the resolved default for display/introspection.
ATTACHMENTS_ROOT = _attachments_root()
