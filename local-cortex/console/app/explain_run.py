"""explain_run: generate ONE visual HTML explainer on the HOST, persist it to L5, exit.

The Explain twin of `chat_run`. The Explain capability turns a code TARGET (a file, a
function's blast radius, a directory, or a git diff) into a SELF-CONTAINED HTML document
that visually explains it (diagrams + prose), then persists that document as a Cortex L5
artifact via the HTTP `POST /artifacts` endpoint. Like the chat runner, generation is
HOST-side (the containerized console can't read repo files or run `cortex-graph-*`), so
the host harness-service shells `run-explain`, which drives THIS module.

`explain_one(...)` is the reusable core: GIVEN a `target` + a pre-created `run_id`, it:
  * `start_run(run_id, lease_owner='explain')` opens the run row (NO handoff),
  * `assemble(target)` gathers the source context (best-effort; degrade-safe),
  * builds the prompt from `EXPLAIN_SYSTEM_PROMPT` + the context,
  * streams the generation via `runner.stream_chat(...)`, appending each delta as an
    `output` span (so the SPA can watch it build),
  * VALIDATES the result is a single self-contained HTML document (non-empty, starts
    with `<html`/`<!DOCTYPE html>`, ≤ 2 MB),
  * BEST-EFFORT persists it to Cortex L5 (`cortex.post_artifact`, modality `html`, an
    `explains` edge to the target) — a failed write does NOT abort the run,
  * `set_status('ok', metadata={artifact_id})` on success | `set_status('error', …)`.

GRACEFUL-DEGRADE (house law, mirrors `chat_run`): EVERY store call is best-effort (a
None / raising store is a clean no-op; the document is still produced). The L5 write is
ALSO best-effort — a None / raising `cortex` leaves the run OK with `artifact_id=None`
(logged), never an error. The only things that set status `error` are an empty / non-HTML
/ oversized generation, or a harness error.

Exit code: 0 = ok, 1 = error.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import re
import sys
from dataclasses import dataclass
from typing import Any, Optional

from .explain_context import assemble

log = logging.getLogger("console.explain_run")

# The largest generated document we will validate + persist (bytes of the UTF-8 HTML).
# A document over this is rejected as an error (a runaway generation, not a real
# explainer). Env-overridable; not a per-project literal.
MAX_HTML_BYTES = 2_000_000

# The HTML-document prefixes a valid explainer must start with (case-insensitive, after
# stripping leading whitespace). The model is instructed to emit NO surrounding markdown
# fences, so the very first non-space chars are the doctype / root element.
_HTML_PREFIXES = ("<!doctype html", "<html")
_HTML_ROOT_RE = re.compile(r"<html(?:\s|>)", re.IGNORECASE)
_HTML_CLOSE = "</html>"


# The generation instruction. Pinned as a module constant so the ONE place the explainer
# contract lives is obvious. It demands a SINGLE self-contained HTML document with inline
# styles, Mermaid via CDN (each diagram backed by a text fallback so it still reads with
# no JS / no network), an optional Chart.js, a CSS-grid layout, a one-line <title>, and
# NO surrounding markdown fences (so the output is the document itself, ready to render in
# a sandboxed iframe).
EXPLAIN_SYSTEM_PROMPT = """You are a senior engineer producing a VISUAL CODE EXPLAINER as ONE self-contained HTML document.

