"""Rendering: status table, JSON, and Prometheus metrics."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Config
from .state import STATUS_OK, RunRecord, State
from .util import human_bytes, human_duration

STATUS_OK_LABEL = "OK"
STATUS_STALE_LABEL = "STALE"
STATUS_FAIL_LABEL = "FAIL"
STATUS_NEVER_LABEL = "NEVER"
STATUS_OFF_LABEL = "OFF"


@dataclass
class JobStatus:
    """The answer to 'is this backup known-good right now?'"""

    name: str
    enabled: bool
    max_age: int
    last_run: RunRecord | None
    last_success: RunRecord | None

    @property
    def success_age(self) -> float | None:
        return None if self.last_success is None else self.last_success.age

    @property
    def stale(self) -> bool:
        if self.last_success is None:
            return True
        return self.success_age > self.max_age

    @property
    def label(self) -> str:
        if not self.enabled:
            return STATUS_OFF_LABEL
        if self.last_success is None:
            return STATUS_NEVER_LABEL
        if self.last_run is not None and not self.last_run.ok:
            return STATUS_FAIL_LABEL
        if self.stale:
            return STATUS_STALE_LABEL
        return STATUS_OK_LABEL

    @property
    def healthy(self) -> bool:
        return self.label in (STATUS_OK_LABEL, STATUS_OFF_LABEL)


def collect(config: Config, state: State) -> list[JobStatus]:
    statuses = []
    for job in config.jobs:
        statuses.append(
            JobStatus(
                name=job.name,
                enabled=job.enabled,
                max_age=job.max_age,
                last_run=state.last_run(job.name),
                last_success=state.last_success(job.name),
            )
        )
    return statuses


def status_table(statuses: list[JobStatus]) -> str:
    headers = ("JOB", "STATUS", "VERIFIED", "SNAPSHOT", "SIZE", "TOOK", "DETAIL")
    rows = [headers]

    for status in statuses:
        success = status.last_success
        run = status.last_run
        detail = ""
        if run is not None and not run.ok:
            detail = run.message.splitlines()[0][:60] if run.message else run.status
        elif status.stale and success is not None:
            detail = f"older than {human_duration(status.max_age)}"
        elif success is None:
            detail = "never successfully verified"

        rows.append(
            (
                status.name,
                status.label,
                human_duration(status.success_age) + " ago" if success else "never",
                (success.snapshot_id or "-")[:12] if success else "-",
                human_bytes(success.bytes) if success else "-",
                human_duration(success.duration) if success else "-",
                detail,
            )
        )

    widths = [max(len(str(row[i])) for row in rows) for i in range(len(headers))]
    lines = []
    for index, row in enumerate(rows):
        line = "  ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(row)).rstrip()
        lines.append(line)
        if index == 0:
            lines.append("  ".join("-" * width for width in widths))
    return "\n".join(lines)


def history_table(records: list[RunRecord]) -> str:
    if not records:
        return "no runs recorded yet"
    rows = [("WHEN", "JOB", "STATUS", "SNAPSHOT", "TOOK", "MESSAGE")]
    for record in records:
        rows.append(
            (
                time.strftime("%Y-%m-%d %H:%M", time.localtime(record.started_at)),
                record.job,
                record.status,
                (record.snapshot_id or "-")[:12],
                human_duration(record.duration),
                (record.message.splitlines()[0][:70] if record.message else ""),
            )
        )
    widths = [max(len(str(row[i])) for row in rows) for i in range(len(rows[0]))]
    lines = []
    for index, row in enumerate(rows):
        lines.append("  ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(row)).rstrip())
        if index == 0:
            lines.append("  ".join("-" * width for width in widths))
    return "\n".join(lines)


def json_report(statuses: list[JobStatus]) -> str:
    payload: dict[str, Any] = {
        "generated_at": time.time(),
        "healthy": all(status.healthy for status in statuses),
        "jobs": [],
    }
    for status in statuses:
        payload["jobs"].append(
            {
                "name": status.name,
                "status": status.label,
                "enabled": status.enabled,
                "stale": status.stale,
                "max_age_seconds": status.max_age,
                "last_success_age_seconds": status.success_age,
                "last_success": status.last_success.to_dict() if status.last_success else None,
                "last_run": status.last_run.to_dict() if status.last_run else None,
            }
        )
    return json.dumps(payload, indent=2, default=str)


def prometheus_metrics(statuses: list[JobStatus]) -> str:
    """Textfile-collector format — point node_exporter at the output file."""
    lines = [
        "# HELP restore_guard_last_success_timestamp_seconds Unix time of the last verified restore.",
        "# TYPE restore_guard_last_success_timestamp_seconds gauge",
    ]
    for status in statuses:
        value = status.last_success.finished_at if status.last_success else 0
        lines.append(
            f'restore_guard_last_success_timestamp_seconds{{job="{_escape(status.name)}"}} {value:.0f}'
        )

    lines += [
        "# HELP restore_guard_stale Whether the last verified restore is older than max_age.",
        "# TYPE restore_guard_stale gauge",
    ]
    for status in statuses:
        lines.append(
            f'restore_guard_stale{{job="{_escape(status.name)}"}} {1 if status.stale else 0}'
        )

    lines += [
        "# HELP restore_guard_last_run_success Whether the most recent run passed.",
        "# TYPE restore_guard_last_run_success gauge",
    ]
    for status in statuses:
        ok = 1 if (status.last_run and status.last_run.status == STATUS_OK) else 0
        lines.append(f'restore_guard_last_run_success{{job="{_escape(status.name)}"}} {ok}')

    lines += [
        "# HELP restore_guard_last_run_duration_seconds Duration of the most recent run.",
        "# TYPE restore_guard_last_run_duration_seconds gauge",
        "# HELP restore_guard_last_restore_bytes Bytes restored in the last successful run.",
        "# TYPE restore_guard_last_restore_bytes gauge",
    ]
    for status in statuses:
        duration = status.last_run.duration if status.last_run else 0
        lines.append(
            f'restore_guard_last_run_duration_seconds{{job="{_escape(status.name)}"}} {duration:.1f}'
        )
    for status in statuses:
        size = status.last_success.bytes if status.last_success else 0
        lines.append(f'restore_guard_last_restore_bytes{{job="{_escape(status.name)}"}} {size}')

    return "\n".join(lines) + "\n"


def write_metrics(path: Path, content: str) -> None:
    """Write atomically: node_exporter may read the file at any moment."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(content, encoding="utf-8")
    temp.replace(path)


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')
