"""ZFS snapshots, verified via a throwaway clone.

Proxmox and TrueNAS boxes already snapshot everything. Copying a 2 TB dataset
somewhere else to check it is absurd when `zfs clone` gives you a writable view
in milliseconds. The verifiers then run against the clone's mountpoint.

Btrfs deliberately has no source of its own: its snapshots are already
directories, so `type: local` with a `pattern` covers it without new code.
"""

from __future__ import annotations

import time
from pathlib import Path

from ..config import ConfigError
from ..util import require_binary, run
from . import RestoreOutcome, Snapshot, Source, SourceError, register

#: Every clone we create carries this in its name. Nothing without it is ever
#: destroyed — see `_assert_safe_to_destroy`.
CLONE_MARKER = "restore-guard"


@register
class ZfsSource(Source):
    type = "zfs"
    manages_destination = True

    def validate(self) -> None:
        super().validate()
        dataset = str(self._required("dataset"))
        if dataset.startswith("/") or "@" in dataset:
            raise ConfigError(
                f"job {self.job.name!r}: source.dataset must be a dataset name like "
                f"'tank/data', not a path or a snapshot (got {dataset!r})"
            )
        base = str(self.spec.get("clone_base") or f"{dataset.split('/')[0]}/{CLONE_MARKER}")
        if CLONE_MARKER not in base:
            raise ConfigError(
                f"job {self.job.name!r}: source.clone_base must contain {CLONE_MARKER!r} "
                f"(got {base!r}) — the safety check refuses to destroy anything else"
            )
        self._clone_base = base
        self._clone: str | None = None

    @property
    def binary(self) -> str:
        return str(self.spec.get("binary", "zfs"))

    @property
    def dataset(self) -> str:
        return str(self.spec["dataset"])

    # -- Source API ----------------------------------------------------

    def preflight(self) -> None:
        require_binary(self.binary)
        result = run([self.binary, "list", "-H", "-o", "name", self.dataset], timeout=60)
        if not result.ok:
            raise SourceError(f"zfs dataset {self.dataset!r} not readable: {result.tail()}")

    def list_snapshots(self) -> list[Snapshot]:
        result = run(
            [
                self.binary, "list", "-H", "-p",
                "-t", "snapshot",
                "-o", "name,creation",
                "-s", "creation",
                "-d", "1",
                self.dataset,
            ],
            timeout=120,
        )
        if not result.ok:
            raise SourceError(f"zfs list of {self.dataset!r} failed: {result.tail()}")

        snapshots = []
        for line in result.stdout.splitlines():
            if not line.strip():
                continue
            name, _, creation = line.partition("\t")
            short = name.split("@", 1)[1] if "@" in name else name
            snapshots.append(
                Snapshot(
                    id=name,
                    time=float(creation) if creation.strip().isdigit() else None,
                    label=short,
                    raw={"dataset": self.dataset},
                )
            )
        return snapshots

    def restore(self, snapshot: Snapshot, dest: Path, timeout: float) -> RestoreOutcome:
        started = time.monotonic()
        clone = f"{self._clone_base}/{self.job.name}-{int(time.time())}"

        # The runner already created dest; zfs wants to mount onto it itself.
        result = run(
            [
                self.binary, "clone",
                "-o", f"mountpoint={dest}",
                "-o", "readonly=on",
                snapshot.id,
                clone,
            ],
            timeout=timeout,
        )
        if not result.ok:
            raise SourceError(f"zfs clone of {snapshot.label} failed: {result.tail()}")
        self._clone = clone

        if not any(dest.iterdir()):
            raise SourceError(
                f"clone {clone} mounted at {dest} but the directory is empty — "
                "is the dataset mounted elsewhere (canmount=noauto)?"
            )

        return RestoreOutcome(
            snapshot=snapshot,
            dest=dest,
            duration=time.monotonic() - started,
            log=f"cloned {snapshot.id} -> {clone}",
        )

    def cleanup(self, restore_dir: Path, succeeded: bool) -> None:
        if not self._clone:
            return
        clone = self._clone
        try:
            _assert_safe_to_destroy(self.binary, clone)
        except SourceError as exc:
            # Refuse rather than guess. A leaked clone costs disk; a wrong
            # `zfs destroy` costs the dataset.
            self.log.fail(f"refusing to destroy {clone}: {exc}")
            return

        result = run([self.binary, "destroy", clone], timeout=300)
        if result.ok:
            self._clone = None
            self.log.debug(f"destroyed clone {clone}")
        else:
            self.log.fail(f"could not destroy clone {clone}: {result.tail()}")


def _assert_safe_to_destroy(binary: str, dataset: str) -> None:
    """Three independent conditions, all of which must hold.

    Belt and braces on purpose: this is the only destructive command in the
    entire program, and it runs unattended at 03:30.
    """
    if CLONE_MARKER not in dataset:
        raise SourceError(f"name does not contain {CLONE_MARKER!r}")

    result = run([binary, "get", "-H", "-p", "-o", "value", "origin", dataset], timeout=60)
    if not result.ok:
        raise SourceError(f"cannot read origin property ({result.tail()})")
    origin = result.stdout.strip()
    if not origin or origin == "-":
        raise SourceError("dataset is not a clone (no origin snapshot)")

    children = run(
        [binary, "list", "-H", "-o", "name", "-r", "-t", "filesystem,volume", dataset],
        timeout=60,
    )
    if children.ok:
        names = [line.strip() for line in children.stdout.splitlines() if line.strip()]
        if len(names) > 1:
            raise SourceError(f"dataset has {len(names) - 1} descendant dataset(s)")
