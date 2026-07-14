"""Explain context assembler — gather the source material the LLM explains (HOST-side).

The Explain capability turns a code TARGET (a file, a function's blast radius, a
directory, or a git diff) into a self-contained visual HTML explainer. Generation is
HOST-side (the containerized console can't read repo files or run `cortex-graph-*`; the
host harness-service can) — so this module runs on the host inside the `run-explain`
process, alongside the repo + the `cortex-graph-blast` CLI.

`assemble(target, popen=subprocess.run) -> (context_text, provenance)` dispatches on
`target["kind"]`:
  * `file`  → `_assemble_file`  : read `<repo>/<path>` (cap 40k chars).
  * `blast` → `_assemble_blast` : `cortex-graph-blast --target <fn> --repo <repo>` (cap 8k).
  * `dir`   → `_assemble_dir`   : walk *.py/*.ts/*.tsx, ~300-char snippets (cap 20k / 60 files).
  * `diff`  → `_assemble_diff`  : `git -C <repo> diff <rev>..HEAD` (or `diff HEAD`) (cap 24k).
  * `project` → `_assemble_project`: repo architecture brief + source map.

SECURITY (file/dir reads): a caller-supplied `path` is untrusted, so `_is_within`
(a LOCAL COPY of `workspace._is_within` — copied, NOT imported, so this host module
carries no console dependency) confines every read under the repo root: an absolute path
or a `..` escape is REJECTED before any read. The graph-blast/git lanes shell a fixed,
argv-list command (no shell → no injection) with the repo as `--repo`/`-C`.

ROBUSTNESS: each shell runs with a 30s timeout; a timeout (or any subprocess failure)
yields a short truncated PLACEHOLDER rather than crashing — assembling context must
NEVER break the run (the LLM still gets something, and the run records the degrade in
its provenance). Every cap truncates with a visible marker so the LLM (and a human
reading the artifact) can see the content was clipped.

`popen` is injected (defaults to `subprocess.run`) so tests drive every lane with a fake
that returns canned stdout / raises / times out — no live graph CLI, no git, no network.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any, Callable

# ---------------------------------------------------------------------------
#  Caps (config-as-data; env-overridable, never a per-project literal).
# ---------------------------------------------------------------------------

# Per-lane character caps for the assembled context. Bounds the prompt the LLM sees so a
# huge file/diff can't blow the context window or the cost. Each truncation is marked.
_FILE_MAX_CHARS = int(os.environ.get("EXPLAIN_FILE_MAX_CHARS", "40000"))
_BLAST_MAX_CHARS = int(os.environ.get("EXPLAIN_BLAST_MAX_CHARS", "8000"))
_DIR_MAX_CHARS = int(os.environ.get("EXPLAIN_DIR_MAX_CHARS", "20000"))
_DIFF_MAX_CHARS = int(os.environ.get("EXPLAIN_DIFF_MAX_CHARS", "24000"))
_PROJECT_MAX_CHARS = int(os.environ.get("EXPLAIN_PROJECT_MAX_CHARS", "32000"))

# Directory walk bounds: how many files, and how much of each (a short snippet so the
# LLM gets a structural overview of a package, not every line of every file).
_DIR_MAX_FILES = int(os.environ.get("EXPLAIN_DIR_MAX_FILES", "60"))
_DIR_SNIPPET_CHARS = int(os.environ.get("EXPLAIN_DIR_SNIPPET_CHARS", "300"))

# The file extensions the directory lane includes (source files worth explaining).
_DIR_EXTENSIONS = (".py", ".ts", ".tsx")
_PROJECT_SOURCE_EXTENSIONS = (
    ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
    ".sql", ".sh", ".md", ".json", ".toml", ".yaml", ".yml",
)
_PROJECT_SKIP_DIRS = {
    ".git", ".hg", ".svn", "__pycache__", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", ".venv", "venv", "node_modules", "dist", "build",
    "coverage", ".next", ".turbo",
}
_PROJECT_SIGNAL_FILES = (
    "README.md",
    "AGENTS.md",
    "pyproject.toml",
    "package.json",
    "Dockerfile",
    "docker-compose.yml",
    "compose.yml",
)
_PROJECT_SIGNAL_SNIPPET_CHARS = int(os.environ.get("EXPLAIN_PROJECT_SIGNAL_SNIPPET_CHARS", "1400"))
_PROJECT_MAX_FILES = int(os.environ.get("EXPLAIN_PROJECT_MAX_FILES", "240"))

# Per-shell wall-clock cap. A hung graph-blast / git must not block the run; on a
# timeout we record a placeholder and move on.
_SHELL_TIMEOUT_S = float(os.environ.get("EXPLAIN_SHELL_TIMEOUT_S", "30"))

# The visible marker appended when a cap clips the content.
_TRUNCATION_MARKER = "\n\n…[truncated by Explain — content exceeded the cap]…\n"


def _graph_blast_script() -> str:
    """Resolve the `cortex-graph-blast` script path — env-overridable
    (`ORCH_GRAPH_BLAST`), defaulting to the sibling `.agents/scripts/cortex-graph-blast`
    derived from THIS file's location (console/app → console → local-cortex → repo root →
    .agents/scripts). Never a hardcoded personal path (the no-project-literals gate)."""
    override = os.environ.get("ORCH_GRAPH_BLAST", "").strip()
    if override:
        return override
    repo_root = Path(__file__).resolve().parents[3]
    return str(repo_root / ".agents" / "scripts" / "cortex-graph-blast")


# ---------------------------------------------------------------------------
#  Confinement (LOCAL COPY of workspace._is_within — host module, no import).
# ---------------------------------------------------------------------------

def _is_within(path: Path, base_dir: Path) -> bool:
    """True if `path` is `base_dir` or a descendant of it (both already resolved). A LOCAL
    COPY of `workspace._is_within` — this host module is standalone, so it carries its
    own copy rather than importing the console workspace layer."""
    if path == base_dir:
        return True
    try:
        return path.is_relative_to(base_dir)  # type: ignore[attr-defined]
    except AttributeError:  # pragma: no cover - very old Python
        try:
            path.relative_to(base_dir)
            return True
        except ValueError:
            return False


def _confined_target(repo: str, rel_path: str) -> Path | None:
    """Resolve `<repo>/<rel_path>` and confirm it stays inside the repo root.

    The security gate for the file/dir lanes: rejects an absolute path or a `..` escape
    BEFORE any read (returns None — the caller records a rejection placeholder). Mirrors
    `workspace._safe_target` but is read-only + returns None instead of raising (this
    host path must degrade, never crash)."""
    rel = (rel_path or "").strip()
    if "\x00" in rel:
        return None
    # Reject an absolute-looking path outright (don't silently treat it as repo-relative;
    # an explicit reject is the safe, auditable behaviour for an untrusted input).
    if rel.startswith("/") or rel.startswith("\\"):
        return None
    try:
        repo_dir = Path(repo).expanduser().resolve(strict=False)
    except (OSError, RuntimeError):
        return None
    candidate = (repo_dir / rel) if rel else repo_dir
    try:
        target = candidate.resolve(strict=False)
    except (OSError, RuntimeError):
        return None
    if not _is_within(target, repo_dir):
        return None
    return target


def _truncate(text: str, cap: int) -> tuple[str, bool]:
    """Clip `text` to `cap` chars, appending the visible truncation marker if clipped.
    Returns (possibly-clipped text, was_truncated)."""
    if len(text) <= cap:
        return text, False
    return text[:cap] + _TRUNCATION_MARKER, True


def _run_shell(
    argv: list[str], popen: Callable[..., Any], *, cwd: str | None = None
) -> tuple[str, str | None]:
    """Run `argv` (a LIST — no shell, no injection) with the per-shell timeout and return
    (stdout, error). On success → (stdout, None); on a timeout / non-zero exit / spawn
    failure → ("", "<reason>") so the caller can record a placeholder. NEVER raises."""
    try:
        proc = popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=_SHELL_TIMEOUT_S,
            cwd=cwd,
        )
    except subprocess.TimeoutExpired:
        return "", f"timed out after {int(_SHELL_TIMEOUT_S)}s"
    except (OSError, ValueError) as exc:
        return "", f"could not run {argv[0] if argv else '?'}: {exc}"
    rc = getattr(proc, "returncode", 0) or 0
    out = getattr(proc, "stdout", "") or ""
    if rc != 0:
        err = (getattr(proc, "stderr", "") or "").strip()
        # A non-zero exit still returns whatever stdout we got (some tools emit partial
        # output then a non-zero code); the error is surfaced in provenance.
        return out, f"exited {rc}" + (f": {err[:200]}" if err else "")
    return out, None


# ---------------------------------------------------------------------------
#  Per-kind assemblers
# ---------------------------------------------------------------------------

def _assemble_file(target: dict, popen: Callable[..., Any]) -> tuple[str, dict]:
    """Read a single repo file (cap 40k). Confined under the repo root (an escape →
    a rejection placeholder)."""
    repo = target.get("repo") or "."
    path = target.get("path") or ""
    prov: dict[str, Any] = {"kind": "file", "repo": repo, "path": path}
    resolved = _confined_target(repo, path)
    if resolved is None:
        prov["error"] = "path rejected (escapes the repo root or is absolute)"
        return f"[Explain could not read '{path}': it escapes the repo root.]", prov
    if not resolved.is_file():
        prov["error"] = "not a file"
        return f"[Explain could not read '{path}': not a regular file.]", prov
    try:
        raw = resolved.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        prov["error"] = f"read failed: {exc}"
        return f"[Explain could not read '{path}': {exc}.]", prov
    body, truncated = _truncate(raw, _FILE_MAX_CHARS)
    prov["truncated"] = truncated
    prov["chars"] = len(body)
    header = f"# File: {path} (repo: {repo})\n\n"
    return header + body, prov


def _assemble_blast(target: dict, popen: Callable[..., Any]) -> tuple[str, dict]:
    """Run `cortex-graph-blast --target <fn> --repo <repo>` (cap 8k). The blast radius
    is the structural neighborhood (callers/callees) of a function/symbol."""
    repo = target.get("repo") or "."
    fn = target.get("fn") or target.get("fn_name") or ""
    prov: dict[str, Any] = {"kind": "blast", "repo": repo, "fn": fn}
    if not fn:
        prov["error"] = "no function/symbol given"
        return "[Explain blast: no target function/symbol was provided.]", prov
    script = _graph_blast_script()
    prov["script"] = script
    out, err = _run_shell([script, "--target", fn, "--repo", repo], popen)
    if err:
        prov["error"] = err
    if not out.strip():
        placeholder = (
            f"[Explain blast radius for '{fn}' in {repo} is unavailable"
            + (f" ({err})" if err else "")
            + ".]"
        )
        return placeholder, prov
    body, truncated = _truncate(out, _BLAST_MAX_CHARS)
    prov["truncated"] = truncated
    prov["chars"] = len(body)
    header = f"# Blast radius: {fn} (repo: {repo})\n\n"
    return header + body, prov


def _assemble_dir(target: dict, popen: Callable[..., Any]) -> tuple[str, dict]:
    """Walk a directory's source files (*.py/*.ts/*.tsx), emit a ~300-char snippet each
    (cap 20k total / 60 files). A structural overview of a package."""
    repo = target.get("repo") or "."
    path = target.get("path") or ""
    prov: dict[str, Any] = {"kind": "dir", "repo": repo, "path": path}
    resolved = _confined_target(repo, path)
    if resolved is None:
        prov["error"] = "path rejected (escapes the repo root or is absolute)"
        return f"[Explain could not walk '{path}': it escapes the repo root.]", prov
    if not resolved.is_dir():
        prov["error"] = "not a directory"
        return f"[Explain could not walk '{path}': not a directory.]", prov
    try:
        repo_dir = Path(repo).expanduser().resolve(strict=False)
    except (OSError, RuntimeError):
        repo_dir = resolved
    pieces: list[str] = []
    total = 0
    files_used = 0
    truncated = False
    # Deterministic order so the same directory always assembles the same way.
    candidates = sorted(
        p for p in resolved.rglob("*")
        if p.is_file() and p.suffix in _DIR_EXTENSIONS
    )
    for fp in candidates:
        if files_used >= _DIR_MAX_FILES:
            truncated = True
            break
        # Re-confine each walked file (defence in depth: a symlink could point out).
        if not _is_within(fp.resolve(strict=False), repo_dir):
            continue
        try:
            rel = fp.resolve(strict=False).relative_to(repo_dir).as_posix()
        except ValueError:  # pragma: no cover - already confined
            rel = fp.name
        try:
            snippet = fp.read_text(encoding="utf-8", errors="replace")[:_DIR_SNIPPET_CHARS]
        except OSError:
            continue
        block = f"## {rel}\n{snippet}\n"
        if total + len(block) > _DIR_MAX_CHARS:
            truncated = True
            break
        pieces.append(block)
        total += len(block)
        files_used += 1
    prov["files"] = files_used
    prov["truncated"] = truncated
    prov["chars"] = total
    if not pieces:
        return f"[Explain found no source files (*.py/*.ts/*.tsx) under '{path}'.]", prov
    header = f"# Directory: {path} (repo: {repo}) — {files_used} file(s)\n\n"
    body = header + "".join(pieces)
    if truncated:
        body += _TRUNCATION_MARKER
    return body, prov


def _assemble_diff(target: dict, popen: Callable[..., Any]) -> tuple[str, dict]:
    """Run `git -C <repo> diff <rev>..HEAD` (or `diff HEAD` when no rev) (cap 24k). The
    change set the explainer narrates."""
    repo = target.get("repo") or "."
    rev = (target.get("git_rev") or "").strip()
    prov: dict[str, Any] = {"kind": "diff", "repo": repo, "git_rev": rev or None}
    if rev:
        argv = ["git", "-C", repo, "diff", f"{rev}..HEAD"]
    else:
        argv = ["git", "-C", repo, "diff", "HEAD"]
    out, err = _run_shell(argv, popen)
    if err:
        prov["error"] = err
    if not out.strip():
        # An empty diff is legitimate (nothing changed) — say so plainly.
        placeholder = (
            f"[Explain diff for {repo} ({rev or 'working tree'} → HEAD) is empty"
            + (f" or unavailable ({err})" if err else "")
            + ".]"
        )
        return placeholder, prov
    body, truncated = _truncate(out, _DIFF_MAX_CHARS)
    prov["truncated"] = truncated
    prov["chars"] = len(body)
    header = f"# Diff: {rev or 'working tree'} → HEAD (repo: {repo})\n\n"
    return header + body, prov


def _assemble_project(target: dict, popen: Callable[..., Any]) -> tuple[str, dict]:
    """Assemble a project-level architecture brief from repo-local signals.

    This is intentionally not "dir at .". It gives the LLM an architecture-oriented
    source map: operator docs/config snippets, representative source inventory,
    extension counts, top directories, and required coverage. It reads only inside the
    resolved repo root and skips generated/vendor caches.
    """
    repo = target.get("repo") or "."
    prov: dict[str, Any] = {"kind": "project", "repo": repo}
    root = _confined_target(repo, "")
    if root is None or not root.is_dir():
        prov["error"] = "repo root unavailable"
        return f"[Explain project overview for {repo} is unavailable: repo root is not readable.]", prov

    signal_blocks: list[str] = []
    signals_used: list[str] = []
    for rel in _PROJECT_SIGNAL_FILES:
        fp = root / rel
        if not fp.is_file():
            continue
        try:
            raw = fp.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        snippet = raw[:_PROJECT_SIGNAL_SNIPPET_CHARS]
        signal_blocks.append(f"## Signal file: {rel}\n{snippet}\n")
        signals_used.append(rel)

    files: list[Path] = []
    top_counts: dict[str, int] = {}
    ext_counts: dict[str, int] = {}
    truncated = False
    for base, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in _PROJECT_SKIP_DIRS)
        for name in sorted(names):
            fp = Path(base) / name
            suffix = fp.suffix.lower()
            if suffix not in _PROJECT_SOURCE_EXTENSIONS and name not in _PROJECT_SIGNAL_FILES:
                continue
            try:
                rel = fp.resolve(strict=False).relative_to(root).as_posix()
            except (OSError, ValueError):
                continue
            parts = rel.split("/", 1)
            top = parts[0] if len(parts) > 1 else "."
            top_counts[top] = top_counts.get(top, 0) + 1
            ext = suffix or fp.name
            ext_counts[ext] = ext_counts.get(ext, 0) + 1
            files.append(fp)
            if len(files) >= _PROJECT_MAX_FILES:
                truncated = True
                break
        if truncated:
            break

    rel_files: list[str] = []
    for fp in files:
        try:
            rel_files.append(fp.resolve(strict=False).relative_to(root).as_posix())
        except (OSError, ValueError):
            continue
    top_lines = [f"- {name}: {count} file(s)" for name, count in sorted(top_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:20]]
    ext_lines = [f"- {name}: {count}" for name, count in sorted(ext_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:20]]
    file_lines = [f"- {rel}" for rel in rel_files[:120]]

    body = (
        f"# Project architecture overview (repo: {repo})\n\n"
        "## Required explainer coverage\n"
        "- Explain the project purpose from repo-local evidence.\n"
        "- Include a system architecture map and a runtime/data-flow diagram.\n"
        "- Identify primary entrypoints, services, storage/config surfaces, integrations, and extension/update seams.\n"
        "- Call out uncertainty explicitly when the repo evidence is incomplete.\n\n"
        "## Repository signal files\n"
        + ("\n".join(signal_blocks) if signal_blocks else "[No standard signal files found.]\n")
        + "\n## Top-level source map\n"
        + ("\n".join(top_lines) if top_lines else "[No source files found.]\n")
        + "\n\n## File extension profile\n"
        + ("\n".join(ext_lines) if ext_lines else "[No source extensions found.]\n")
        + "\n\n## Representative files\n"
        + ("\n".join(file_lines) if file_lines else "[No representative files found.]\n")
        + "\n"
    )
    body, clipped = _truncate(body, _PROJECT_MAX_CHARS)
    prov.update({
        "signals": signals_used,
        "files": len(rel_files),
        "top_dirs": top_counts,
        "extensions": ext_counts,
        "truncated": truncated or clipped,
        "chars": len(body),
    })
    return body, prov


# ---------------------------------------------------------------------------
#  Dispatch
# ---------------------------------------------------------------------------

_ASSEMBLERS: dict[str, Callable[[dict, Callable[..., Any]], tuple[str, dict]]] = {
    "file": _assemble_file,
    "blast": _assemble_blast,
    "dir": _assemble_dir,
    "diff": _assemble_diff,
    "project": _assemble_project,
}


def assemble(
    target: dict, popen: Callable[..., Any] = subprocess.run
) -> tuple[str, dict]:
    """Assemble the explain CONTEXT for a `target`, returning (context_text, provenance).

    `target` is a dict with a `kind` ∈ {file, blast, dir, diff} plus the per-kind fields
    (`repo`, and `path` / `fn` / `git_rev`). Dispatches to the matching assembler; an
    unknown kind returns a clear placeholder (never raises). `provenance` is a dict the
    run records (the kind, the inputs, char counts, truncation, and any degrade error) so
    the persisted artifact carries an audit trail of what it was built from.

    `popen` is injected (defaults to `subprocess.run`) so the graph-blast/git lanes are
    fully testable with a fake — no live CLI, no git, no network."""
    kind = (target.get("kind") or "").strip().lower()
    fn = _ASSEMBLERS.get(kind)
    if fn is None:
        return (
            f"[Explain does not know how to assemble kind '{kind}'.]",
            {"kind": kind, "error": "unknown kind"},
        )
    return fn(target, popen)


__all__ = ["assemble"]
