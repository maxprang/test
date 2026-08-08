"""Restic repositories."""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import ConfigError
from ..util import CommandError, require_binary, run
from . import RestoreOutcome, Snapshot, Source, SourceError, register


@register
class ResticSource(Source):
    type = "restic"

    def validate(self) -> None:
        super().validate()
        self._required("repository")
        spec = self.spec
        if not any(
            spec.get(key) for key in ("password", "password_file", "password_command")
        ) and "RESTIC_PASSWORD" not in (spec.get("env") or {}):
            raise ConfigError(
                f"job {self.job.name!r}: restic needs one of password, password_file, "
                "password_command (use ${ENV_VAR} to keep secrets out of the file)"
            )

    # -- helpers -------------------------------------------------------

    @property
    def binary(self) -> str:
        return str(self.spec.get("binary", "restic"))

    def _env(self) -> dict[str, str]:
        env: dict[str, str] = {
            "RESTIC_REPOSITORY": str(self.spec["repository"]),
            # restic's progress output is noise in a log file.
            "RESTIC_PROGRESS_FPS": "0",
        }
        if self.spec.get("password"):
            env["RESTIC_PASSWORD"] = str(self.spec["password"])
        if self.spec.get("password_file"):
            env["RESTIC_PASSWORD_FILE"] = str(self.spec["password_file"])
        if self.spec.get("password_command"):
            env["RESTIC_PASSWORD_COMMAND"] = str(self.spec["password_command"])
        env.update({k: str(v) for k, v in (self.spec.get("env") or {}).items()})
        return env

    def _base_argv(self) -> list[str]:
        argv = [self.binary]
        if self.spec.get("cacert"):
            argv += ["--cacert", str(self.spec["cacert"])]
        if self.spec.get("no_cache", False):
            argv.append("--no-cache")
        return argv

    def _filter_argv(self) -> list[str]:
        argv: list[str] = []
        for host in _as_list(self.spec.get("host")):
            argv += ["--host", str(host)]
        for tag in _as_list(self.spec.get("tags")):
            argv += ["--tag", str(tag)]
        return argv

    # -- Source API ----------------------------------------------------

    def preflight(self) -> None:
        require_binary(self.binary)
        result = run(
            self._base_argv() + ["cat", "config"], env=self._env(), timeout=120
        )
        if not result.ok:
            raise SourceError(
                f"restic repository {self.spec['repository']!r} not readable: {result.tail()}"
            )

    def list_snapshots(self) -> list[Snapshot]:
        argv = self._base_argv() + ["snapshots", "--json"] + self._filter_argv()
        result = run(argv, env=self._env(), timeout=300)
        if not result.ok:
            raise SourceError(f"restic snapshots failed: {result.tail()}")
        try:
            payload = json.loads(result.stdout or "[]")
        except ValueError as exc:
            raise SourceError(f"restic returned unparsable JSON: {exc}") from exc

        snapshots = []
        for entry in payload:
            snapshots.append(
                Snapshot(
                    id=str(entry.get("id", "")),
                    time=_parse_time(entry.get("time")),
                    label=str(entry.get("short_id") or entry.get("id", ""))[:12],
                    raw=entry,
                )
            )
        return snapshots

    def restore(self, snapshot: Snapshot, dest: Path, timeout: float) -> RestoreOutcome:
        argv = self._base_argv() + ["restore", snapshot.id, "--target", str(dest)]
        for path in _as_list(self.spec.get("paths")):
            argv += ["--include", str(path)]
        for pattern in _as_list(self.spec.get("exclude")):
            argv += ["--exclude", str(pattern)]
        if self.spec.get("verify", False):
            # restic >= 0.16: re-hash restored files against the repository index.
            argv.append("--verify")

        started = time.monotonic()
        try:
            result = run(argv, env=self._env(), timeout=timeout)
        except CommandError as exc:
            raise SourceError(str(exc)) from exc
        if not result.ok:
            hint = "timed out" if result.timed_out else result.tail()
            raise SourceError(f"restic restore of {snapshot.short} failed: {hint}")

        return RestoreOutcome(
            snapshot=snapshot,
            dest=dest,
            duration=time.monotonic() - started,
            log=result.stdout.strip(),
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
    text = str(value)
    # restic emits RFC3339 with nanoseconds, which fromisoformat rejects before 3.11.
    if "." in text:
        head, _, tail = text.partition(".")
        fraction = "".join(ch for ch in tail if ch.isdigit())[:6]
        suffix = tail[len(fraction) :].lstrip("0123456789")
        text = f"{head}.{fraction or '0'}{suffix}"
    text = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None
