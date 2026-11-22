import os
import sqlite3
from contextlib import closing
from hashlib import sha256
from pathlib import Path

from fastapi.testclient import TestClient

from core import actions


def _open_handles(path: Path) -> int:
    fd_dir = Path("/proc/self/fd")
    return sum(1 for fd in fd_dir.iterdir() if os.path.realpath(fd) == str(path.resolve()))


def test_tx_closes_connection(tmp_path, monkeypatch):
    """Regression: `with sqlite3.connect() as c` never closes; the server ran out of file handles."""
    monkeypatch.setattr(actions, "DB", tmp_path / "actions.db")
    for _ in range(50):
        actions.log_event("test", None, "hello")
    assert _open_handles(actions.DB) == 0
    with sqlite3.connect(actions.DB) as c:
        assert c.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 50


def test_dashboard_gets_use_read_only_connections(tmp_path, monkeypatch):
    from core import ingest, rules
    from server import app, discord_bot

    monkeypatch.setattr(actions, "DB", tmp_path / "actions #1.db")
    monkeypatch.setattr(rules, "DB_PATH", tmp_path / "master #1.db")
    monkeypatch.setenv("NO_LOOP", "1")
    monkeypatch.setattr(discord_bot, "start", lambda *_: None)
    with closing(sqlite3.connect(rules.DB_PATH)) as db, db:
        db.executescript(ingest.SCHEMA)
        db.execute("INSERT INTO launch_status VALUES (?, ?, 100, 0, 100, 60, 'packing')",
                   (rules.cfg("priority_product"), rules.now().isoformat()))
        db.execute("INSERT INTO components(component_id, description) VALUES ('cards', 'Cards')")
        db.execute("INSERT INTO bom(parent_id, child_id, qty_per) VALUES (?, 'cards', 1)",
                   (rules.cfg("priority_product"),))

    with TestClient(app.app) as client:
        # The first write after startup uses tables that startup already initialized.
        with closing(sqlite3.connect(actions.DB)) as db, db:
            db.execute("INSERT INTO actions(id, finding_id, title, kind, status, cost_eur) "
                       "VALUES ('action', 'gap-cards', 'Ask about cards', 'ask', 'proposed', 0)")
            db.execute("INSERT INTO messages(id, action_id, channel, to_name) "
                       "VALUES ('call', 'action', 'phone_script', 'Supplier')")
            db.execute("INSERT INTO replies(id, action_id, from_name, body) "
                       "VALUES ('reply', 'action', 'Supplier', 'Cards are ready.')")
        connect = sqlite3.connect

        def read_only_connect(database, *args, **kwargs):
            assert kwargs.get("uri") and database.endswith("?mode=ro")
            connection = connect(database, *args, **kwargs)
            connection.set_authorizer(lambda operation, *_: sqlite3.SQLITE_OK if operation in {
                sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_TRANSACTION,
            } else sqlite3.SQLITE_DENY)
            return connection

        monkeypatch.setattr(sqlite3, "connect", read_only_connect)
        paths = (actions.DB, rules.DB_PATH)
        for endpoint in ("/api/dispatch", "/api/parts/cards/graph", "/api/calls", "/api/calls/call",
                         "/api/chaser", "/api/state", "/api/me", "/api/replies/reply"):
            before = [sha256(path.read_bytes()).digest() for path in paths]
            response = client.get(endpoint)
            assert response.status_code == 200, (endpoint, response.text)
            assert [sha256(path.read_bytes()).digest() for path in paths] == before, endpoint
