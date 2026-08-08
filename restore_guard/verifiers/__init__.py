"""Verifiers: the checks that turn a restore into proof."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import ConfigError, JobConfig
from ..sources import Snapshot
from ..util import Logger


@dataclass
class VerifyContext:
    """Everything a check needs to know about the restore it is inspecting."""

    restore_dir: Path
    snapshot: Snapshot
    job: JobConfig
    timeout: float
    log: Logger

    def resolve(self, relative: str) -> Path:
        """Resolve a config path against the restore dir, refusing to escape it."""
        candidate = (self.restore_dir / relative).resolve()
        root = self.restore_dir.resolve()
        if not str(candidate).startswith(str(root)):
            raise VerifyError(f"path {relative!r} points outside the restore directory")
        return candidate


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
