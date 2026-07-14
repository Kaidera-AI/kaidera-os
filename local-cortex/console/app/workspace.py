"""Project-scoped filesystem layer for the Workspace column.

Backs the col-4 workspace file tree + center file pane (read: `list_dir`,
`read_file`) and every workspace MUTATION — the editor SAVE (`write_file`) plus
the file-tree actions ported from the prototype: create file/folder
(`create_file` / `create_dir`), rename (`rename_entry`), move (`move_entry`),
and delete (`delete_entry`). Every operation is *scoped to a single project's
`repo_root`* (the working folder reported by Cortex /projects).

ALL of these — reads AND writes/creates/renames/moves/deletes — funnel through
the SAME `_safe_target` repo_root gate. The gate resolves the FINAL real path
(collapsing `..`, following symlinks) and requires it to be the root itself or a
descendant. A mutation that names a path OUTSIDE the root (`../../tmp/evil`, an
absolute path, an out-of-root symlink) is rejected with WorkspaceError(403) and
NOTHING is created, renamed, moved, or deleted. For ops that take TWO paths
(rename/move), BOTH the source and the destination are gated independently, so
neither end can escape.

SECURITY MODEL (the whole point of this module)
-----------------------------------------------
The public calls (`list_dir`, `read_file`, `write_file`) take a caller-supplied
relative path. That path is attacker-controlled (it arrives off a query string),
so we treat it as hostile:

  1. Resolve the project's `repo_root` to its REAL absolute path (symlinks
     followed) once, up front.
  2. Join the requested rel_path onto it and resolve the RESULT's real path
     (`Path.resolve()` collapses `..` and follows symlinks).
  3. Confirm the resolved target is the root itself or lives *inside* it
     (`is_relative_to`). Anything that escapes — `../../etc/hosts`, an absolute
     path, or a symlink that points outside the worktree — is rejected with
     `WorkspaceError` and never read.
  4. Reject credential files and generated/internal state even when they live
     inside the project root. The workspace is a source editor, not a secret or
     runtime-state browser.

Because we resolve the FINAL path (not just the lexical join), a symlink inside
the worktree that points at `/etc` is caught: its real path is outside the root.

`write_file` (R5b) goes through the EXACT SAME `_safe_target` gate as the reads
before it touches disk, so `../../tmp/evil`, an absolute path, or an out-of-root
symlink is rejected (403) and NOTHING is written. The one extra wrinkle for a
write is that the target may not exist yet (a brand-new file the editor is
saving for the first time), so `write_file` validates the *parent directory*
under the root — that directory must already exist and resolve inside the root.

The project→repo_root mapping is fetched from the same Cortex /projects surface
the rest of the console uses (no new source of truth). Only `status == active`
projects with a real on-disk `repo_root` are addressable.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Read cap for the viewer. Files larger than this are not slurped into the page;
# the viewer shows a "large file" notice instead. 256 KiB is plenty for source
# files / configs while keeping the HTMX payload bounded.
MAX_READ_BYTES = 256 * 1024

# Write cap for the editor SAVE. Bounds how much UTF-8 text a single POST can
# persist (a runaway/garbage body shouldn't be written wholesale). Matches the
# read cap so anything the viewer would show truncated can't be round-tripped.
MAX_WRITE_BYTES = 256 * 1024

# How many bytes we sniff to decide "is this binary?" — a NUL byte in the head
# is the classic, cheap binary signal (matches git's own heuristic).
_BINARY_SNIFF_BYTES = 8192

# In-root paths still need a confidentiality and relevance boundary. These names
# are never listed or addressable through the workspace API, including through
# write/move/rename/delete routes. Keep source-bearing project metadata such as
# `.agents` and `.claude` available; block credentials and generated tool state.
_PROTECTED_NAMES = frozenset(
    {
        ".env",
        ".envrc",
        ".ds_store",
        ".git-credentials",
        ".netrc",
        ".npmrc",
        ".pgpass",
        ".pypirc",
        "credentials.json",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "id_rsa",
        "obscura",
        "obscura-worker",
        "secrets.json",
        "secrets.toml",
        "secrets.yaml",
        "secrets.yml",
    }
)
_PROTECTED_PREFIXES = (".env.",)
_PROTECTED_SUFFIXES = (".jks", ".key", ".keystore", ".p12", ".pem", ".pfx")
_PROTECTED_DIR_NAMES = frozenset(
    {
        ".aws",
        ".azure",
        ".build",
        ".code-review-graph",
        ".docker",
        ".cortex",
        ".dogfood-backup",
        ".git",
        ".gnupg",
        ".kube",
        ".local",
        ".mypy_cache",
        ".nox",
        ".obsidian",
        ".pi",
        ".playwright-cli",
        ".pytest_cache",
        ".ruff_cache",
        ".smart-env",
        ".ssh",
        ".tox",
        ".venv",
        ".worktrees",
        "__pycache__",
        "node_modules",
        "venv",
    }
)


class WorkspaceError(Exception):
    """Raised for any disallowed or impossible workspace access.

    Carries an HTTP-ish `status` so the route layer can map it to a response
    code without leaking internals: 404 (unknown project / missing path),
    403 (path escaped the root — a security rejection), 400 (bad input).
    """

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


@dataclass(frozen=True)
class Entry:
    """One directory entry for the tree (a file or a folder)."""

    name: str
    rel_path: str  # path relative to repo_root, POSIX-style ('a/b/c')
    is_dir: bool
    size: int | None  # bytes for files; None for dirs (or on stat error)


def _resolve_root(repo_root: str | os.PathLike[str]) -> Path:
    """Resolve a project's repo_root to its real absolute path.

    Used as the security anchor — every requested path must resolve to inside
    this. Raises WorkspaceError(404) if the root doesn't exist or isn't a
    directory (a stale/typo'd repo_root in the registry shouldn't 500)."""
    try:
        root = Path(repo_root).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:  # missing, or symlink loop
        raise WorkspaceError(
            f"project root is not accessible on disk: {repo_root}", status=404
        ) from exc
    if not root.is_dir():
        raise WorkspaceError(
            f"project root is not a directory: {repo_root}", status=404
        )
    return root


def _safe_target(root: Path, rel_path: str) -> Path:
    """Resolve `rel_path` *under* `root` and confirm it does not escape.

    This is the security gate. `root` is already a resolved real path. We join
    the (untrusted) rel_path, resolve the combined real path (collapsing `..`
    and following symlinks), and require the result to be the root itself or a
    descendant of it. Absolute rel_paths, `..` escapes, and symlinks pointing
    outside the worktree all fail here.

    Returns the resolved, in-root target Path. Raises WorkspaceError(403) on any
    escape, WorkspaceError(404) if the target doesn't exist.
    """
    rel = (rel_path or "").strip()

    # Normalise leading slashes / backslashes so an absolute-looking input is
    # treated as relative to the root rather than the OS filesystem root. We do
    # NOT trust this normalisation for security (the resolve()+is_relative_to
    # check below is the real gate) — it just makes ordinary input behave.
    rel = rel.replace("\\", "/").lstrip("/")

    # Reject NUL bytes outright (can confuse the filesystem layer).
    if "\x00" in rel:
        raise WorkspaceError("invalid path", status=400)
    _assert_rel_path_allowed(rel)

    candidate = (root / rel) if rel else root

    try:
        target = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        # Distinguish "escaped" from "missing": resolve non-strict to see where
        # it WOULD land, so a probe at ../../etc/hosts is reported as forbidden
        # (403), not merely missing (404). Defence in depth + clearer signal.
        loose = candidate.resolve(strict=False)
        if not _is_within(loose, root):  # fitness:allow-literal false-match: "root" path var, not the agent
            raise WorkspaceError("path escapes project root", status=403) from exc  # fitness:allow-literal false-match: "root" in error prose
        raise WorkspaceError("path not found", status=404) from exc

    if not _is_within(target, root):  # fitness:allow-literal false-match: "root" path var, not the agent
        raise WorkspaceError("path escapes project root", status=403)  # fitness:allow-literal false-match: "root" in error prose

    _assert_target_allowed(root, target)

    return target


def _is_within(path: Path, root: Path) -> bool:
    """True if `path` is `root` or a descendant of it (both already resolved)."""
    if path == root:
        return True
    try:
        # Python 3.9+: Path.is_relative_to. Guarded by a manual fallback below
        # for older runtimes, though the console targets a modern interpreter.
        return path.is_relative_to(root)  # type: ignore[attr-defined]  # fitness:allow-literal false-match: "root" path var, not the agent
    except AttributeError:  # pragma: no cover - very old Python
        try:
            path.relative_to(root)  # fitness:allow-literal false-match: "root" path var, not the agent
            return True
        except ValueError:
            return False


def _is_protected_name(name: str) -> bool:
    """Return whether one path component is unavailable to the workspace."""
    lowered = (name or "").casefold()
    return (
        lowered in _PROTECTED_NAMES
        or lowered in _PROTECTED_DIR_NAMES
        or lowered.startswith(_PROTECTED_PREFIXES)
        or lowered.endswith(_PROTECTED_SUFFIXES)
    )


def _assert_rel_path_allowed(rel_path: str) -> None:
    """Reject protected components in a normalized caller-supplied path."""
    parts = (rel_path or "").replace("\\", "/").strip("/").split("/")
    if any(part not in ("", ".", "..") and _is_protected_name(part) for part in parts):
        raise WorkspaceError("path is not available in the workspace", status=403)


def _assert_target_allowed(root: Path, target: Path) -> None:
    """Reject protected components after resolution, including symlink targets."""
    try:
        parts = target.relative_to(root).parts
    except ValueError:
        return
    if any(_is_protected_name(part) for part in parts):
        raise WorkspaceError("path is not available in the workspace", status=403)


def _entry_sort_key(entry: Entry) -> tuple[int, str]:
    """Dirs first, then case-insensitive lexical order by name."""
    return (0 if entry.is_dir else 1, entry.name.lower())


def list_dir(
    repo_root: str | os.PathLike[str],
    rel_path: str = "",
) -> list[Entry]:
    """List one directory under the project root.

    Returns entries (files + folders) directly inside `repo_root/rel_path`,
    including source-bearing hidden paths such as `.agents` and `.claude`, but
    excluding credentials and generated/internal tool state. Results are sorted
    directories-first then case-insensitive lexical order.

    `repo_root` is the project's working folder (from Cortex /projects).
    `rel_path` is the (untrusted) sub-path to list; "" lists the root.

    Raises WorkspaceError on an unknown/inaccessible root, a path that escapes
    the root (403), a missing path (404), or a non-directory target (400).
    """
    root = _resolve_root(repo_root)
    target = _safe_target(root, rel_path)

    if not target.is_dir():
        raise WorkspaceError("not a directory", status=400)

    entries: list[Entry] = []
    try:
        with os.scandir(target) as it:
            for de in it:
                if _is_protected_name(de.name):
                    continue
                # Determine dir-ness without following a dangling symlink into a
                # crash. follow_symlinks=False means a symlink-to-dir shows as a
                # file row here; that's fine and safe (clicking it would re-run
                # the security gate, which rejects out-of-root symlink targets).
                try:
                    is_dir = de.is_dir(follow_symlinks=False)
                except OSError:
                    is_dir = False
                size: int | None = None
                if not is_dir:
                    try:
                        size = de.stat(follow_symlinks=False).st_size
                    except OSError:
                        size = None
                name = de.name
                child_rel = f"{rel_path.strip('/')}/{name}".lstrip("/") if rel_path else name
                # Normalise the stored rel path to POSIX form.
                child_rel = child_rel.replace("\\", "/")
                entries.append(
                    Entry(name=name, rel_path=child_rel, is_dir=is_dir, size=size)
                )
    except OSError as exc:
        raise WorkspaceError(f"cannot read directory: {exc}", status=403) from exc

    entries.sort(key=_entry_sort_key)
    return entries


def read_file(
    repo_root: str | os.PathLike[str],
    rel_path: str,
) -> dict[str, Any]:
    """Read a text file under the project root for the viewer.

    Returns a dict the viewer template renders directly:
        {
          "rel_path": str,         # the (normalised) path shown in the header
          "name": str,             # basename
          "size": int,             # bytes on disk
          "binary": bool,          # True → content is None, show a notice
          "truncated": bool,       # True → content is the first MAX_READ_BYTES
          "content": str | None,   # decoded text, or None for binary
          "lines": int | None,     # line count for the gutter (text only)
        }

    Caps reads at MAX_READ_BYTES and flags non-decodable (binary) files instead
    of dumping bytes into the page. Raises WorkspaceError on escape (403),
    missing/!file (404), or a directory target (400).
    """
    root = _resolve_root(repo_root)
    target = _safe_target(root, rel_path)

    if target.is_dir():
        raise WorkspaceError("path is a directory, not a file", status=400)
    if not target.is_file():
        raise WorkspaceError("not a regular file", status=404)

    try:
        size = target.stat().st_size
    except OSError as exc:
        raise WorkspaceError("cannot stat file", status=403) from exc

    norm_rel = (rel_path or "").replace("\\", "/").strip("/") or target.name
    base = {
        "rel_path": norm_rel,
        "name": target.name,
        "size": size,
    }

    # Read up to the cap (+1 byte so we can tell "exactly at cap" from "over").
    try:
        with open(target, "rb") as fh:
            raw = fh.read(MAX_READ_BYTES + 1)
    except OSError as exc:
        raise WorkspaceError("cannot read file", status=403) from exc

    truncated = len(raw) > MAX_READ_BYTES
    if truncated:
        raw = raw[:MAX_READ_BYTES]

    # Binary sniff: a NUL in the head means "not text" (git's heuristic).
    if b"\x00" in raw[:_BINARY_SNIFF_BYTES]:
        return {**base, "binary": True, "truncated": truncated, "content": None, "lines": None}

    # Decode as UTF-8; fall back to declaring it binary if it won't decode
    # cleanly (covers latin-1-only blobs, partial multibyte at the cut, etc.).
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return {**base, "binary": True, "truncated": truncated, "content": None, "lines": None}

    line_count = text.count("\n") + (1 if text and not text.endswith("\n") else 0)
    return {
        **base,
        "binary": False,
        "truncated": truncated,
        "content": text,
        "lines": line_count or (1 if text else 0),
    }


def write_file(
    repo_root: str | os.PathLike[str],
    rel_path: str,
    content: str,
) -> dict[str, Any]:
    """Write UTF-8 `content` to a file under the project root (R5b editor SAVE).

    The ONLY write path in this module. Security is the same `_safe_target`
    repo_root gate the reads use — an escaping `rel_path` (`../../tmp/evil`, an
    absolute path, an out-of-root symlink) is rejected with WorkspaceError(403)
    and NOTHING is written.

    Because the target may not exist yet (saving a brand-new file), we run the
    gate against the target's PARENT directory (which must already exist and
    resolve inside the root dir), then re-confirm the final path is in-root before
    opening it for write. We refuse to overwrite a directory, refuse paths that
    name no file (a bare "" or trailing slash), and cap the body at
    MAX_WRITE_BYTES (measured as encoded UTF-8 bytes).

    Returns {"rel_path", "name", "size", "lines"} describing what was persisted
    so the route can render a save confirmation. Raises WorkspaceError on escape
    (403), a missing parent dir (404), a bad/empty path or oversized body (400),
    or a write failure (403).
    """
    root = _resolve_root(repo_root)

    # Normalise the requested path the same way the gate does, so we can split
    # off the basename and validate the parent. (The authoritative escape check
    # is still _safe_target below — this normalisation only shapes the input.)
    rel = (rel_path or "").strip().replace("\\", "/").lstrip("/")
    if "\x00" in rel:
        raise WorkspaceError("invalid path", status=400)
    rel = rel.rstrip("/")  # a trailing slash names a dir, not a file
    if not rel:
        raise WorkspaceError("no file path given", status=400)

    parent_rel, _, name = rel.rpartition("/")
    if not name or name in (".", ".."):
        raise WorkspaceError("invalid file path", status=400)

    # Gate the PARENT directory through the same security check as reads. This
    # resolves `..`/symlinks and confirms the parent is the root or inside it;
    # an escaping path fails here (403) before any disk write. The parent must
    # already exist (strict resolve) — we don't mkdir new trees from the editor.
    parent_dir = _safe_target(root, parent_rel)
    if not parent_dir.is_dir():
        raise WorkspaceError("parent path is not a directory", status=400)

    target = parent_dir / name

    # Defence in depth: re-resolve the final target (non-strict, since it may be
    # new) and require it to stay inside the root. Catches a `name` that is a
    # symlink pointing out of the worktree, or any residual escape.
    final = target.resolve(strict=False)
    if not _is_within(final, root):  # fitness:allow-literal false-match: "root" path var, not the agent
        raise WorkspaceError("path escapes project root", status=403)  # fitness:allow-literal false-match: "root" in error prose
    _assert_target_allowed(root, final)

    if target.is_dir():
        raise WorkspaceError("path is a directory, not a file", status=400)

    # Cap the encoded size (what actually lands on disk).
    encoded = (content or "").encode("utf-8")
    if len(encoded) > MAX_WRITE_BYTES:
        raise WorkspaceError(
            f"file too large to save ({len(encoded)} bytes; "
            f"max {MAX_WRITE_BYTES})",
            status=400,
        )

    try:
        # Write bytes (newline-preserving; no universal-newline translation) so
        # the saved content is byte-for-byte what the editor sent.
        with open(target, "wb") as fh:
            fh.write(encoded)
    except OSError as exc:
        raise WorkspaceError(f"cannot write file: {exc}", status=403) from exc

    norm_rel = rel
    line_count = (content or "").count("\n") + (
        1 if content and not content.endswith("\n") else 0
    )
    return {
        "rel_path": norm_rel,
        "name": name,
        "size": len(encoded),
        "lines": line_count or (1 if content else 0),
    }


# ---------------------------------------------------------------------------
#  File-tree MUTATIONS (create / rename / move / delete) — prototype actions.
#  Each one runs the requested rel_path(s) through `_safe_target` before it
#  touches disk; an escaping path is rejected 403 and nothing happens.
# ---------------------------------------------------------------------------

# Component names we never allow a caller to create/rename to (path traversal
# tokens and the empty name). `.` / `..` are the dangerous ones; the gate would
# catch an escape anyway, but rejecting up-front gives a clean 400.
_BAD_NAMES = {"", ".", ".."}


def _validate_name(name: str) -> str:
    """Validate a SINGLE path component for create/rename (not a sub-path).

    A new file/folder name (or a rename target) must be one path segment: no
    slashes, no NUL, not '.'/'..'. Returns the cleaned name. Raises
    WorkspaceError(400) otherwise. (The repo_root gate is still the real escape
    guard — this just stops a 'name' from secretly being a path.)"""
    nm = (name or "").strip()
    if "\x00" in nm:
        raise WorkspaceError("invalid name", status=400)
    if "/" in nm or "\\" in nm:
        raise WorkspaceError("name may not contain a path separator", status=400)
    if nm in _BAD_NAMES:
        raise WorkspaceError("invalid name", status=400)
    return nm


def _gated_parent(root: Path, rel: str) -> tuple[Path, str]:
    """Split `rel` into (gated parent dir, basename) under root.

    Normalises `rel`, pulls off the last component as the basename, and runs the
    PARENT through `_safe_target` (so the parent must exist + resolve in-root).
    Returns the resolved parent Path and the basename. Used by create/rename/move
    so the new/target leaf lands in a verified in-root directory."""
    rel = (rel or "").strip().replace("\\", "/").strip("/")
    if "\x00" in rel:
        raise WorkspaceError("invalid path", status=400)
    if not rel:
        raise WorkspaceError("no path given", status=400)
    parent_rel, _, name = rel.rpartition("/")
    name = _validate_name(name)
    parent_dir = _safe_target(root, parent_rel)
    if not parent_dir.is_dir():
        raise WorkspaceError("parent path is not a directory", status=400)
    return parent_dir, name


def _ensure_in_root(root: Path, target: Path) -> None:
    """Re-resolve `target` (it may not exist) and require it inside root (403)."""
    final = target.resolve(strict=False)
    if not _is_within(final, root):  # fitness:allow-literal false-match: "root" path var, not the agent
        raise WorkspaceError("path escapes project root", status=403)  # fitness:allow-literal false-match: "root" in error prose
    _assert_target_allowed(root, final)


def _entry_result(root: Path, target: Path) -> dict[str, Any]:
    """Small {rel_path, name, is_dir} descriptor the routes echo back to the UI."""
    try:
        rel = target.resolve(strict=False).relative_to(root).as_posix()  # fitness:allow-literal false-match: "root" path var, not the agent
    except ValueError:  # pragma: no cover - target already gated in-root
        rel = target.name
    return {"rel_path": rel, "name": target.name, "is_dir": target.is_dir()}


def create_file(
    repo_root: str | os.PathLike[str], rel_path: str
) -> dict[str, Any]:
    """Create a NEW empty file at `rel_path` under the project root.

    Goes through the `_safe_target` gate (via `_gated_parent`): the parent dir
    must already exist and resolve in-root; an escaping path is rejected 403 and
    nothing is written. Refuses to clobber an existing file/dir (409).

    Returns {rel_path, name, is_dir:False}."""
    root = _resolve_root(repo_root)
    parent_dir, name = _gated_parent(root, rel_path)
    target = parent_dir / name
    _ensure_in_root(root, target)
    if target.exists():
        raise WorkspaceError("a file or folder with that name already exists", status=409)
    try:
        # x mode: create-exclusive, never truncate an existing file.
        with open(target, "xb"):
            pass
    except FileExistsError as exc:
        raise WorkspaceError("already exists", status=409) from exc
    except OSError as exc:
        raise WorkspaceError(f"cannot create file: {exc}", status=403) from exc
    return _entry_result(root, target)


def create_dir(
    repo_root: str | os.PathLike[str], rel_path: str
) -> dict[str, Any]:
    """Create a NEW folder at `rel_path` under the project root.

    Same `_safe_target` parent gate as `create_file`; an escaping path is 403.
    Refuses to clobber an existing entry (409). Returns {rel_path, name,
    is_dir:True}."""
    root = _resolve_root(repo_root)
    parent_dir, name = _gated_parent(root, rel_path)
    target = parent_dir / name
    _ensure_in_root(root, target)
    if target.exists():
        raise WorkspaceError("a file or folder with that name already exists", status=409)
    try:
        target.mkdir(parents=False, exist_ok=False)
    except FileExistsError as exc:
        raise WorkspaceError("already exists", status=409) from exc
    except OSError as exc:
        raise WorkspaceError(f"cannot create folder: {exc}", status=403) from exc
    return _entry_result(root, target)


def rename_entry(
    repo_root: str | os.PathLike[str], rel_path: str, new_name: str
) -> dict[str, Any]:
    """Rename the file/folder at `rel_path` to `new_name` (same parent).

    `rel_path` (the existing entry) is gated by `_safe_target` (must exist
    in-root); `new_name` is a single validated component placed in the SAME
    parent. The destination is re-checked in-root. Escapes → 403; a missing
    source → 404; an existing destination → 409.

    Returns {rel_path, name, is_dir} for the renamed entry."""
    root = _resolve_root(repo_root)
    src = _safe_target(root, rel_path)  # must exist + be in-root
    name = _validate_name(new_name)
    dest = src.parent / name
    _ensure_in_root(root, dest)
    if dest == src:
        return _entry_result(root, src)  # no-op rename
    if dest.exists():
        raise WorkspaceError("a file or folder with that name already exists", status=409)
    try:
        src.rename(dest)
    except OSError as exc:
        raise WorkspaceError(f"cannot rename: {exc}", status=403) from exc
    return _entry_result(root, dest)


def move_entry(
    repo_root: str | os.PathLike[str], src_rel: str, dest_dir_rel: str
) -> dict[str, Any]:
    """Move the entry at `src_rel` INTO the directory `dest_dir_rel`.

    BOTH paths are gated by `_safe_target` independently — the source must exist
    in-root, and the destination directory must exist + be a dir in-root. The
    final landing path (dest_dir/basename) is re-checked in-root. Refuses to move
    a directory into itself or its own descendant (400), and refuses to clobber
    (409). `dest_dir_rel` == "" moves the entry to the repo root.

    Returns {rel_path, name, is_dir} for the moved entry at its new home."""
    root = _resolve_root(repo_root)
    src = _safe_target(root, src_rel)  # must exist + in-root
    # dest dir: "" → the root itself; otherwise gated like any path.
    dest_dir = _safe_target(root, dest_dir_rel) if (dest_dir_rel or "").strip().strip("/") else root
    if not dest_dir.is_dir():
        raise WorkspaceError("destination is not a directory", status=400)

    # Guard: don't move a directory into itself or a descendant of itself.
    if src.is_dir():
        if dest_dir == src or _is_within(dest_dir, src):
            raise WorkspaceError("cannot move a folder into itself", status=400)

    dest = dest_dir / src.name
    _ensure_in_root(root, dest)
    if dest == src:
        return _entry_result(root, src)  # already there — no-op
    if dest.exists():
        raise WorkspaceError("destination already has an entry with that name", status=409)
    try:
        # shutil.move handles cross-device + dir/file; both ends are gated above.
        shutil.move(str(src), str(dest))
    except OSError as exc:
        raise WorkspaceError(f"cannot move: {exc}", status=403) from exc
    return _entry_result(root, dest)


def delete_entry(
    repo_root: str | os.PathLike[str], rel_path: str
) -> dict[str, Any]:
    """Delete the file or folder at `rel_path` under the project root.

    Gated by `_safe_target` (must exist in-root); an escaping path is 403 and
    nothing is removed. Refuses to delete the project ROOT itself (403). A folder
    is removed recursively (the prototype's delete acted on a whole subtree).

    Returns {rel_path, name, is_dir} describing what was removed."""
    root = _resolve_root(repo_root)
    target = _safe_target(root, rel_path)
    if target == root:
        raise WorkspaceError("refusing to delete the project root", status=403)  # fitness:allow-literal false-match: "root" in error prose
    info = _entry_result(root, target)
    try:
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        else:
            # files and symlinks: unlink the link/file itself (never follow).
            target.unlink()
    except OSError as exc:
        raise WorkspaceError(f"cannot delete: {exc}", status=403) from exc
    return info


# ---------------------------------------------------------------------------
#  Excalidraw scene decode — turn an .excalidraw(.md) file into a real scene
#  (the element list with full geometry), so the center pane can render the
#  ACTUAL drawing as SVG instead of a static mock.
# ---------------------------------------------------------------------------

def _decompress_obsidian(blob: str) -> str | None:
    """Decompress an Obsidian Excalidraw ```compressed-json``` payload.

    Obsidian's Excalidraw plugin stores the scene as `LZString.compressToBase64`
    of the scene JSON. We strip ALL whitespace (the plugin hard-wraps the base64
    at ~76 cols) and decompress. Returns the JSON string, or None if lzstring
    isn't importable or the payload won't decompress."""
    try:
        import lzstring  # optional dep; declared in requirements
    except Exception:  # pragma: no cover - lib missing
        return None
    packed = re.sub(r"\s+", "", blob or "")
    if not packed:
        return None
    try:
        out = lzstring.LZString().decompressFromBase64(packed)
    except Exception:
        return None
    return out or None


def parse_excalidraw(content: str | None) -> dict | None:
    """Parse an `.excalidraw` / `.excalidraw.md` body into a renderable scene.

    Handles all three on-disk shapes we see:
      * raw `.excalidraw` — the whole file is the scene JSON `{type, elements…}`
      * Obsidian `.excalidraw.md` with a ```compressed-json``` block (LZ-String)
      * legacy `.excalidraw.md` with an uncompressed ```json``` scene block

    Returns {"elements": list, "count": int, "source": str} where `elements` is
    the live element list (rectangles/arrows/text with x/y/width/height so the
    SVG renderer can draw them) — or None when no usable scene JSON is found
    (the caller then falls back to showing the raw source)."""
    if not content:
        return None

    blobs: list[tuple[str, str]] = []  # (source-label, json-text)

    # Obsidian compressed block first — it's the authoritative, current scene.
    m = re.search(r"```compressed-json\s*(.*?)\s*```", content, re.DOTALL)
    if m:
        dec = _decompress_obsidian(m.group(1))
        if dec:
            blobs.append(("compressed-json", dec))

    # Legacy / explicit uncompressed JSON scene block.
    for jm in re.finditer(r"```json\s*(\{.*?\})\s*```", content, re.DOTALL):
        blobs.append(("json-block", jm.group(1)))

    # Raw .excalidraw (whole file is the object).
    stripped = content.lstrip()
    if stripped.startswith("{"):
        blobs.append(("raw", content))

    # Last resort: the widest brace-balanced slice.
    first = content.find("{")
    last = content.rfind("}")
    if first != -1 and last > first:
        blobs.append(("slice", content[first : last + 1]))

    for label, blob in blobs:
        try:
            obj = json.loads(blob)
        except ValueError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("elements"), list):
            els = [e for e in obj["elements"] if isinstance(e, dict)]
            return {"elements": els, "count": len(els), "source": label}
    return None


# ---------------------------------------------------------------------------
#  Excalidraw LIVE round-trip — the Obsidian `.excalidraw.md` <-> scene-JSON
#  bridge that backs the live Excalidraw canvas editor in the center pane.
#
#  read_excalidraw_scene(path)  → parse an .excalidraw / .excalidraw.md file into
#                                 the full scene object the Excalidraw React
#                                 component loads (elements + appState + files),
#                                 PLUS the surrounding markdown wrapper so a save
#                                 can put the edited scene back without disturbing
#                                 the front-matter / Text-Elements section.
#  write_excalidraw_scene(path, scene) → serialise an edited scene back into the
#                                 SAME Obsidian markdown wrapper (replacing ONLY
#                                 the drawing block) so Obsidian still opens it.
#
#  BOTH go through `_safe_target` (via read_file / write_file) — they NEVER touch
#  disk outside the repo_root gate.
#
#  FORMAT NOTE / limitation: Obsidian's plugin stores the live scene LZ-String
#  *compressed* in a ```compressed-json``` block. We can READ that (lzstring), but
#  to avoid a JS/Python compressor dependency on the write path we serialise back
#  as the plugin's *uncompressed* ```json``` form under `## Drawing`. Obsidian's
#  Excalidraw plugin reads BOTH forms (uncompressed is its documented "legacy /
#  compatibility" on-disk shape), so the file still opens; it simply isn't
#  re-compressed. The front-matter, the `# Excalidraw Data` heading, and the
#  `## Text Elements` block above `## Drawing` are preserved verbatim.
# ---------------------------------------------------------------------------

# The marker the Obsidian Excalidraw plugin uses to fence the embedded scene off
# from the human-readable part of the note. Everything from this line down (the
# `## Drawing` heading + its fenced block) is the machine-owned drawing payload.
_EXCAL_DRAWING_HEADING = "## Drawing"

# A minimal, valid empty Excalidraw scene (used when a brand-new .excalidraw.md
# has no decodable drawing yet, so the canvas still mounts on something).
_EMPTY_SCENE: dict[str, Any] = {
    "type": "excalidraw",
    "version": 2,
    "source": "https://excalidraw.com",
    "elements": [],
    "appState": {"gridSize": None, "viewBackgroundColor": "#ffffff"},
    "files": {},
}


def _split_excalidraw_wrapper(content: str) -> tuple[str, str | None]:
    """Split an Obsidian `.excalidraw.md` body into (prefix, drawing_section).

    `prefix` is everything ABOVE the `## Drawing` heading (front-matter, the
    `# Excalidraw Data` heading, the `## Text Elements` block, the `%%` comment
    fence) — preserved verbatim on save. `drawing_section` is the `## Drawing`
    heading and everything after it (the fenced scene), or None if the file has
    no `## Drawing` section (a raw `.excalidraw`, or a not-yet-Obsidian file).

    Matching is line-anchored on `## Drawing` so a literal "## Drawing" inside a
    text element can't trip it (the plugin always emits it at column 0)."""
    if not content:
        return "", None
    # Find the line-anchored `## Drawing` heading.
    m = re.search(r"(?m)^[ \t]*##[ \t]+Drawing[ \t]*$", content)
    if not m:
        return content, None
    return content[: m.start()], content[m.start():]


def _scene_payload(scene: dict | None) -> dict[str, Any]:
    """Normalise an arbitrary scene-ish dict into a clean Excalidraw scene object.

    Keeps the canonical top-level keys the component round-trips (`type`,
    `version`, `source`, `elements`, `appState`, `files`), drops everything else,
    and guarantees `elements` is a list and `appState`/`files` are objects so the
    serialised JSON is always a loadable scene."""
    base = dict(_EMPTY_SCENE)
    if isinstance(scene, dict):
        if isinstance(scene.get("elements"), list):
            base["elements"] = [e for e in scene["elements"] if isinstance(e, dict)]
        if isinstance(scene.get("appState"), dict):
            # appState carries transient UI bits (collaborators, cursors); keep it
            # but never let a non-dict through.
            base["appState"] = scene["appState"]
        if isinstance(scene.get("files"), dict):
            base["files"] = scene["files"]
        if isinstance(scene.get("type"), str):
            base["type"] = scene["type"]
        if scene.get("version") is not None:
            base["version"] = scene["version"]
        if isinstance(scene.get("source"), str):
            base["source"] = scene["source"]
    return base


def _build_obsidian_markdown(prefix: str | None, scene: dict) -> str:
    """Rebuild an Obsidian `.excalidraw.md` body: `prefix` + an uncompressed
    `## Drawing` JSON block holding `scene`.

    `prefix` is the verbatim text above the old `## Drawing` heading (front-matter
    + Text Elements). If `prefix` is None/empty (a brand-new file or a raw
    `.excalidraw` being upgraded), a minimal Obsidian front-matter + heading
    scaffold is emitted so the result is a valid Obsidian Excalidraw note.

    Only the drawing block is (re)written; the human-readable section is kept."""
    payload = _scene_payload(scene)
    # Pretty-print (sorted keys off; stable, diff-friendly 2-space indent) so the
    # embedded scene is human-inspectable in plain Markdown too.
    scene_json = json.dumps(payload, ensure_ascii=False, indent=2)

    pre = prefix if (prefix and prefix.strip()) else (
        "---\n\nexcalidraw-plugin: parsed\ntags: [excalidraw]\n\n---\n"
        "==⚠  Switch to EXCALIDRAW VIEW in the MORE OPTIONS menu of this "
        "document. ⚠==\n\n# Excalidraw Data\n\n## Text Elements\n%%\n"
    )
    # Ensure exactly one newline boundary between the preserved prefix and the
    # drawing heading (the plugin keeps `%%` then `## Drawing` on its own line).
    if not pre.endswith("\n"):
        pre += "\n"

    return f"{pre}{_EXCAL_DRAWING_HEADING}\n```json\n{scene_json}\n```\n"


def read_excalidraw_scene(
    repo_root: str | os.PathLike[str],
    rel_path: str,
) -> dict[str, Any]:
    """Read an `.excalidraw` / `.excalidraw.md` file into a live editor payload.

    Funnels through `read_file` (hence the SAME `_safe_target` repo_root gate —
    an escaping path is rejected 403, NOTHING is read). Returns:

        {
          "rel_path": str,
          "name": str,
          "scene": {type, version, source, elements, appState, files},
          "count": int,            # element count (decoded)
          "source": str | None,    # which block the scene came from, or None
          "prefix": str | None,    # verbatim markdown above `## Drawing`
          "is_markdown": bool,      # True for .excalidraw.md (Obsidian wrapper)
          "raw": str,              # the full original file content (for the JS)
        }

    When the file has no decodable scene, `scene` is a valid EMPTY scene so the
    canvas still mounts (the user can draw + save). Raises WorkspaceError on
    escape (403), missing/!file (404), or a binary/oversized file (the read cap)."""
    data = read_file(repo_root, rel_path)  # gated; raises on escape/missing
    if data.get("binary"):
        raise WorkspaceError("not a text Excalidraw file", status=415)
    content = data.get("content") or ""

    parsed = parse_excalidraw(content)
    if parsed and parsed.get("elements") is not None:
        # Re-extract the FULL scene object (parse_excalidraw only returns
        # elements/count); we want appState + files too for a faithful canvas.
        scene = _full_scene_from_content(content) or _scene_payload(
            {"elements": parsed["elements"]}
        )
        count = parsed["count"]
        source = parsed["source"]
    else:
        scene = dict(_EMPTY_SCENE)
        count = 0
        source = None

    low = (rel_path or "").lower()
    is_md = low.endswith(".excalidraw.md")
    prefix, _drawing = _split_excalidraw_wrapper(content) if is_md else (None, None)

    return {
        "rel_path": data["rel_path"],
        "name": data["name"],
        "scene": scene,
        "count": count,
        "source": source,
        "prefix": prefix,
        "is_markdown": is_md,
        "raw": content,
    }


def _full_scene_from_content(content: str) -> dict[str, Any] | None:
    """Like parse_excalidraw, but return the WHOLE scene object (with appState +
    files), normalised — not just the element list. Returns None if no scene."""
    if not content:
        return None
    candidates: list[str] = []
    m = re.search(r"```compressed-json\s*(.*?)\s*```", content, re.DOTALL)
    if m:
        dec = _decompress_obsidian(m.group(1))
        if dec:
            candidates.append(dec)
    for jm in re.finditer(r"```json\s*(\{.*?\})\s*```", content, re.DOTALL):
        candidates.append(jm.group(1))
    stripped = content.lstrip()
    if stripped.startswith("{"):
        candidates.append(content)
    first = content.find("{")
    last = content.rfind("}")
    if first != -1 and last > first:
        candidates.append(content[first : last + 1])
    for blob in candidates:
        try:
            obj = json.loads(blob)
        except ValueError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("elements"), list):
            return _scene_payload(obj)
    return None


