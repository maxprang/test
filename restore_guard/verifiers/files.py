"""Structural checks on the restored tree.

Cheap, dependency-free, and catches the most common real failure by far: the
backup ran every night and wrote almost nothing, because a path moved or an
exclude rule got too greedy.
"""

from __future__ import annotations

import time
from typing import Any

from ..config import ConfigError
from ..util import dir_stats, human_bytes, human_duration, parse_duration, parse_size
from . import VerifyContext, VerifyResult, Verifier, register


@register
class FilesVerifier(Verifier):
    type = "files"

    def validate(self) -> None:
        if self.spec.get("min_bytes") is not None:
            self._parsed_min_bytes = _size(self.spec["min_bytes"], "verify.files.min_bytes")
        else:
            self._parsed_min_bytes = None
        if self.spec.get("newer_than") is not None:
            self._parsed_newer_than = _duration(
                self.spec["newer_than"], "verify.files.newer_than"
            )
        else:
            self._parsed_newer_than = None
        if not any(
            key in self.spec
            for key in ("min_files", "min_bytes", "must_exist", "must_not_exist", "newer_than")
        ):
            raise ConfigError(
                f"job {self.job.name!r}: verify.files needs at least one condition "
                "(min_files, min_bytes, must_exist, must_not_exist, newer_than)"
            )

    def run(self, ctx: VerifyContext) -> VerifyResult:
        started = time.monotonic()
        stats = dir_stats(ctx.restore_dir)
        problems: list[str] = []

        min_files = self.spec.get("min_files")
        if min_files is not None and stats.files < int(min_files):
            problems.append(f"only {stats.files} files restored, expected >= {min_files}")

        if self._parsed_min_bytes is not None and stats.bytes < self._parsed_min_bytes:
            problems.append(
                f"only {human_bytes(stats.bytes)} restored, "
                f"expected >= {human_bytes(self._parsed_min_bytes)}"
            )

        for pattern in _as_list(self.spec.get("must_exist")):
            matches = list(ctx.restore_dir.glob(str(pattern)))
            if not matches:
                problems.append(f"missing from backup: {pattern}")

        for pattern in _as_list(self.spec.get("must_not_exist")):
            matches = list(ctx.restore_dir.glob(str(pattern)))
            if matches:
                problems.append(
                    f"unexpectedly present: {pattern} ({len(matches)} match(es))"
                )

        if self._parsed_newer_than is not None:
            if stats.newest_mtime is None:
                problems.append("no files at all, cannot check freshness")
            else:
                age = max(0.0, time.time() - stats.newest_mtime)
                if age > self._parsed_newer_than:
                    problems.append(
                        f"newest file is {human_duration(age)} old, "
                        f"expected younger than {human_duration(self._parsed_newer_than)}"
                    )

        details: dict[str, Any] = {
            "files": stats.files,
            "dirs": stats.dirs,
            "bytes": stats.bytes,
            "largest": stats.largest[0] if stats.largest else None,
            "newest_mtime": stats.newest_mtime,
            "problems": problems,
        }
        duration = time.monotonic() - started
        summary = f"{stats.files} files, {human_bytes(stats.bytes)}"

        if problems:
            result = self.failed(f"{summary}; " + "; ".join(problems), **details)
        else:
            result = self.ok(summary, **details)
        result.duration = duration
        return result


def _as_list(value: Any) -> list[Any]:
    if value in (None, ""):
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _size(value: Any, where: str) -> int:
    try:
        return parse_size(value)
    except ValueError as exc:
        raise ConfigError(f"{where}: {exc}") from exc


def _duration(value: Any, where: str) -> int:
    try:
        return parse_duration(value)
    except ValueError as exc:
        raise ConfigError(f"{where}: {exc}") from exc
