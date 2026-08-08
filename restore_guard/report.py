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


_HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="300">
<title>restore-guard</title>
<style>
:root {{
  color-scheme: light dark;
  --bg: #f6f7f9; --fg: #14161a; --muted: #5b6270; --card: #ffffff;
  --line: #dfe3ea; --ok: #1a7f47; --warn: #9a6700; --bad: #b42318; --off: #6b7280;
}}
@media (prefers-color-scheme: dark) {{
  :root {{
    --bg: #14161a; --fg: #e7e9ee; --muted: #9aa2b1; --card: #1c1f26;
    --line: #2c313b; --ok: #4ade80; --warn: #fbbf24; --bad: #f87171; --off: #7c8394;
  }}
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0; padding: 2rem 1rem; background: var(--bg); color: var(--fg);
  font: 15px/1.5 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
}}
main {{ max-width: 62rem; margin: 0 auto; }}
h1 {{ font-size: 1.35rem; margin: 0 0 .25rem; letter-spacing: -.01em; }}
.sub {{ color: var(--muted); font-size: .875rem; margin: 0 0 1.5rem; }}
.banner {{
  padding: .75rem 1rem; border-radius: .5rem; margin-bottom: 1.5rem;
  font-weight: 600; border: 1px solid var(--line); background: var(--card);
}}
.banner.ok {{ color: var(--ok); }}
.banner.bad {{ color: var(--bad); }}
.wrap {{ overflow-x: auto; background: var(--card); border: 1px solid var(--line); border-radius: .5rem; }}
table {{ border-collapse: collapse; width: 100%; font-size: .9rem; }}
th, td {{ text-align: left; padding: .7rem .9rem; border-bottom: 1px solid var(--line); white-space: nowrap; }}
th {{ font-size: .75rem; text-transform: uppercase; letter-spacing: .04em; color: var(--muted); font-weight: 600; }}
tr:last-child td {{ border-bottom: 0; }}
td.detail {{ white-space: normal; color: var(--muted); min-width: 16rem; }}
.pill {{
  display: inline-block; padding: .15rem .5rem; border-radius: 999px;
  font-size: .75rem; font-weight: 700; letter-spacing: .03em;
  border: 1px solid currentColor;
}}
.OK {{ color: var(--ok); }} .STALE {{ color: var(--warn); }}
.FAIL, .NEVER {{ color: var(--bad); }} .OFF {{ color: var(--off); }}
.job {{ font-weight: 600; }}
code {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: .85em; color: var(--muted); }}
footer {{ margin-top: 1.25rem; color: var(--muted); font-size: .8rem; }}
</style>
</head>
<body>
<main>
  <h1>restore-guard</h1>
  <p class="sub">When was each backup last proven restorable?</p>
  <div class="banner {banner_class}">{banner_text}</div>
  <div class="wrap">
    <table>
      <thead>
        <tr><th>Job</th><th>Status</th><th>Verified</th><th>Snapshot</th>
            <th>Size</th><th>Took</th><th>Detail</th></tr>
      </thead>
      <tbody>
{rows}
      </tbody>
    </table>
  </div>
  <footer>Generated {generated} &middot; page refreshes every 5 minutes</footer>
</main>
</body>
</html>
"""


def html_report(statuses: list[JobStatus]) -> str:
    """A self-contained status page for a homelab dashboard.

    No external assets: an artifact that reports on backups must not need the
    network to render, least of all while you are recovering from an outage.
    """
    broken = [s for s in statuses if not s.healthy]
    if broken:
        banner_class = "bad"
        banner_text = f"{len(broken)} of {len(statuses)} job(s) need attention"
    else:
        banner_class = "ok"
        banner_text = f"All {len(statuses)} job(s) verified restorable"

    rows = []
    for status in statuses:
        success = status.last_success
        run = status.last_run
        if run is not None and not run.ok and run.message:
            detail = run.message.splitlines()[0][:200]
        elif status.stale and success is not None:
            detail = f"older than {human_duration(status.max_age)}"
        elif success is None:
            detail = "never successfully verified"
        else:
            detail = ""

        rows.append(
            "        <tr>"
            f"<td class=\"job\">{_html(status.name)}</td>"
            f"<td><span class=\"pill {status.label}\">{status.label}</span></td>"
            f"<td>{_html(human_duration(status.success_age) + ' ago') if success else 'never'}</td>"
            f"<td><code>{_html((success.snapshot_id or '-')[:24]) if success else '-'}</code></td>"
            f"<td>{human_bytes(success.bytes) if success else '-'}</td>"
            f"<td>{human_duration(success.duration) if success else '-'}</td>"
            f"<td class=\"detail\">{_html(detail)}</td>"
            "</tr>"
        )

    return _HTML_TEMPLATE.format(
        banner_class=banner_class,
        banner_text=_html(banner_text),
        rows="\n".join(rows),
        generated=time.strftime("%Y-%m-%d %H:%M:%S %Z"),
    )


def _html(value: str) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def write_atomic(path: Path, content: str) -> None:
    """Write atomically: node_exporter may read the file at any moment."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(content, encoding="utf-8")
    temp.replace(path)


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')
