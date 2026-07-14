"""AttachmentMeta — the pure value object for ONE received chat attachment.

The DTO half of chat file-attachments (feature-gap step 6, Increment A). It describes
an attachment the operator added to a chat turn: the minted id, the run it belongs to,
the (sanitized) filename, the byte size, the content-type, the HOST path the bytes
landed at, and a created-at stamp.

It lives alongside the other domain DTOs (`SpawnRequest` / `ChatSpawnRequest` in
`app/domain/harness.py`) and obeys the SAME house law as the rest of `app/domain/`: it
is PURE — it imports ONLY the standard library (no httpx / fastapi / subprocess /
psycopg2 / asyncpg, and no filesystem I/O). The sandbox + inject + cleanup mechanics
live in the imperative shell (`app/attachments.py`); this module is just the data
shape, so it round-trips through `dataclasses.asdict` for serialization. A guard test
(`tests/test_attachments_domain.py`) pins the import purity exactly like the
ports/runstate guards.

SECURITY NOTE: `host_path` is the absolute on-disk location of the bytes. It is for
SERVER-SIDE use only (the chat runner reads it to inline the file into the prompt). It
MUST NEVER be returned to the browser — the upload route echoes only
`{attachment_id, filename, size_bytes}` back to the client.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class AttachmentMeta:
    """One received chat attachment's metadata (pure value object).

    Fields:
      * `attachment_id` — the server-minted id (uuid4 hex) the upload route returns and
        the SPA echoes on the chat send; the per-attachment sandbox subdir is keyed by
        it on the host side.
      * `run_id` — the chat run the attachment belongs to (the pre-minted chat run id);
        the in-process sandbox groups a turn's files under this.
      * `filename` — the sanitized basename shown in the transcript chip + woven into the
        prompt. NEVER a path (the sandbox layer strips any directory component).
      * `size_bytes` — the decoded byte size (after the per-file cap check).
      * `content_type` — the client-declared MIME type; drives text-vs-image handling in
        `inline_attachments`.
      * `host_path` — the ABSOLUTE on-disk path the bytes were written to. SERVER-SIDE
        ONLY — never serialized to the browser (see the module security note).
      * `created_at` — a unix timestamp (seconds) used by the 24h cleanup sweep.
    """

    attachment_id: str
    run_id: str
    filename: str
    size_bytes: int
    content_type: str
    host_path: str
    created_at: float


__all__ = ["AttachmentMeta"]
