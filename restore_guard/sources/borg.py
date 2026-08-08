"""BorgBackup repositories."""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from ..util import CommandError, require_binary, run
from . import RestoreOutcome, Snapshot, Source, SourceError, register


@register
class BorgSource(Source):
    type = "borg"

    def validate(self) -> None:
        super().validate()
        self._required("repository")

    @property
    def binary(self) -> str:
        return str(self.spec.get("binary", "borg"))

    def _env(self) -> dict[str, str]:
        env: dict[str, str] = {
            "BORG_REPO": str(self.spec["repository"]),
            # A verifier must never block on an interactive prompt in a cron job.
            "BORG_UNKNOWN_UNENCRYPTED_REPO_ACCESS_IS_OK": "yes",
            "BORG_RELOCATED_REPO_ACCESS_IS_OK": "yes",
        }
        if self.spec.get("passphrase"):
            env["BORG_PASSPHRASE"] = str(self.spec["passphrase"])
        if self.spec.get("passcommand"):
            env["BORG_PASSCOMMAND"] = str(self.spec["passcommand"])
        if self.spec.get("rsh"):
            env["BORG_RSH"] = str(self.spec["rsh"])
        env.update({k: str(v) for k, v in (self.spec.get("env") or {}).items()})
        return env

    def preflight(self) -> None:
        require_binary(self.binary)
        result = run(
            [self.binary, "info", str(self.spec["repository"])], env=self._env(), timeout=120
        )
        if not result.ok:
            raise SourceError(
                f"borg repository {self.spec['repository']!r} not readable: {result.tail()}"
            )

    def list_snapshots(self) -> list[Snapshot]:
        argv = [self.binary, "list", "--json", str(self.spec["repository"])]
        for prefix in _as_list(self.spec.get("prefix")):
            argv += ["--glob-archives", f"{prefix}*"]
        result = run(argv, env=self._env(), timeout=300)
        if not result.ok:
            raise SourceError(f"borg list failed: {result.tail()}")
        try:
            payload = json.loads(result.stdout or "{}")
        except ValueError as exc:
            raise SourceError(f"borg returned unparsable JSON: {exc}") from exc

        snapshots = []
        for entry in payload.get("archives", []):
            name = str(entry.get("archive") or entry.get("name") or "")
            snapshots.append(
                Snapshot(
                    id=name,
                    time=_parse_time(entry.get("time") or entry.get("start")),
                    label=name,
                    raw=entry,
                )
            )
        return snapshots

    def restore(self, snapshot: Snapshot, dest: Path, timeout: float) -> RestoreOutcome:
        # borg extract writes into the current working directory, so we chdir
        # into the (already created, empty) restore target instead of passing it.
        argv = [self.binary, "extract", f"{self.spec['repository']}::{snapshot.id}"]
        argv += [str(p) for p in _as_list(self.spec.get("paths"))]
        for pattern in _as_list(self.spec.get("exclude")):
            argv += ["--exclude", str(pattern)]

        started = time.monotonic()
        try:
            result = run(argv, env=self._env(), timeout=timeout, cwd=dest)
        except CommandError as exc:
            raise SourceError(str(exc)) from exc
        if not result.ok:
            hint = "timed out" if result.timed_out else result.tail()
            raise SourceError(f"borg extract of {snapshot.short} failed: {hint}")

        return RestoreOutcome(
            snapshot=snapshot,
            dest=dest,
            duration=time.monotonic() - started,
            log=result.stderr.strip(),
        )


def _as_list(value: Any) -> list[Any]:
    if value in (None, ""):
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _parse_time(value: Any) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
