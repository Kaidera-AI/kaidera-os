"""host_appdb_dsn() — the HOST app-DB DSN resolver (run-state visibility fix).

THE BUG it guards against: the autonomous worker runs on the HOST (spawned by the
host harness-service), but the harness-service inherits its environment from whatever
shell launched it. If that shell had ``HARNESS_APPDB_DSN`` set to the CONTAINER value
(``harness-appdb:5432`` — the orchestrator's in-container DSN), the host worker
inherits it and EVERY run-state write silently no-ops (``harness-appdb`` does not
resolve on the host → asyncpg connect fails → graceful-degrade swallows it), so the
run sticks at ``queued`` with no pid / no spans. The console can then never SHOW the
run live.

``host_appdb_dsn()`` is the single source of truth for "the DSN a HOST-side process
must use to reach the app-DB". The harness-service forces it into every spawned
worker's env so the worker can NEVER keep the container DSN. Resolution order:

  1. an explicit ``HARNESS_APPDB_DSN_HOST`` override (operator's escape hatch), else
  2. ``HARNESS_APPDB_DSN`` rewritten to loopback when it points at the in-container
     host (``harness-appdb`` / a docker-gateway alias) — the host can't reach that
     hostname, so we swap the host:port for the loopback ``localhost:5500``, else
  3. ``HARNESS_APPDB_DSN`` as-is when it's already a host-reachable DSN, else
  4. the loopback default (``postgresql://harness:harness@localhost:5500/harness_app``).
"""
from __future__ import annotations

import app.appdb as appdb

HOST_DEFAULT = "postgresql://harness:harness@localhost:5500/harness_app"
CONTAINER_DSN = "postgresql://harness:harness@harness-appdb:5432/harness_app"


def _clear(monkeypatch):
    monkeypatch.delenv("HARNESS_APPDB_DSN", raising=False)
    monkeypatch.delenv("HARNESS_APPDB_DSN_HOST", raising=False)


def test_no_env_returns_loopback_default(monkeypatch):
    """No env at all → the loopback default (localhost:5500)."""
    _clear(monkeypatch)
    assert appdb.host_appdb_dsn() == HOST_DEFAULT


def test_container_dsn_is_rewritten_to_loopback(monkeypatch):
    """THE FIX: a container DSN (harness-appdb:5432) is rewritten to loopback so the
    HOST worker reaches the same Postgres via the published host port (5500)."""
    _clear(monkeypatch)
    monkeypatch.setenv("HARNESS_APPDB_DSN", CONTAINER_DSN)
    out = appdb.host_appdb_dsn()
    assert "harness-appdb" not in out
    assert "localhost:5500" in out
    # creds + db name preserved.
    assert out.startswith("postgresql://harness:harness@")
    assert out.endswith("/harness_app")


def test_explicit_host_override_wins(monkeypatch):
    """An explicit HARNESS_APPDB_DSN_HOST overrides everything (operator escape hatch),
    even when HARNESS_APPDB_DSN points at the container."""
    _clear(monkeypatch)
    monkeypatch.setenv("HARNESS_APPDB_DSN", CONTAINER_DSN)
    monkeypatch.setenv(
        "HARNESS_APPDB_DSN_HOST",
        "postgresql://harness:harness@127.0.0.1:5500/harness_app",
    )
    assert appdb.host_appdb_dsn() == "postgresql://harness:harness@127.0.0.1:5500/harness_app"


def test_already_host_dsn_passes_through(monkeypatch):
    """A HARNESS_APPDB_DSN that is ALREADY host-reachable (localhost / 127.0.0.1) is
    left untouched — we only rewrite the in-container hostname."""
    _clear(monkeypatch)
    already = "postgresql://harness:harness@localhost:5500/harness_app"
    monkeypatch.setenv("HARNESS_APPDB_DSN", already)
    assert appdb.host_appdb_dsn() == already


def test_custom_host_dsn_with_other_host_passes_through(monkeypatch):
    """A non-container custom host (e.g. a remote db host) is NOT rewritten — only the
    known in-container alias is swapped for loopback."""
    _clear(monkeypatch)
    remote = "postgresql://u:p@db.internal.example:6000/harness_app"
    monkeypatch.setenv("HARNESS_APPDB_DSN", remote)
    assert appdb.host_appdb_dsn() == remote


def test_container_host_alias_rewrite_preserves_db_and_query(monkeypatch):
    """The rewrite swaps ONLY host:port, preserving the path (db) and any query args."""
    _clear(monkeypatch)
    monkeypatch.setenv(
        "HARNESS_APPDB_DSN",
        "postgresql://harness:harness@harness-appdb:5432/harness_app?sslmode=disable",
    )
    out = appdb.host_appdb_dsn()
    assert "harness-appdb" not in out
    assert "localhost:5500" in out
    assert out.endswith("/harness_app?sslmode=disable")


def test_host_default_constant_is_loopback():
    """REGRESSION: the host-default constant is the loopback DSN (the value
    host_appdb_dsn() falls back to). Asserted WITHOUT reloading the module — a reload
    would replace the shared `settings_db` singleton and break other suites' isolation."""
    assert appdb._HOST_APPDB_DSN_DEFAULT == HOST_DEFAULT
