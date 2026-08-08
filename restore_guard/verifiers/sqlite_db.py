"""SQLite integrity checks — no container needed.

Home Assistant, Immich's thumbnails, *arr apps, Vaultwarden, Grafana: half a
homelab runs on SQLite files that copy just fine while being corrupt.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any

from . import VerifyContext, VerifyError, VerifyResult, Verifier, register
from .checks import QueryFailed, run_checks, validate_check_specs


@register
class SqliteVerifier(Verifier):
    type = "sqlite"

    def validate(self) -> None:
        self._required("path")
        self.query_specs = validate_check_specs(
            self.spec.get("queries"), f"job {self.job.name!r}: verify.sqlite.queries"
        )

    def run(self, ctx: VerifyContext) -> VerifyResult:
        started = time.monotonic()
        matches = sorted(ctx.restore_dir.glob(str(self.spec["path"])))
        if not matches:
            return self.failed(f"no database matching {self.spec['path']!r} in restore")
        db_path = matches[0]

        problems: list[str] = []
        details: dict[str, Any] = {"database": str(db_path.relative_to(ctx.restore_dir))}

        # Open read-only so a stray WAL replay cannot alter the restored copy.
        uri = f"file:{db_path}?mode=ro"
        try:
            connection = sqlite3.connect(uri, uri=True, timeout=30.0)
        except sqlite3.Error as exc:
            raise VerifyError(f"cannot open {db_path.name}: {exc}") from exc

        try:
            if self.spec.get("integrity_check", True):
                rows = connection.execute("PRAGMA integrity_check").fetchall()
                verdict = rows[0][0] if rows else "no result"
                details["integrity_check"] = verdict
                if verdict != "ok":
                    problems.append(f"integrity_check: {verdict}")

            if self.spec.get("foreign_key_check", False):
                broken = connection.execute("PRAGMA foreign_key_check").fetchall()
                details["foreign_key_violations"] = len(broken)
                if broken:
                    problems.append(f"{len(broken)} foreign key violation(s)")

            query_results, query_problems = run_checks(
                self.query_specs, lambda spec: _scalar(connection, spec)
            )
            problems += query_problems
            if query_results:
                details["queries"] = query_results
        except sqlite3.DatabaseError as exc:
            # A corrupt file typically blows up here rather than returning "not ok".
            problems.append(f"database error: {exc}")
        finally:
            connection.close()

        summary = f"{db_path.name}: " + ("; ".join(problems) if problems else "integrity ok")
        result = self.failed(summary, **details) if problems else self.ok(summary, **details)
        result.duration = time.monotonic() - started
        return result


def _scalar(connection: sqlite3.Connection, spec: dict[str, Any]):
    """Run one query and return its first cell."""
    try:
        row = connection.execute(str(spec["sql"])).fetchone()
    except sqlite3.Error as exc:
        raise QueryFailed(f"query failed ({exc})") from exc
    return row[0] if row else None
