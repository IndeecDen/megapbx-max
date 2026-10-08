from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path


def run_backup(source: Path, destination: Path) -> subprocess.CompletedProcess[str]:
    installer = (Path(__file__).resolve().parents[1] / "install.sh").read_text(encoding="utf-8")
    # Exercise the exact Python backup program shipped inside the installer.
    program = installer.split("<<'PY' || return 1\n", 1)[1].split("\nPY\n", 1)[0]
    return subprocess.run(
        [sys.executable, "-", str(source), str(destination)],
        input=program, text=True, capture_output=True, timeout=70,
    )


def test_installer_backup_includes_committed_wal_only(tmp_path: Path) -> None:
    source = tmp_path / "live.sqlite3"
    destination = tmp_path / "backup.sqlite3"
    with sqlite3.connect(source) as live:
        live.execute("PRAGMA journal_mode=WAL")
        live.execute("PRAGMA wal_autocheckpoint=0")
        live.execute("CREATE TABLE events (id INTEGER PRIMARY KEY)")
        live.execute("INSERT INTO events VALUES (1)")
        live.commit()
        live.execute("INSERT INTO events VALUES (2)")
        assert Path(str(source) + "-wal").stat().st_size > 0
        result = run_backup(source, destination)
        assert result.returncode == 0, result.stderr
        with sqlite3.connect(destination) as backup:
            assert backup.execute("PRAGMA quick_check").fetchall() == [("ok",)]
            assert backup.execute("SELECT id FROM events").fetchall() == [(1,)]
        live.rollback()
        assert live.execute("SELECT id FROM events").fetchall() == [(1,)]


def test_installer_backup_failure_removes_partial_snapshot(tmp_path: Path) -> None:
    source = tmp_path / "corrupt.sqlite3"
    source.write_bytes(b"not a sqlite database")
    destination = tmp_path / "backup.sqlite3"
    result = run_backup(source, destination)
    assert result.returncode != 0
    assert "SQLite backup failed" in result.stderr
    assert not destination.exists()
    assert source.read_bytes() == b"not a sqlite database"
