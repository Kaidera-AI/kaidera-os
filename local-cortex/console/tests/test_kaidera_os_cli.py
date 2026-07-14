"""kaidera-os CLI — the upgrade money-path (success + automatic rollback).

`do_upgrade` takes injectable run/http seams, so we drive both a healthy upgrade and a
failed-health-check rollback against real temp dirs — no systemd, no Docker, no network.
"""

from __future__ import annotations

import tarfile
from pathlib import Path

import pytest

from app.kaidera_os_cli import _materialize, do_upgrade, _migrate


def _make_console(root: Path, marker: str) -> Path:
    """A fake installed console: app/ + spa/dist each carrying a version marker file."""
    cdir = root / "console"
    (cdir / "app").mkdir(parents=True)
    (cdir / "spa" / "dist" / "assets").mkdir(parents=True)
    (cdir / "app" / "version.py").write_text(f'__version__ = "{marker}"\n')
    (cdir / "spa" / "dist" / "index.html").write_text(f"<!-- {marker} -->")
    return cdir


def _make_artifact(root: Path, marker: str) -> Path:
    """A fake release directory holding the NEW app/ + spa/dist."""
    src = root / "release"
    (src / "app").mkdir(parents=True)
    (src / "spa" / "dist").mkdir(parents=True)
    (src / "app" / "version.py").write_text(f'__version__ = "{marker}"\n')
    (src / "spa" / "dist" / "index.html").write_text(f"<!-- {marker} -->")
    return src


def _make_full_repo_artifact(root: Path, marker: str) -> Path:
    """A fake canonical signed-release layout: full repo → local-cortex/console."""
    repo = root / "kaidera-os-v0.2.0"
    console = repo / "local-cortex" / "console"
    (console / "app").mkdir(parents=True)
    (console / "spa" / "dist").mkdir(parents=True)
    (console / "app" / "version.py").write_text(f'__version__ = "{marker}"\n')
    (console / "spa" / "dist" / "index.html").write_text(f"<!-- {marker} -->")
    return repo


def _tar_dir(src: Path, out: Path) -> Path:
    with tarfile.open(out, "w:gz") as tf:
        tf.add(src, arcname=src.name)
    return out


@pytest.fixture
def systemd_present(monkeypatch):
    monkeypatch.setattr(
        "app.kaidera_os_cli.shutil.which",
        lambda name: "/bin/systemctl" if name == "systemctl" else None,
    )


def test_upgrade_success_swaps_dirs(tmp_path, systemd_present):
    cdir = _make_console(tmp_path / "repo" / "local-cortex", "0.1.0")
    src = _make_artifact(tmp_path, "0.2.0")
    runs: list = []

    rc = do_upgrade(
        str(src),
        cdir=cdir,
        run=lambda argv, **kw: runs.append(argv) or type("R", (), {"returncode": 0})(),
        http_status_version=lambda url: "0.2.0",  # healthy: reports the new version
        expected_version="0.2.0",
        skip_migrate=True,
    )

    assert rc == 0
    # The new release content is live.
    assert '0.2.0' in (cdir / "app" / "version.py").read_text()
    assert "0.2.0" in (cdir / "spa" / "dist" / "index.html").read_text()


def test_upgrade_accepts_full_repo_release_directory(tmp_path, systemd_present):
    cdir = _make_console(tmp_path / "installed" / "local-cortex", "0.1.0")
    src = _make_full_repo_artifact(tmp_path / "release", "0.2.0")

    rc = do_upgrade(
        str(src),
        cdir=cdir,
        run=lambda argv, **kw: type("R", (), {"returncode": 0})(),
        http_status_version=lambda url: "0.2.0",
        expected_version="0.2.0",
        skip_migrate=True,
    )

    assert rc == 0
    assert '0.2.0' in (cdir / "app" / "version.py").read_text()
    assert "0.2.0" in (cdir / "spa" / "dist" / "index.html").read_text()


def test_materialize_accepts_full_repo_release_tarball(tmp_path):
    src = _make_full_repo_artifact(tmp_path / "release", "0.2.0")
    art = _tar_dir(src, tmp_path / "kaidera-os-v0.2.0.tar.gz")

    materialized = _materialize(str(art), tmp_path / "work")

    assert materialized.name == "console"
    assert materialized.parent.name == "local-cortex"
    assert (materialized / "app" / "version.py").exists()
    assert (materialized / "spa" / "dist" / "index.html").exists()


def test_upgrade_failed_healthcheck_rolls_back(tmp_path, systemd_present):
    cdir = _make_console(tmp_path, "0.1.0")
    src = _make_artifact(tmp_path, "0.2.0")

    rc = do_upgrade(
        str(src),
        cdir=cdir,
        run=lambda argv, **kw: type("R", (), {"returncode": 0})(),
        http_status_version=lambda url: None,  # console never comes back up → unhealthy
        expected_version="0.2.0",
        skip_migrate=True,
        health_attempts=3,
        health_sleep=lambda _s: None,  # don't really sleep between retries
    )

    assert rc == 1
    # Rolled back: the ORIGINAL content is restored, not the broken new release.
    assert '0.1.0' in (cdir / "app" / "version.py").read_text()
    assert "0.1.0" in (cdir / "spa" / "dist" / "index.html").read_text()


