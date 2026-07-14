"""Task 1 (propose_mode flag) TDD — RED tests.

Tests for the per-project propose_mode boolean in the app-DB:
  * is_propose_mode(project) -> bool
  * set_propose_mode(project, enabled) -> bool
  * default is False (no row / blank project / unavailable DB)

Uses monkeypatching of the SettingsDB so these run without a live app-DB.
"""

import app.settings as settings_store
from app import appdb as appdb_mod


def _make_propose_db(*, initial: dict[str, bool] | None = None) -> object:
    """A minimal stub for the propose_mode SettingsDB methods."""
    state: dict[str, bool] = dict(initial or {})
    UNAVAILABLE = appdb_mod.UNAVAILABLE

    class _StubDB:
        def get_project_propose_mode(self, project: str):
            key = (project or "").strip().lower()
            if not key:
                return False
            return state.get(key, False)

        def set_project_propose_mode(self, project: str, enabled: bool, updated_by=None):
            key = (project or "").strip().lower()
            if not key:
                return False
            state[key] = bool(enabled)
            return True

    return _StubDB()


def test_propose_mode_false_by_default(monkeypatch):
    """A project with no row reads as False (the safe default)."""
    stub = _make_propose_db()
    monkeypatch.setattr(settings_store, "_db", stub)
    assert settings_store.is_propose_mode("kaidera-os") is False


def test_propose_mode_set_true_reads_true(monkeypatch):
    """Setting propose_mode to True on a project → reads True."""
    stub = _make_propose_db()
    monkeypatch.setattr(settings_store, "_db", stub)
    ok = settings_store.set_propose_mode("kaidera-os", True)
    assert ok is True
    assert settings_store.is_propose_mode("kaidera-os") is True


def test_propose_mode_set_false_reads_false(monkeypatch):
    """Setting propose_mode to False is persisted (not just the default)."""
    stub = _make_propose_db(initial={"kaidera-os": True})
    monkeypatch.setattr(settings_store, "_db", stub)
    settings_store.set_propose_mode("kaidera-os", False)
    assert settings_store.is_propose_mode("kaidera-os") is False


def test_propose_mode_blank_project_false(monkeypatch):
    """A blank/None project key always reads False."""
    stub = _make_propose_db()
    monkeypatch.setattr(settings_store, "_db", stub)
    assert settings_store.is_propose_mode(None) is False
    assert settings_store.is_propose_mode("") is False
    assert settings_store.is_propose_mode("   ") is False


def test_propose_mode_unavailable_db_false(monkeypatch):
    """When the DB returns UNAVAILABLE (down), is_propose_mode falls back to False."""
    UNAVAILABLE = appdb_mod.UNAVAILABLE

    class _UnavailableDB:
        def get_project_propose_mode(self, project):
            return UNAVAILABLE

        def set_project_propose_mode(self, project, enabled, updated_by=None):
            return False

    monkeypatch.setattr(settings_store, "_db", _UnavailableDB())
    assert settings_store.is_propose_mode("kaidera-os") is False


def test_propose_mode_different_projects_isolated(monkeypatch):
    """propose_mode is per-project — setting one does not affect another."""
    stub = _make_propose_db()
    monkeypatch.setattr(settings_store, "_db", stub)
    settings_store.set_propose_mode("project-a", True)
    assert settings_store.is_propose_mode("project-a") is True
    assert settings_store.is_propose_mode("project-b") is False
