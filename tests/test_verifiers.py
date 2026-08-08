import sqlite3
import time

import pytest

from restore_guard.config import ConfigError, JobConfig
from restore_guard.sources import Snapshot
from restore_guard.util import Logger
from restore_guard.verifiers import VerifyContext, build_verifiers


def context(restore_dir, verify_specs, timeout=60):
    job = JobConfig(
        name="demo",
        source={"type": "local", "repository": "/tmp"},
        verify=verify_specs,
    )
    ctx = VerifyContext(
        restore_dir=restore_dir,
        snapshot=Snapshot(id="abc123", time=time.time(), label="abc123"),
        job=job,
        timeout=timeout,
        log=Logger(quiet=True),
    )
    return build_verifiers(job), ctx


@pytest.fixture
def restored(tmp_path):
    root = tmp_path / "restore"
    (root / "etc").mkdir(parents=True)
    (root / "etc" / "config.yaml").write_text("key: value\n")
    (root / "blob.bin").write_bytes(b"y" * 4096)
    return root


# -- files ------------------------------------------------------------


def test_files_passes_when_conditions_met(restored):
    verifiers, ctx = context(restored, [{"type": "files", "min_files": 2, "min_bytes": "4KiB"}])
    result = verifiers[0].run(ctx)
    assert result.ok, result.summary
    assert result.details["files"] == 2


def test_files_catches_near_empty_backup(restored):
    verifiers, ctx = context(restored, [{"type": "files", "min_bytes": "1GiB"}])
    result = verifiers[0].run(ctx)
    assert not result.ok
    assert "expected >=" in result.summary


def test_files_must_exist_reports_missing_path(restored):
    verifiers, ctx = context(
        restored, [{"type": "files", "must_exist": ["etc/config.yaml", "**/secrets.env"]}]
    )
    result = verifiers[0].run(ctx)
    assert not result.ok
    assert "secrets.env" in result.summary


def test_files_newer_than_catches_stale_content(restored):
    import os

    old = time.time() - 10 * 86400
    for path in restored.rglob("*"):
        os.utime(path, (old, old))
    verifiers, ctx = context(restored, [{"type": "files", "newer_than": "2d"}])
    result = verifiers[0].run(ctx)
    assert not result.ok
    assert "old" in result.summary


def test_files_requires_at_least_one_condition():
    with pytest.raises(ConfigError, match="at least one condition"):
        context("/tmp", [{"type": "files"}])


# -- sqlite -----------------------------------------------------------


def test_sqlite_healthy_database(sqlite_backup_dir, tmp_path):
    restore = sqlite_backup_dir / "nightly"
    verifiers, ctx = context(
        restore,
        [
            {
                "type": "sqlite",
                "path": "*.db",
                "queries": [{"sql": "SELECT count(*) FROM users", "expect_min": 2}],
            }
        ],
    )
    result = verifiers[0].run(ctx)
    assert result.ok, result.summary
    assert result.details["integrity_check"] == "ok"


def test_sqlite_detects_too_few_rows(sqlite_backup_dir):
    restore = sqlite_backup_dir / "nightly"
    verifiers, ctx = context(
        restore,
        [
            {
                "type": "sqlite",
                "path": "*.db",
                "queries": [
                    {"name": "users", "sql": "SELECT count(*) FROM users", "expect_min": 500}
                ],
            }
        ],
    )
    result = verifiers[0].run(ctx)
    assert not result.ok
    assert "users: 2 < expected minimum 500" in result.summary


def test_sqlite_detects_corruption(tmp_path):
    restore = tmp_path / "restore"
    restore.mkdir()
    db_path = restore / "app.db"
    connection = sqlite3.connect(db_path)
    connection.execute("CREATE TABLE t (a INTEGER)")
    connection.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(500)])
    connection.commit()
    connection.close()

    # Scribble over the middle of the file, the way a half-written backup looks.
    with open(db_path, "r+b") as handle:
        handle.seek(4096)
        handle.write(b"\x00" * 4096)

    verifiers, ctx = context(restore, [{"type": "sqlite", "path": "*.db"}])
    result = verifiers[0].run(ctx)
    assert not result.ok


def test_sqlite_missing_file_fails(tmp_path):
    restore = tmp_path / "restore"
    restore.mkdir()
    verifiers, ctx = context(restore, [{"type": "sqlite", "path": "*.db"}])
    result = verifiers[0].run(ctx)
    assert not result.ok
    assert "no database matching" in result.summary


# -- command ----------------------------------------------------------


def test_command_success(restored):
    verifiers, ctx = context(
        restored, [{"type": "command", "run": ["test", "-f", "etc/config.yaml"]}]
    )
    assert verifiers[0].run(ctx).ok


def test_command_failure_is_reported(restored):
    verifiers, ctx = context(restored, [{"type": "command", "run": ["false"]}])
    result = verifiers[0].run(ctx)
    assert not result.ok
    assert "exit code 1" in result.summary


def test_command_placeholder_substitution(restored):
    verifiers, ctx = context(
        restored,
        [
            {
                "type": "command",
                "shell": True,
                "run": "ls {restore_dir} && echo snapshot={snapshot}",
                "stdout_contains": ["snapshot=abc123", "blob.bin"],
            }
        ],
    )
    assert verifiers[0].run(ctx).ok


def test_command_string_without_shell_is_rejected():
    with pytest.raises(ConfigError, match="pass a list"):
        context("/tmp", [{"type": "command", "run": "rm -rf /"}])


def test_context_refuses_paths_outside_the_restore(restored):
    _, ctx = context(restored, [{"type": "files", "min_files": 1}])
    with pytest.raises(Exception, match="outside the restore directory"):
        ctx.resolve("../../etc")
