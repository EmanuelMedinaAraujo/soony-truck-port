import os
import sqlite3
from pathlib import Path

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
