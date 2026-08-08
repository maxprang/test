"""Plain directories and tar archives on disk.

Covers rsync/rsnapshot-style backups, `pg_dump | gzip` into a folder, and the
countless homelab cron jobs that just write `backup-2026-08-08.tar.gz` somewhere.
Also the source used by the test suite, since it needs no external binary.
"""

from __future__ import annotations

import fnmatch
import shutil
import tarfile
import time
from pathlib import Path
from typing import Any

from ..config import ConfigError
from ..util import CommandError, require_binary, run
from . import RestoreOutcome, Snapshot, Source, SourceError, register

_TAR_SUFFIXES = (".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz", ".tar.xz", ".txz", ".tar.zst")


@register
class LocalSource(Source):
    type = "local"

    def validate(self) -> None:
        super().validate()
        self._required("repository")
        mode = self.spec.get("mode", "auto")
        if mode not in ("auto", "copy", "tar"):
            raise ConfigError(
                f"job {self.job.name!r}: source.mode must be auto, copy or tar (got {mode!r})"
            )

    @property
    def repository(self) -> Path:
        return Path(str(self.spec["repository"])).expanduser()

    @property
    def pattern(self) -> str:
        return str(self.spec.get("pattern", "*"))

    def preflight(self) -> None:
        if not self.repository.is_dir():
            raise SourceError(f"backup directory {self.repository} does not exist")

    def list_snapshots(self) -> list[Snapshot]:
        entries = []
        for entry in sorted(self.repository.iterdir()):
            if entry.name.startswith("."):
                continue
            if not fnmatch.fnmatch(entry.name, self.pattern):
                continue
            if entry.is_dir() or _is_tar(entry):
                entries.append(
                    Snapshot(
                        id=entry.name,
                        time=entry.stat().st_mtime,
                        label=entry.name,
                        raw={"path": str(entry), "kind": "dir" if entry.is_dir() else "tar"},
                    )
                )
        return entries

    def restore(self, snapshot: Snapshot, dest: Path, timeout: float) -> RestoreOutcome:
        source_path = self.repository / snapshot.id
        if not source_path.exists():
            raise SourceError(f"snapshot {snapshot.id!r} vanished from {self.repository}")

        started = time.monotonic()
        mode = self.spec.get("mode", "auto")
        if mode == "auto":
            mode = "tar" if _is_tar(source_path) else "copy"

        if mode == "tar":
            log = self._extract_tar(source_path, dest, timeout)
        else:
            log = self._copy_tree(source_path, dest)

        return RestoreOutcome(
            snapshot=snapshot, dest=dest, duration=time.monotonic() - started, log=log
        )

    # -- restore modes -------------------------------------------------

    def _copy_tree(self, source_path: Path, dest: Path) -> str:
        if not source_path.is_dir():
            shutil.copy2(source_path, dest / source_path.name)
            return f"copied file {source_path.name}"
        # dirs_exist_ok because the runner pre-creates the (empty) destination.
        shutil.copytree(source_path, dest, symlinks=True, dirs_exist_ok=True)
        return f"copied tree {source_path}"

    def _extract_tar(self, source_path: Path, dest: Path, timeout: float) -> str:
        # Prefer the system tar: it handles zstd and sparse files that Python's
        # tarfile does not, and it streams instead of buffering.
        try:
            tar_binary = require_binary(str(self.spec.get("tar_binary", "tar")))
        except CommandError:
            tar_binary = ""

        if tar_binary:
            result = run(
                [tar_binary, "-xf", str(source_path), "-C", str(dest)], timeout=timeout
            )
            if result.ok:
                return f"tar -xf {source_path.name}"
            if not _looks_like_missing_codec(result.tail()):
                raise SourceError(f"tar extraction of {source_path.name} failed: {result.tail()}")

        try:
            with tarfile.open(source_path) as archive:
                _safe_extract(archive, dest)
        except (tarfile.TarError, OSError) as exc:
            raise SourceError(f"cannot extract {source_path.name}: {exc}") from exc
        return f"python tarfile extract of {source_path.name}"


def _is_tar(path: Path) -> bool:
    name = path.name.lower()
    return path.is_file() and any(name.endswith(suffix) for suffix in _TAR_SUFFIXES)


def _looks_like_missing_codec(message: str) -> bool:
    lowered = message.lower()
    return "zstd" in lowered or "unrecognized" in lowered or "cannot exec" in lowered


def _safe_extract(archive: tarfile.TarFile, dest: Path) -> None:
    """Extract while refusing paths that escape the destination directory.

    Backup archives are trusted-ish, but a verifier that unpacks `../../etc/passwd`
    onto the host because of a corrupted archive would be a bad way to find out.
    """
    root = dest.resolve()
    for member in archive.getmembers():
        target = (root / member.name).resolve()
        if not str(target).startswith(str(root)):
            raise SourceError(f"archive member escapes restore directory: {member.name}")
    extract_kwargs: dict[str, Any] = {}
    if hasattr(tarfile, "data_filter"):  # Python >= 3.12 warns without this
        extract_kwargs["filter"] = "data"
    archive.extractall(root, **extract_kwargs)
