"""Notifications: ntfy, generic webhook, or shell out to anything else."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from .config import Config, NotifyTarget
from .runner import JobOutcome
from .state import STATUS_ERROR
from .util import Logger, human_duration, run


def notify(config: Config, outcomes: list[JobOutcome], logger: Logger) -> None:
    if not config.notify:
        return
    for target in config.notify:
        events = _events_for(target, outcomes)
        if not events:
            continue
        title, body, priority = _compose(events)
        try:
            _dispatch(target, title, body, priority)
            logger.debug(f"notified via {target.kind}")
        except Exception as exc:  # a broken webhook must never fail the run
            logger.fail(f"notification via {target.kind} failed: {exc}")


def _events_for(target: NotifyTarget, outcomes: list[JobOutcome]) -> list[tuple[str, JobOutcome]]:
    selected = []
    for outcome in outcomes:
        if outcome.is_recovery and "recovery" in target.events:
            selected.append(("recovery", outcome))
        elif not outcome.record.ok and "failure" in target.events:
            selected.append(("failure", outcome))
        elif outcome.record.ok and "success" in target.events:
            selected.append(("success", outcome))
    return selected


def _compose(events: list[tuple[str, JobOutcome]]) -> tuple[str, str, int]:
    failures = [outcome for kind, outcome in events if kind == "failure"]
    recoveries = [outcome for kind, outcome in events if kind == "recovery"]
    successes = [outcome for kind, outcome in events if kind == "success"]

    if failures:
        names = ", ".join(o.record.job for o in failures)
        title = f"Backup NOT restorable: {names}"
        priority = 5 if any(o.record.status == STATUS_ERROR for o in failures) else 4
    elif recoveries:
        title = "Backup restorable again: " + ", ".join(o.record.job for o in recoveries)
        priority = 3
    else:
        title = f"{len(successes)} backup(s) verified"
        priority = 2

    lines = []
    for kind, outcome in events:
        record = outcome.record
        marker = {"failure": "FAIL", "recovery": "RECOVERED", "success": "OK"}[kind]
        snapshot = record.snapshot_id[:12] if record.snapshot_id else "-"
        lines.append(
            f"[{marker}] {record.job} (snapshot {snapshot}, "
            f"{human_duration(record.duration)})"
        )
        if record.message and kind != "success":
            lines.append(f"        {record.message.splitlines()[0][:200]}")
    return title, "\n".join(lines), priority


def _dispatch(target: NotifyTarget, title: str, body: str, priority: int) -> None:
    kind = target.kind.lower()
    if kind == "ntfy":
        _ntfy(target.spec, title, body, priority)
    elif kind == "webhook":
        _webhook(target.spec, title, body, priority)
    elif kind == "command":
        _command(target.spec, title, body)
    else:
        raise ValueError(f"unknown notifier {target.kind!r} (ntfy, webhook, command)")


def _ntfy(spec: dict[str, Any], title: str, body: str, priority: int) -> None:
    url = spec.get("url")
    if not url:
        raise ValueError("notify.ntfy.url is required")
    headers = {
        "Title": title.encode("utf-8").decode("latin-1", "replace"),
        "Priority": str(spec.get("priority", priority)),
        "Tags": ",".join(spec.get("tags") or ["floppy_disk"]),
    }
    if spec.get("token"):
        headers["Authorization"] = f"Bearer {spec['token']}"
    _post(str(url), body.encode("utf-8"), headers, float(spec.get("timeout", 15)))


def _webhook(spec: dict[str, Any], title: str, body: str, priority: int) -> None:
    url = spec.get("url")
    if not url:
        raise ValueError("notify.webhook.url is required")
    payload = {"title": title, "text": body, "priority": priority}
    payload.update(spec.get("extra_fields") or {})
    headers = {"Content-Type": "application/json"}
    headers.update({str(k): str(v) for k, v in (spec.get("headers") or {}).items()})
    _post(str(url), json.dumps(payload).encode("utf-8"), headers, float(spec.get("timeout", 15)))


def _command(spec: dict[str, Any], title: str, body: str) -> None:
    argv = spec.get("run")
    if not argv or not isinstance(argv, list):
        raise ValueError("notify.command.run must be a list, e.g. ['/usr/local/bin/alert.sh']")
    result = run(
        [str(part) for part in argv],
        timeout=float(spec.get("timeout", 60)),
        env={"RESTORE_GUARD_TITLE": title, "RESTORE_GUARD_BODY": body},
        stdin_text=f"{title}\n\n{body}\n",
    )
    if not result.ok:
        raise RuntimeError(result.tail())


def _post(url: str, data: bytes, headers: dict[str, str], timeout: float) -> None:
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read(4096)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code} from {url}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"cannot reach {url}: {exc.reason}") from exc
