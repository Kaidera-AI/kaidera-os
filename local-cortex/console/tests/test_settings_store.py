"""Settings store canonicalization.

Pins the app-DB as the canonical settings store while keeping
`config/settings.local.json` as a seed/fallback only. These tests drive the
legacy sync `app.settings` facade directly with a fake SettingsDB; no live DB.
"""

from __future__ import annotations

import json
from typing import Any


class FakeSettingsDB:
    def __init__(
        self,
        *,
        app: dict[str, Any] | None = None,
        overrides: dict[str, dict[str, str]] | None = None,
        unavailable: Any = None,
        down: bool = False,
    ) -> None:
        self.app = dict(app or {})
        self.overrides = {str(k): dict(v) for k, v in (overrides or {}).items()}
        self.unavailable = unavailable
        self.down = down
        self.calls: list[tuple[str, Any]] = []

    def _down(self):
        return self.unavailable if self.down else None

    def load_app_settings(self):
        self.calls.append(("load_app_settings", None))
        down = self._down()
        return down if down is not None else dict(self.app)

    def upsert_app_settings(self, items):
        self.calls.append(("upsert_app_settings", dict(items)))
        if self.down:
            return False
        self.app.update(dict(items))
        return True

    def delete_app_setting(self, key):
        self.calls.append(("delete_app_setting", key))
        if self.down:
            return False
        self.app.pop(str(key), None)
        return True

    def has_any_app_settings(self):
        self.calls.append(("has_any_app_settings", None))
        down = self._down()
        return down if down is not None else bool(self.app)

    def load_agent_overrides(self):
        self.calls.append(("load_agent_overrides", None))
        down = self._down()
        if down is not None:
            return down
        return {str(k): dict(v) for k, v in self.overrides.items()}

    def replace_all_agent_overrides(self, blob):
        self.calls.append(("replace_all_agent_overrides", dict(blob)))
        if self.down:
            return False
        self.overrides = {str(k): dict(v) for k, v in blob.items()}
        return True

    def save_agent_override(self, project, agent, entry):
        self.calls.append(("save_agent_override", (project, agent, dict(entry))))
        if self.down:
            return False
        key = f"{project}:{agent}"
        if entry:
            self.overrides[key] = dict(entry)
        else:
            self.overrides.pop(key, None)
        return True


def _patch_store(monkeypatch, tmp_path, settings_store, fake_db):
    config_dir = tmp_path / "config"
    settings_path = config_dir / "settings.local.json"
    monkeypatch.setattr(settings_store, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(settings_store, "SETTINGS_PATH", settings_path)
    monkeypatch.setattr(settings_store, "_db", fake_db)
    return settings_path


def test_appdb_write_does_not_mirror_json_when_db_available(monkeypatch, tmp_path):
    """A healthy app-DB write is complete on its own; the JSON file is not kept as
    a live mirror."""
    from app import settings as settings_store

    fake_db = FakeSettingsDB(unavailable=settings_store._UNAVAILABLE)
    settings_path = _patch_store(monkeypatch, tmp_path, settings_store, fake_db)

    payload = {
        "theme": "dark",
        "poll_interval_secs": 12,
        settings_store.AGENT_OVERRIDES_KEY: {
            "proj:agent": {"harness": "codex", "unknown": "dropped"},
        },
    }

    settings_store._atomic_write(payload)

    assert fake_db.app["theme"] == "dark"
    assert fake_db.app["poll_interval_secs"] == 12
    assert fake_db.overrides == {"proj:agent": {"harness": "codex"}}
    assert not settings_path.exists()


def test_appdb_unavailable_writes_json_fallback(monkeypatch, tmp_path):
    """When the app-DB cannot answer, writes still persist to the local JSON
    fallback so a DB-less dev console remains usable."""
    from app import settings as settings_store

    fake_db = FakeSettingsDB(unavailable=settings_store._UNAVAILABLE, down=True)
    settings_path = _patch_store(monkeypatch, tmp_path, settings_store, fake_db)

    payload = {"theme": "light", "poll_interval_secs": 15}
    settings_store._atomic_write(payload)

    assert json.loads(settings_path.read_text(encoding="utf-8")) == payload


def test_existing_json_seed_imports_into_empty_appdb(monkeypatch, tmp_path):
    """An existing settings.local.json is imported once when the app-DB is reachable
    but empty, preserving the upgrade seed path."""
    from app import settings as settings_store

    fake_db = FakeSettingsDB(unavailable=settings_store._UNAVAILABLE)
    settings_path = _patch_store(monkeypatch, tmp_path, settings_store, fake_db)
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(
        json.dumps(
            {
                "theme": "light",
                "poll_interval_secs": 22,
                settings_store.AGENT_OVERRIDES_KEY: {
                    "proj:agent": {"model": "m", "designation": "INTERACTIVE"},
                },
            }
        ),
        encoding="utf-8",
    )

    assert settings_store.migrate_json_to_appdb() is True
    assert fake_db.app["theme"] == "light"
    assert fake_db.app["poll_interval_secs"] == 22
    assert fake_db.overrides == {
        "proj:agent": {"model": "m", "designation": "interactive"}
    }
    assert settings_path.exists()