Output rules (STRICT — the output is rendered directly in a sandboxed iframe):
- Output ONE complete HTML document and NOTHING else. Start with `<!DOCTYPE html>` then `<html>`. Do NOT wrap the document in markdown code fences. Do NOT add any prose before or after the document.
- Put ALL styling in a single inline `<style>` block. Use a clean CSS-grid layout. No external stylesheets.
- Give the document a single concise `<title>` (a one-line summary of what is explained) — it becomes the artifact caption.
- Use Mermaid for diagrams, loaded from a CDN (`https://cdn.jsdelivr.net/npm/mermaid/dist/mermaid.min.js`, initialized on load). For EVERY Mermaid diagram, ALSO include the same information as a plain-text fallback inside a `<details>` block AND a `<noscript>` block, so the document still explains the code with no JavaScript and no network access.
- Chart.js (from a CDN) is optional — use it only if a chart genuinely helps; otherwise omit it. Like Mermaid, any chart needs a text fallback.
- Explain the STRUCTURE and BEHAVIOUR: what the code does, the key components and how they relate, the important control/data flow, and the notable edge cases. Be accurate to the provided source; do not invent APIs.
- If the source material is a PROJECT ARCHITECTURE OVERVIEW, include these sections explicitly: purpose, architecture map, runtime/data flow, entrypoints, storage/config, integrations, extension/update seams, operational risks, and open questions/unknowns. Include at least two Mermaid diagrams when the evidence supports them.
- Keep it readable and self-contained: assume the reader may have neither JavaScript nor network access (hence the text fallbacks).

