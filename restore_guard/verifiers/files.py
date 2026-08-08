"""Structural checks on the restored tree.

Cheap, dependency-free, and catches the most common real failure by far: the
backup ran every night and wrote almost nothing, because a path moved or an
exclude rule got too greedy.
"""

from __future__ import annotations

import time
from typing import Any

from ..config import ConfigError, duration_or_raise, size_or_raise
from ..util import as_list, human_bytes, human_duration
from . import VerifyContext, VerifyResult, Verifier, register


@register
class FilesVerifier(Verifier):
    type = "files"

    #: Conditions this verifier understands; at least one must be configured,
    #: otherwise the check would pass unconditionally.
    CONDITIONS = ("min_files", "min_bytes", "must_exist", "must_not_exist", "newer_than")

    def validate(self) -> None:
        self.min_bytes = (
            size_or_raise(self.spec["min_bytes"], "verify.files.min_bytes")
            if self.spec.get("min_bytes") is not None
            else None
        )
        self.newer_than = (
            duration_or_raise(self.spec["newer_than"], "verify.files.newer_than")
            if self.spec.get("newer_than") is not None
            else None
        )
        if not any(
key in self.spec for key in self.CONDITIONS):
            raise ConfigError(
                f"job {self.job.name!r}: verify.files needs at least one condition "
                f"({', '.join(self.CONDITIONS)})"
            )

    def run(self, ctx: VerifyContext) -> VerifyResult:
        started = time.monotonic()
        stats = ctx.tree_stats()
        problems: list[str] = []

        min_files = self.spec.get("min_files")
        if min_files is not None and stats.files < int(min_files):
            problems.append(f"only {stats.files} files restored, expected >= {min_files}")

        if self.min_bytes is not None and stats.bytes < self.min_bytes:
            problems.append(
                f"only {human_bytes(stats.bytes)} restored, "
                f"expected >= {human_bytes(self.min_bytes)}"
            )

        for pattern in as_list(self.spec.get("must_exist")):
            matches = list(ctx.restore_dir.glob(str(pattern)))
            if not matches:
                problems.append(f"missing from backup: {pattern}")

        for pattern in as_list(self.spec.get("must_not_exist")):
            matches = list(ctx.restore_dir.glob(str(pattern)))
            if matches:
                problems.append(
                    f"unexpectedly present: {pattern} ({len(matches)} match(es))"
                )

        if self.newer_than is not None:
            if stats.newest_mtime is None:
                problems.append("no files at all, cannot check freshness")
            else:
                age = max(0.0, time.time() - stats.newest_mtime)
                if age > self.newer_than:
                    problems.append(
                        f"newest file is {human_duration(age)} old, "
                        f"expected younger than {human_duration(self.newer_than)}"
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

