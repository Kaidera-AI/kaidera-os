from app.appdb import SettingsDB


class _Cursor:
    def __init__(self):
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params):
        self.calls.append((sql, params))


class _Conn:
    closed = 0

    def __init__(self):
        self.cursor_obj = _Cursor()

    def cursor(self):
        return self.cursor_obj


def test_set_awaiting_approval_requeues_existing_rows(monkeypatch):
    conn = _Conn()
    db = SettingsDB(dsn="unused")
    monkeypatch.setattr(db, "_get_conn", lambda: conn)

    assert db.set_awaiting_approval(" kaidera-os ", " h-1 ") is True

    sql, params = conn.cursor_obj.calls[0]
    assert "ON CONFLICT (project, handoff_id) DO UPDATE" in sql
    assert "SET status = 'awaiting'" in sql
    assert params == ("kaidera-os", "h-1")