def test_upgrade_wrong_version_rolls_back(tmp_path, systemd_present):
    """Console comes up but reports the WRONG version (stale bundle) → rollback."""
    cdir = _make_console(tmp_path, "0.1.0")
    src = _make_artifact(tmp_path, "0.2.0")

    rc = do_upgrade(
        str(src),
        cdir=cdir,
        run=lambda argv, **kw: type("R", (), {"returncode": 0})(),
        http_status_version=lambda url: "0.1.0",  # didn't actually take
        expected_version="0.2.0",
        skip_migrate=True,
        health_attempts=3,
        health_sleep=lambda _s: None,
    )

    assert rc == 1
    assert '0.1.0' in (cdir / "app" / "version.py").read_text()


def test_upgrade_restart_failure_rolls_back(tmp_path, systemd_present):
    cdir = _make_console(tmp_path, "0.1.0")
    src = _make_artifact(tmp_path, "0.2.0")

    rc = do_upgrade(
        str(src),
        cdir=cdir,
        run=lambda argv, **kw: type("R", (), {"returncode": 1})(),
        http_status_version=lambda url: "0.1.0",
        skip_migrate=True,
    )

    assert rc == 1
    assert '0.1.0' in (cdir / "app" / "version.py").read_text()


def test_upgrade_migration_failure_rolls_back(monkeypatch, tmp_path):
    cdir = _make_console(tmp_path / "repo" / "local-cortex", "0.1.0")
    src = _make_artifact(tmp_path, "0.2.0")
    compose = cdir.parent.parent / ".agents" / "docker-compose.cortex.yml"
    compose.parent.mkdir(parents=True)
    compose.write_text("services: {}\n", encoding="utf-8")

    monkeypatch.setattr(
        "app.kaidera_os_cli.shutil.which",
        lambda name: f"/bin/{name}" if name in {"docker", "systemctl"} else None,
    )

    def _run(argv, **kw):
        rc = 1 if argv[:2] == ["docker", "compose"] else 0
        return type("R", (), {"returncode": rc})()

    rc = do_upgrade(
        str(src),
        cdir=cdir,
        run=_run,
        http_status_version=lambda url: "0.2.0",
        expected_version="0.2.0",
    )

    assert rc == 1
    assert '0.1.0' in (cdir / "app" / "version.py").read_text()
    assert "0.1.0" in (cdir / "spa" / "dist" / "index.html").read_text()


def test_upgrade_incomplete_artifact_is_rejected(tmp_path):
    cdir = _make_console(tmp_path, "0.1.0")
    bad = tmp_path / "bad"
    (bad / "app").mkdir(parents=True)  # has app/ but NO spa/dist
    rc = do_upgrade(str(bad), cdir=cdir, http_status_version=lambda url: "x", skip_migrate=True)
    assert rc == 2
    # Untouched.
    assert '0.1.0' in (cdir / "app" / "version.py").read_text()


# ── supply-chain integrity (a release becomes the live process) ─────────────

def test_materialize_rejects_insecure_http(tmp_path):
    with pytest.raises(SystemExit):
        _materialize("http://evil.example/release.tgz", tmp_path)


def test_materialize_https_requires_sha256(tmp_path):
    # Refused BEFORE any network call — no --sha256 pin given.
    with pytest.raises(SystemExit):
        _materialize("https://example.com/release.tgz", tmp_path)


def test_materialize_local_tarball_sha_mismatch_is_rejected(tmp_path):
    # A real .tgz, but the operator-supplied digest doesn't match → refuse to extract.
    payload = tmp_path / "app"
    payload.mkdir()
    (payload / "x").write_text("hi")
    art = tmp_path / "r.tgz"
    with tarfile.open(art, "w:gz") as tf:
        tf.add(payload, arcname="app")
    with pytest.raises(SystemExit):
        _materialize(str(art), tmp_path, sha256="deadbeef")


def test_migrate_uses_configured_compose_project(monkeypatch, tmp_path):
    cdir = tmp_path / "repo" / "local-cortex" / "console"
    compose = tmp_path / "repo" / ".agents" / "docker-compose.cortex.yml"
    compose.parent.mkdir(parents=True)
    compose.write_text("services: {}\n", encoding="utf-8")
    seen: list[list[str]] = []

    monkeypatch.setattr("app.kaidera_os_cli.shutil.which", lambda name: "/usr/bin/docker")
    monkeypatch.setenv("KAIDERA_OS_COMPOSE_PROJECT", "customer-stack")

    _migrate(cdir, run=lambda argv, **kw: seen.append(argv) or type("R", (), {"returncode": 0})())

    assert seen
    assert seen[0][0:4] == ["docker", "compose", "-p", "customer-stack"]
