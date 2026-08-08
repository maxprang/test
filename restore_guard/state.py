"""Run history in a single SQLite file.

The whole point of the tool is the question "when was this backup last *proven*
restorable?", so history is not optional bookkeeping — it is the product.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 1

STATUS_OK = "ok"
STATUS_FAILED = "failed"  # restore worked, a check said no
STATUS_ERROR = "error"  # restore itself broke (repo unreachable, timeout, ...)
STATUS_SKIPPED = "skipped"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    job           TEXT    NOT NULL,
    status        TEXT    NOT NULL,
    started_at    REAL    NOT NULL,
    finished_at   REAL    NOT NULL,
    duration      REAL    NOT NULL,
    snapshot_id   TEXT,
    snapshot_time REAL,
    files         INTEGER NOT NULL DEFAULT 0,
    bytes         INTEGER NOT NULL DEFAULT 0,
    message       TEXT    NOT NULL DEFAULT '',
    details       TEXT    NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS runs_job_started ON runs (job, started_at DESC);
CREATE INDEX IF NOT EXISTS runs_job_status  ON runs (job, status, started_at DESC);
"""


@dataclass
class RunRecord:
    job: str
    status: str
    started_at: float
    finished_at: float
    duration: float
    snapshot_id: str | None = None
    snapshot_time: float | None = None
    files: int = 0
    bytes: int = 0
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    id: int | None = None

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK

    @property
    def age(self) -> float:
        return max(0.0, time.time() - self.finished_at)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["ok"] = self.ok
        return data


class State:
    """Thread-safe wrapper around SQLite.

    Jobs may run in parallel (``run --parallel N``), so every access goes
    through one lock. The critical sections are microseconds of SQLite work,
    never a restore, so contention is irrelevant.
    """

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), timeout=30.0, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        current = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if current == 0:
            self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        elif current > SCHEMA_VERSION:
            raise RuntimeError(
                f"{self.path} was written by a newer restore-guard "
                f"(schema {current} > {SCHEMA_VERSION})"
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "State":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def record(self, run: RunRecord) -> RunRecord:
        with self._lock:
            cursor = self._conn.execute(
                """
                INSERT INTO runs (job, status, started_at, finished_at, duration,
                                  snapshot_id, snapshot_time, files, bytes, message, details)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run.job,
                    run.status,
                    run.started_at,
                    run.finished_at,
                    run.duration,
                    run.snapshot_id,
                    run.snapshot_time,
                    run.files,
                    run.bytes,
                    run.message,
                    json.dumps(run.details, default=str),
                ),
            )
            self._conn.commit()
            run.id = cursor.lastrowid
            return run

    def last_run(self, job: str) -> RunRecord | None:
        return self._one(
            "SELECT * FROM runs WHERE job = ? ORDER BY started_at DESC LIMIT 1", (job,)
        )

    def last_success(self, job: str) -> RunRecord | None:
        return self._one(
            "SELECT * FROM runs WHERE job = ? AND status = ? ORDER BY started_at DESC LIMIT 1",
            (job, STATUS_OK),
        )

    def history(self, job: str | None = None, limit: int = 20) -> list[RunRecord]:
        with self._lock:
            if job:
                rows = self._conn.execute(
                    "SELECT * FROM runs WHERE job = ? ORDER BY started_at DESC LIMIT ?",
                    (job, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)
                ).fetchall()
        return [_row_to_record(row) for row in rows]

    def jobs_seen(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute("SELECT DISTINCT job FROM runs ORDER BY job").fetchall()
        return [row["job"] for row in rows]

    def prune(self, keep_per_job: int) -> int:
        """Keep only the newest ``keep_per_job`` runs per job. Returns rows deleted."""
        if keep_per_job <= 0:
            return 0
        with self._lock:
            cursor = self._conn.execute(
                """
                DELETE FROM runs
                WHERE id NOT IN (
                    SELECT id FROM runs r
                    WHERE (
                        SELECT COUNT(*) FROM runs r2
                        WHERE r2.job = r.job AND r2.started_at >= r.started_at
                    ) <= ?
                )
                """,
                (keep_per_job,),
            )
            self._conn.commit()
            return cursor.rowcount

    def _one(self, sql: str, params: Iterable[Any]) -> RunRecord | None:
        with self._lock:
            row = self._conn.execute(sql, tuple(params)).fetchone()
        return _row_to_record(row) if row else None


def _row_to_record(row: sqlite3.Row) -> RunRecord:
    try:
        details = json.loads(row["details"])
    except (ValueError, TypeError):
        details = {}
    return RunRecord(
        id=row["id"],
        job=row["job"],
        status=row["status"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        duration=row["duration"],
        snapshot_id=row["snapshot_id"],
        snapshot_time=row["snapshot_time"],
        files=row["files"],
        bytes=row["bytes"],
        message=row["message"],
        details=details,
    )
