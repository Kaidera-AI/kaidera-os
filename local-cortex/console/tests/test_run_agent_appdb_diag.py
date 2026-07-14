"""run-agent app-DB connect diagnosability (run-state visibility fix).

THE BUG it guards: a HOST worker whose app-DB is unreachable wrote NOTHING anywhere —
the run-state writes silently no-op (graceful-degrade) AND the worker's stderr was
DEVNULL, so the operator had zero signal for why a run stuck at ``queued``. With the
stderr-to-logfile change, the worker now EMITS one diagnostic line at startup that
states the resolved DSN and whether the app-DB connect-probe succeeded — so the next
failure is visible in ``~/.harness-runs/<run_id>.stderr``.

``_probe_runstate_appdb`` performs a best-effort, time-bounded connect probe and writes
ONE line to stderr:
  * OK    → ``[run-agent] app-DB OK (postgresql://harness:***@localhost:5500/...)``
  * DOWN  → ``[run-agent] app-DB UNREACHABLE (...) — run-state writes will NOT land``
It NEVER raises and NEVER blocks the run (a None store / a down DB just logs + returns).
"""
from __future__ import annotations

import asyncio

import app.run_agent as ra


class _OkAppDB:
    """An AppDB stand-in whose ping() succeeds (DB reachable)."""

    dsn = "postgresql://harness:harness@localhost:5500/harness_app"

    async def ping(self) -> bool:
        return True


class _DownAppDB:
    """An AppDB stand-in whose ping() fails (DB unreachable)."""

    dsn = "postgresql://harness:harness@harness-appdb:5432/harness_app"

    async def ping(self) -> bool:
        return False


class _StoreWith:
    """A RunStatePgStore stand-in exposing the AppDB as ``_appdb`` (the real attr name)."""

    def __init__(self, appdb):
        self._appdb = appdb


def test_probe_logs_ok_line_when_appdb_reachable(capsys):
    asyncio.run(ra._probe_runstate_appdb(_StoreWith(_OkAppDB())))
    err = capsys.readouterr().err
    assert "app-DB OK" in err
    # The DSN is shown (password redacted) so the operator can confirm host vs container.
    assert "localhost:5500" in err
    assert "harness:harness@" not in err  # password redacted


def test_probe_logs_unreachable_line_when_appdb_down(capsys):
    asyncio.run(ra._probe_runstate_appdb(_StoreWith(_DownAppDB())))
    err = capsys.readouterr().err
    assert "app-DB UNREACHABLE" in err
    # The offending (container) host is named so the operator sees WHY it failed.
    assert "harness-appdb" in err
    assert "run-state" in err.lower()


def test_probe_none_store_is_silent_noop(capsys):
    """A None store (legacy spawn / store construction failed) → no probe, no output."""
    asyncio.run(ra._probe_runstate_appdb(None))
    assert capsys.readouterr().err == ""


def test_probe_never_raises_when_ping_throws(capsys):
    """A ping() that RAISES must be swallowed (the probe is diagnostic-only, never fatal)."""

    class _Boom:
        dsn = "postgresql://x:y@localhost:5500/db"

        async def ping(self):
            raise RuntimeError("boom")

    # Must not raise.
    asyncio.run(ra._probe_runstate_appdb(_StoreWith(_Boom())))
    err = capsys.readouterr().err
    # It degrades to the UNREACHABLE line (a probe that errored == couldn't confirm).
    assert "app-DB UNREACHABLE" in err
