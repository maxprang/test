import sqlite3
import sys
import tarfile
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture
def backup_dir(tmp_path: Path) -> Path:
    """A 'backup repository': three snapshot directories, oldest to newest."""
    repo = tmp_path / "backups"
    repo.mkdir()
    for index, name in enumerate(["snap-2026-08-06", "snap-2026-08-07", "snap-2026-08-08"]):
        snapshot = repo / name
        (snapshot / "etc").mkdir(parents=True)
        (snapshot / "etc" / "config.yaml").write_text(f"generation: {index}\n")
        (snapshot / "data.bin").write_bytes(b"x" * (2048 * (index + 1)))
        stamp = time.time() - (86400 * (2 - index))
        for path in snapshot.rglob("*"):
            import os

            os.utime(path, (stamp, stamp))
        import os

        os.utime(snapshot, (stamp, stamp))
    return repo


@pytest.fixture
def tar_backup_dir(tmp_path: Path) -> Path:
    repo = tmp_path / "tarballs"
    repo.mkdir()
    payload = tmp_path / "payload"
    (payload / "app").mkdir(parents=True)
    (payload / "app" / "settings.json").write_text('{"ok": true}')
    archive_path = repo / "app-2026-08-08.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        archive.add(payload, arcname=".")
    return repo


@pytest.fixture
def sqlite_backup_dir(tmp_path: Path) -> Path:
    """A snapshot containing a healthy SQLite database with two users."""
    repo = tmp_path / "sqlite-backups"
    snapshot = repo / "nightly"
    snapshot.mkdir(parents=True)
    db_path = snapshot / "app.db"
    connection = sqlite3.connect(db_path)
    connection.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT)")
    connection.executemany(
        "INSERT INTO users (email) VALUES (?)", [("a@example.org",), ("b@example.org",)]
    )
    connection.commit()
    connection.close()
    return repo


def make_config(workdir: Path, jobs: list[dict], **defaults) -> dict:
    base = {"workdir": str(workdir), "keep_on_failure": False}
    base.update(defaults)
    return {"version": 1, "defaults": base, "jobs": jobs}
