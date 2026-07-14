"""The `explain` feature module — the console-side surface of the Explain capability.

Explain turns a code TARGET (a file, a function's blast radius, a directory, or a git
diff) into a self-contained visual HTML explainer, persisted as a Cortex L5 artifact.
Generation is HOST-side (`app/explain_run.py` driven by `scripts/run-explain` via the
host harness-service), because the containerized console can't read repo files or run
`cortex-graph-*`. THIS module is the thin console SURFACE that fronts it:

  * `api.py` — a FastAPI `APIRouter` (the only part that imports fastapi):
      - `POST /explain/{project}`              — start a generation (mint run_id, open a
                                                  run_state row, forward to the host
                                                  harness-service `/explain`).
      - `GET  /explain/{project}/result/{run_id}` — the persisted artifact for a run
                                                  (caption + modality + the preview the
                                                  Cortex search surface exposes).
      - `GET  /explain/{project}/list`         — the gallery (recent html artifacts).

`main.py` mounts the router additively (`app.include_router(explain.router)`). The route
forwards the spawn to the host service via httpx EXACTLY like the chat remote path, and
reads the run/artifact through the `CortexClient` + the `RunStatePort` already wired on
`app.state`. The HTML it persists is rendered SANDBOXED by the SPA (an isolated iframe) —
the console never renders it inline.

SCOPE — orchestration + read. The generation LOGIC lives host-side in `app/explain_run.py`
+ `app/explain_context.py`; this module starts a run and reads its result.
"""

from app.explain.api import router

__all__ = ["router"]
