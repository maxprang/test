"""Verifiers: the checks that turn a restore into proof."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import ConfigError, JobConfig
from ..sources import Snapshot
from ..util import DirStats, Logger, PathEscape, dir_stats, ensure_within


@dataclass
class VerifyContext:
    """Everything a check needs to know about the restore it is inspecting."""

    restore_dir: Path
    snapshot: Snapshot
    job: JobConfig
    timeout: float
    log: Logger
    #: Filled in by the runner, which already walked the tree to log the restore.
    stats: DirStats | None = None

    def resolve(self, relative: str) -> Path:
        """Resolve a config path against the restore dir, refusing to escape it."""
        try:
            return ensure_within(self.restore_dir, relative)
        except PathEscape as exc:
            raise VerifyError(
                f"path {relative!r} points outside the restore directory"
            ) from exc

    def tree_stats(self) -> DirStats:
        """File count and size of the restore, walked at most once per job.

        Walking a restored tree costs one lstat per file; on the multi-TB
        restores this tool is built for, doing it once per verifier instead of
        once per job is the difference between seconds and minutes.
        """
        if self.stats is None:
            self.stats = dir_stats(self.restore_dir)
        return self.stats


@dataclass
class VerifyResult:
    ok: bool
    summary: str
    details: dict[str, Any] = field(default_factory=dict)
    type: str = ""
    duration: float = 0.0


class VerifyError(RuntimeError):
    """The check could not be carried out (as opposed to: the check said no)."""


class Verifier(ABC):
    type: str = ""

    def __init__(self, spec: dict[str, Any], job: JobConfig):
        self.spec = spec
        self.job = job
        self.name = str(spec.get("name") or self.type)
        self.validate()

    def validate(self) -> None:
        """Raise ConfigError for anything detectable before the restore runs."""

    def _required(self, key: str) -> Any:
        value = self.spec.get(key)
        if value in (None, "", []):
            raise ConfigError(
                f"job {self.job.name!r}: verify.{self.type}.{key} is required"
            )
        return value

    @abstractmethod
    def run(self, ctx: VerifyContext) -> VerifyResult:
        ...

    def ok(self, summary: str, **details: Any) -> VerifyResult:
        return VerifyResult(True, summary, details, type=self.type)

    def failed(self, summary: str, **details: Any) -> VerifyResult:
        return VerifyResult(False, summary, details, type=self.type)


_REGISTRY: dict[str, type[Verifier]] = {}


def register(cls: type[Verifier]) -> type[Verifier]:
    _REGISTRY[cls.type] = cls
    return cls


def _load_builtins() -> None:
    from . import (  # noqa: F401
        command,
        files,
        http_service,
        mysql,
        postgres,
        sqlite_db,
    )


def build_verifiers(job: JobConfig) -> list[Verifier]:
    _load_builtins()
    verifiers = []
    for spec in job.verify:
        kind = str(spec.get("type", "")).lower()
        if kind not in _REGISTRY:
            known = ", ".join(sorted(_REGISTRY))
            raise ConfigError(
                f"job {job.name!r}: unknown verify type {kind!r} (known: {known})"
            )
        verifiers.append(_REGISTRY[kind](spec, job))
    return verifiers


def known_verifiers() -> list[str]:
    _load_builtins()
    return sorted(_REGISTRY)