def write_excalidraw_scene(
    repo_root: str | os.PathLike[str],
    rel_path: str,
    scene: dict,
) -> dict[str, Any]:
    """Serialise an edited `scene` back into the file at `rel_path` (the SAVE).

    Funnels through `write_file` (hence the SAME `_safe_target` repo_root gate —
    an escaping path is rejected 403 and NOTHING is written).

    For an `.excalidraw.md` (Obsidian) file we RE-READ the current on-disk content
    to recover the verbatim prefix (front-matter + Text Elements) and replace ONLY
    the `## Drawing` block with the new scene as uncompressed JSON, so Obsidian
    still opens the note. For a raw `.excalidraw` file we write the bare scene
    JSON. Returns the `write_file` result dict ({rel_path, name, size, lines})."""
    payload = _scene_payload(scene)
    low = (rel_path or "").lower()

    if low.endswith(".excalidraw.md"):
        # Recover the human-readable prefix from the CURRENT file (gated read).
        prefix: str | None = None
        try:
            existing = read_file(repo_root, rel_path)
            if not existing.get("binary"):
                prefix, _ = _split_excalidraw_wrapper(existing.get("content") or "")
        except WorkspaceError:
            # New file (or unreadable) → _build_obsidian_markdown scaffolds one.
            prefix = None
        body = _build_obsidian_markdown(prefix, payload)
    else:
        # Raw `.excalidraw` — the whole file IS the scene JSON.
        body = json.dumps(payload, ensure_ascii=False, indent=2)

    return write_file(repo_root, rel_path, body)  # gated; raises on escape
