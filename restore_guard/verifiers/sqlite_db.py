"""SQLite integrity checks — no container needed.

Home Assistant, Immich's thumbnails, *arr apps, Vaultwarden, Grafana: half a
homelab runs on SQLite files that copy just fine while being corrupt.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any

from ..config import ConfigError
from . import VerifyContext, VerifyError, VerifyResult, Verifier, register


@register
class SqliteVerifier(Verifier):
    type = "sqlite"

    def validate(self) -> None:
        self._required("path")
        for position, check in enumerate(self.spec.get("queries") or []):
            if not isinstance(check, dict) or not check.get("sql"):
                raise ConfigError(
                    f"job {self.job.name!r}: verify.sqlite.queries[{position}] needs 'sql'"
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

            query_results = []
            for check in self.spec.get("queries") or []:
                outcome = _run_query(connection, check)
                query_results.append(outcome)
                if outcome["problem"]:
                    problems.append(outcome["problem"])
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


def _run_query(connection: sqlite3.Connection, check: dict[str, Any]) -> dict[str, Any]:
    sql = str(check["sql"])
    label = str(check.get("name") or sql[:60])
    try:
        row = connection.execute(sql).fetchone()
    except sqlite3.Error as exc:
        return {"name": label, "value": None, "problem": f"{label}: query failed ({exc})"}

    value = row[0] if row else None
    problem = _compare(label, value, check)
    return {"name": label, "value": value, "problem": problem}


def _compare(label: str, value: Any, check: dict[str, Any]) -> str | None:
    """Shared expectation logic: min/max/equals against the first cell."""
    if "expect_min" in check or "expect_max" in check:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return f"{label}: expected a number, got {value!r}"
        minimum = check.get("expect_min")
        maximum = check.get("expect_max")
        if minimum is not None and numeric < float(minimum):
            return f"{label}: {_fmt(numeric)} < expected minimum {minimum}"
        if maximum is not None and numeric > float(maximum):
            return f"{label}: {_fmt(numeric)} > expected maximum {maximum}"
    if "expect_equals" in check and str(value) != str(check["expect_equals"]):
        return f"{label}: got {value!r}, expected {check['expect_equals']!r}"
    return None


def _fmt(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:.3f}"