You will be given the source material to explain below."""


@dataclass
class ExplainResult:
    """The outcome of one explain run."""

    status: str                       # "ok" | "error"
    html: str = ""
    artifact_id: Optional[str] = None
    caption: str | None = None
    error: Optional[str] = None
    harness: str | None = None
    model: str | None = None


def _extract_title(html: str) -> str | None:
    """Pull the `<title>` text from the generated document (the artifact caption). A
    simple, dependency-free scan (the document is the model's own output, not hostile
    HTML we need to sanitize here — the SPA renders it sandboxed). None if absent."""
    low = html.lower()
    start = low.find("<title")
    if start == -1:
        return None
    gt = low.find(">", start)
    if gt == -1:
        return None
    end = low.find("</title>", gt)
    if end == -1:
        return None
    title = html[gt + 1:end].strip()
    return title or None


def _validate_html(html: str) -> str | None:
    """Validate the generation is a single self-contained HTML document. Returns an
    error string if invalid, else None.

    Rules (the architect's contract): non-empty, starts with `<html`/`<!DOCTYPE html>`
    (case-insensitive, after strip), and ≤ MAX_HTML_BYTES encoded. A non-HTML or empty
    reply means the model didn't follow the contract; an oversized one is a runaway."""
    if not html or not html.strip():
        return "generation was empty"
    stripped = html.lstrip()
    low = stripped.lower()
    if not any(low.startswith(p) for p in _HTML_PREFIXES):
        preview = " ".join(stripped[:80].split())
        return f"generation is not a self-contained HTML document (starts with: {preview!r})"
    if len(html.encode("utf-8")) > MAX_HTML_BYTES:
        return f"generation exceeds the {MAX_HTML_BYTES}-byte HTML cap"
    return None


def extract_html_document(generation: str) -> str:
    """Return the complete HTML document embedded in a harness generation.

    Some harnesses stream their own progress sentence before the model payload even when
    the prompt requires HTML-only output. Prefer a doctype when present, otherwise the
    first real ``<html>`` root, and trim any trailing harness commentary after the final
    closing tag. A generation with no HTML root is returned unchanged so validation can
    produce the existing useful error.
    """
    if not generation:
        return generation
    lower = generation.lower()
    start = lower.find("<!doctype html")
    if start < 0:
        match = _HTML_ROOT_RE.search(generation)
        if match is None:
            return generation
        start = match.start()
    end = lower.rfind(_HTML_CLOSE)
    if end >= start:
        return generation[start : end + len(_HTML_CLOSE)].strip()
    return generation[start:].strip()


def _target_summary(target: dict) -> tuple[str, str]:
    """A short (kind, path-ish) pair describing the target — for the neighborhood text +
    the artifact edge. `path` is the file/dir path, the function name, or the git rev."""
    kind = (target.get("kind") or "").strip().lower() or "code"
    pathish = (
        target.get("path")
        or target.get("fn")
        or target.get("fn_name")
        or target.get("git_rev")
        or target.get("repo")
        or ""
    )
    return kind, str(pathish)


async def explain_one(
    target: dict,
    *,
    run_id: str,
    runner: Any,
    runstate: Optional[Any] = None,
    cortex: Optional[Any] = None,
    project: str = "",
    agent: str = "",
    harness: str | None = None,
    model: str | None = None,
) -> ExplainResult:
    """Run ONE explain generation → write run-state spans + terminal status under
    `run_id`, then BEST-EFFORT persist the document to Cortex L5.

    MIRRORS `chat_run.chat_one`'s run-state discipline (open row → running → spans →
    terminal status), MINUS the handoff lifecycle (an explain run is free-standing:
    `lease_owner='explain'`, `handoff_id=None`). The L5 write is best-effort: a failed
    `post_artifact` keeps the run OK with `artifact_id=None`.

    GRACEFUL-DEGRADE: every store + cortex call is wrapped so a None / raising
    collaborator never breaks the run (the document is still produced + returned)."""
    _rs_on = runstate is not None

    async def _rs_status(status: str, **kw: Any) -> None:
        if not _rs_on:
            return
        with contextlib.suppress(Exception):
            await runstate.set_status(run_id, status, **kw)

    seq = 0

    async def _rs_span(kind: str, text: str) -> None:
        nonlocal seq
        if not _rs_on or not text:
            return
        seq += 1
        with contextlib.suppress(Exception):
            await runstate.append_output(run_id, seq=seq, kind=kind, text=text)

    # Open the run_state row (lease_owner='explain', NO handoff). Best-effort.
    if _rs_on:
        with contextlib.suppress(Exception):
            await runstate.start_run(
                run_id=run_id,
                project=project,
                agent=agent,
                agent_display=agent,
                handoff_id=None,
                harness=harness,
                model=model,
                lease_owner="explain",
            )

    result = ExplainResult(status="ok", harness=harness, model=model)
    kind, pathish = _target_summary(target)

    def _run_metadata(
        *, artifact_id: Optional[str] = None, caption: Optional[str] = None
    ) -> dict[str, Any]:
        """The run_state.metadata sidecar an explain run stamps on a terminal status.

        Carries `capability='explain'` + the run_id, the resolved TARGET (kind + path),
        and — on success — the `artifact_id` + the document `caption` (its <title>). The
        gallery enumerates run_state and reads THIS to label each run + jump to its L5
        artifact, so it never has to parse the input span or re-read the artifact. Empty
        fields are omitted (a thin sidecar is fine — the gallery degrades per field)."""
        meta: dict[str, Any] = {"capability": "explain", "run_id": run_id}
        if kind:
            meta["target_kind"] = kind
        if pathish:
            meta["target_path"] = pathish
        if artifact_id is not None:
            meta["artifact_id"] = artifact_id
        if caption:
            meta["caption"] = caption
        return meta

    # Assemble the source context on the host (degrade-safe — `assemble` never raises;
    # a failed lane yields a placeholder + records the reason in provenance).
    context_text = ""
    provenance: dict[str, Any] = {}
    try:
        context_text, provenance = await asyncio.to_thread(assemble, target)
    except Exception as exc:  # pragma: no cover - assemble is already degrade-safe
        context_text = f"[Explain context assembly failed: {exc}]"
        provenance = {"kind": kind, "error": f"assemble crashed: {exc}"}

    # Record the user-facing intent as an `input` span (mirrors chat's input span) so the
    # transcript shows what was asked, then build the prompt.
    await _rs_span("input", f"Explain {kind}: {pathish}")
    prompt = f"{EXPLAIN_SYSTEM_PROMPT}\n\n---\nSource material to explain:\n\n{context_text}\n"

    await _rs_status("running")

    assembled: list[str] = []
    try:
        async for ev in runner.stream_chat(
            prompt,
            model=model,
            system=EXPLAIN_SYSTEM_PROMPT,
            harness=harness,
            run_context="explain",
        ):
            etype = ev.get("type")
            if etype == "delta":
                delta_text = ev.get("text", "")
                assembled.append(delta_text)
                await _rs_span("output", delta_text)
            elif etype == "result":
                txt = ev.get("text") or ""
                if txt:
                    assembled.append(txt)
                    await _rs_span("output", txt)
            elif etype == "error":
                result.status = "error"
                result.error = ev.get("message", "harness error")
            # session / done frames are not separately surfaced.
    except Exception as exc:  # a runner crash → error terminal (never propagates)
        result.status = "error"
        result.error = f"explain generation crashed: {exc}"

    raw_generation = "".join(assembled)
    html = extract_html_document(raw_generation)
    result.html = html

    # A harness may emit a terminal transport/error frame after it has already delivered a
    # complete document. The document is the Explain deliverable, so recover only when that
    # payload independently satisfies the full HTML contract; partial output remains failed.
    if result.status == "error":
        if _validate_html(html) is not None or not html.rstrip().lower().endswith(_HTML_CLOSE):
            await _rs_status("error", error=result.error, metadata=_run_metadata())
            return result
        result.status = "ok"
        result.error = None

    # VALIDATE the generation is a self-contained HTML document.
    invalid = _validate_html(html)
    if invalid is not None:
        result.status = "error"
        result.error = invalid
        await _rs_status("error", error=invalid, metadata=_run_metadata())
        return result

    caption = _extract_title(html) or f"Explain: {kind} {pathish}".strip()
    result.caption = caption

    # BEST-EFFORT L5 persistence. A failed/absent write keeps the run OK (artifact_id
    # None, logged) — the document is the deliverable; the artifact is the durable copy.
    artifact_id: Optional[str] = None
    if cortex is not None:
        content_hash = hashlib.sha256(html.encode("utf-8")).hexdigest()
        source_file = f"explain/{run_id}.html"
        neighborhood = f"Explain: {kind} {pathish} — {caption}".strip()
        repo = str(target.get("repo") or "")
        edge_kw: dict[str, Any] = {}
        # Record an `explains` edge to the target so the artifact is discoverable from the
        # thing it explains. target_ref is the path/fn/rev; target_type is the kind.
        if pathish:
            edge_kw = {
                "edge_type": "explains",
                "target_type": kind,
                "target_ref": pathish,
            }
        with contextlib.suppress(Exception):
            artifact_id = await cortex.post_artifact(
                project,
                agent,
                source_file=source_file,
                content_hash=content_hash,
                modality="html",
                raw_content=html,
                caption=caption,
                neighborhood_text=neighborhood,
                source_doc_metadata={
                    "explain_kind": kind,
                    "explain_path": pathish,
                    "repo": repo,
                    "provenance": provenance,
                    "run_id": run_id,
                },
                metadata={"capability": "explain", "run_id": run_id},
                **edge_kw,
            )
        if not artifact_id:
            log.warning(
                "explain L5 persist returned no artifact id (run stays ok): run_id=%s",
                run_id,
            )

    result.artifact_id = artifact_id
    # Stamp the artifact_id + the TARGET + the caption on the run (the gallery enumerates
    # run_state and reads this to label the run + jump to the artifact). The metadata is
    # None-safe in set_status — a None artifact_id still records the capability + target.
    await _rs_status(
        "ok",
        metadata=_run_metadata(artifact_id=artifact_id, caption=caption),
    )
    return result


# ---------------------------------------------------------------------------
#  Bootstrap helpers (the real collaborators for the live host run)
# ---------------------------------------------------------------------------

def _build_runner() -> Any:
    """The real harness runner — the SAME `harness_runner` the chat path uses, so a host
    explain generation streams identically to a chat turn."""
    from . import harness_runner
    return harness_runner


def _build_runstate() -> Optional[Any]:
    """Build the explain runner's OWN RunStatePort store (mirrors
    `chat_run._build_runstate`). The runner is a DETACHED host subprocess, so it builds
    its own `RunStatePgStore` over its own `AppDB`. Wrapped so a setup failure → None
    (explain_one then skips store writes cleanly)."""
    try:
        from .adapters.runstate_pg import RunStatePgStore
        from .appdb import AppDB
        return RunStatePgStore(AppDB())
    except Exception:  # pragma: no cover - defensive: store is optional, never fatal
        return None


def _build_cortex() -> Optional[Any]:
    """Build the explain runner's Cortex HTTP client for the L5 write (`post_artifact`).
    Wrapped so a setup failure → None (explain_one then skips the L5 write cleanly; the
    run-state write is unaffected)."""
    try:
        from .cortex_client import CortexClient
        return CortexClient()
    except Exception:  # pragma: no cover - defensive: the L5 write is optional
        return None


# ---------------------------------------------------------------------------
#  Entry point
# ---------------------------------------------------------------------------

async def _amain(
    agent: str,
    project: str,
    run_id: str,
    target: dict,
    *,
    harness: str | None = None,
    model: str | None = None,
) -> int:
    runner = _build_runner()
    runstate = _build_runstate()
    cortex = _build_cortex()
    res = await explain_one(
        target,
        run_id=run_id,
        runner=runner,
        runstate=runstate,
        cortex=cortex,
        project=project,
        agent=agent,
        harness=harness,
        model=model,
    )
    # Close the run-state store's asyncpg pool (best-effort).
    if runstate is not None:
        with contextlib.suppress(Exception):
            await runstate._appdb.aclose()
    # Close the Cortex HTTP client (best-effort).
    if cortex is not None:
        with contextlib.suppress(Exception):
            await cortex.aclose()
    return 0 if res.status == "ok" else 1


def _parse_argv(argv: list[str]) -> tuple[dict, str | None, str | None] | None:
    """Parse the run-explain argv tail into (target, harness, model), or None on a usage
    error. The tail (after `<agent> <project> <run_id>`) is a set of flags:
        --kind <project|file|blast|dir|diff>  --repo <path>  [--path <p>] [--fn <name>]
        [--git-rev <rev>] [--harness <h>] [--model <m>]"""
    flags: dict[str, str] = {}
    rest = list(argv)
    while rest:
        tok = rest.pop(0)
        if not tok.startswith("--"):
            return None
        key = tok[2:]
        if not rest:
            return None
        flags[key] = rest.pop(0)
    kind = (flags.get("kind") or "").strip().lower()
    if kind not in ("project", "file", "blast", "dir", "diff"):
        return None
    target: dict[str, Any] = {
        "kind": kind,
        "repo": flags.get("repo") or ".",
    }
    if "path" in flags:
        target["path"] = flags["path"]
    if "fn" in flags:
        target["fn"] = flags["fn"]
    if "git-rev" in flags:
        target["git_rev"] = flags["git-rev"]
    return target, flags.get("harness"), flags.get("model")


def main(argv: list[str]) -> int:
    # argv: <agent> <project> <run_id> --kind <k> --repo <r> [--path <p>] [--fn <n>]
    #       [--git-rev <rev>] [--harness <h>] [--model <m>]
    usage = (
        "usage: run-explain <agent> <project> <run_id> --kind <project|file|blast|dir|diff> "
        "--repo <path> [--path <p>] [--fn <name>] [--git-rev <rev>] "
        "[--harness <h>] [--model <m>]\n"
    )
    if len(argv) < 5:
        sys.stderr.write(usage)
        return 64
    agent, project, run_id = argv[0], argv[1], argv[2]
    parsed = _parse_argv(argv[3:])
    if parsed is None:
        sys.stderr.write(usage)
        return 64
    target, harness, model = parsed
    return asyncio.run(
        _amain(agent, project, run_id, target, harness=harness, model=model)
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
