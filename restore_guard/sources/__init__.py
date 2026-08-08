"""Backup sources: everything that knows how to list snapshots and restore one."""

from __future__ import annotations

import random
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import ConfigError, JobConfig
from ..util import Logger

SELECT_STRATEGIES = ("latest", "oldest", "random")


@dataclass
class Snapshot:
    """One restorable point in time, normalised across backup tools."""

    id: str
    time: float | None = None
    label: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def short(self) -> str:
        return self.label or self.id[:12]


@dataclass
class RestoreOutcome:
    snapshot: Snapshot
    dest: Path
    duration: float
    log: str = ""


class SourceError(RuntimeError):
    """The backup repository could not be read or restored from."""


class Source(ABC):
    """Base class for backup repositories.

    Subclasses do three things: check they can run at all (``preflight``), list
    snapshots, and restore one into a directory we hand them.
    """

    type: str = ""

    def __init__(self, spec: dict[str, Any], job: JobConfig, logger: Logger):
        self.spec = spec
        self.job = job
        self.log = logger
        self.validate()

    # -- configuration -------------------------------------------------

    def validate(self) -> None:
        """Raise ConfigError for anything we can detect without touching the repo."""
        # Either a named strategy or a literal snapshot id — both are strings.
        strategy = self.spec.get("snapshot", "latest")
        if not isinstance(strategy, str):
            raise ConfigError(
                f"job {self.job.name!r}: source.snapshot must be a string "
                f"({', '.join(SELECT_STRATEGIES)} or a snapshot id), got {strategy!r}"
            )

    def _required(self, key: str) -> Any:
        value = self.spec.get(key)
        if value in (None, "", []):
            raise ConfigError(
                f"job {self.job.name!r}: source.{key} is required for type {self.type!r}"
            )
        return value

    @property
    def selection(self) -> str:
        return str(self.spec.get("snapshot", "latest"))

    # -- repository access ---------------------------------------------

    def preflight(self) -> None:
        """Fail early and clearly if the tool or repository is unusable."""

    @abstractmethod
    def list_snapshots(self) -> list[Snapshot]:
        ...

    @abstractmethod
    def restore(self, snapshot: Snapshot, dest: Path, timeout: float) -> RestoreOutcome:
        ...

    #: True when ``restore`` hands back something other than a plain copy of
    #: files — a ZFS clone mounted at ``dest``, for example. The runner must
    #: then call ``cleanup`` instead of deleting the directory, or it would
    #: happily rm -rf its way through a live dataset.
    manages_destination: bool = False

    def cleanup(self, restore_dir: Path, succeeded: bool) -> None:
        """Release whatever ``restore`` acquired. Only called when the source
        manages the destination; plain copies are removed by the runner."""

    def select(self, snapshots: list[Snapshot]) -> Snapshot:
        """Pick the snapshot to verify.

        ``random`` is the interesting one: verifying only the newest snapshot
        proves last night worked, not that the retention chain is intact.
        """
        if not snapshots:
            raise SourceError(f"job {self.job.name!r}: repository contains no snapshots")

        ordered = sorted(snapshots, key=lambda s: (s.time or 0.0))
        strategy = self.selection
        if strategy == "latest":
            return ordered[-1]
        if strategy == "oldest":
            return ordered[0]
        if strategy == "random":
            return random.choice(ordered)

        for snapshot in ordered:
            if snapshot.id == strategy or snapshot.id.startswith(strategy) or snapshot.label == strategy:
                return snapshot
        raise SourceError(
            f"job {self.job.name!r}: no snapshot matching {strategy!r} "
            f"(have {len(ordered)}: {', '.join(s.short for s in ordered[-5:])})"
        )


_REGISTRY: dict[str, type[Source]] = {}


def register(cls: type[Source]) -> type[Source]:
    _REGISTRY[cls.type] = cls
    return cls


def _load_builtins() -> None:
    from . import borg, local, restic, zfs  # noqa: F401  (import registers the classes)


def build_source(job: JobConfig, logger: Logger) -> Source:
    _load_builtins()

    kind = str(job.source.get("type", "")).lower()
    if kind not in _REGISTRY:
        known = ", ".join(sorted(_REGISTRY))
        raise ConfigError(f"job {job.name!r}: unknown source type {kind!r} (known: {known})")
    return _REGISTRY[kind](job.source, job, logger)


def known_sources() -> list[str]:
    _load_builtins()
    return sorted(_REGISTRY)
