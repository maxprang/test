"""Small shared helpers: durations, sizes, subprocess execution, filesystem stats."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

_DURATION_RE = re.compile(r"(\d+)\s*([smhdw])")
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}

_SIZE_RE = re.compile(r"^\s*([\d.]+)\s*([kmgt]?i?b?)\s*$", re.IGNORECASE)
_SIZE_UNITS = {
    "": 1,
    "b": 1,
    "k": 1000,
    "kb": 1000,
    "ki": 1024,
    "kib": 1024,
    "m": 1000**2,
    "mb": 1000**2,
    "mi": 1024**2,
    "mib": 1024**2,
    "g": 1000**3,
    "gb": 1000**3,
    "gi": 1024**3,
    "gib": 1024**3,
    "t": 1000**4,
    "tb": 1000**4,
    "ti": 1024**4,
    "tib": 1024**4,
}


class UtilError(ValueError):
    """Raised when a helper cannot make sense of its input."""


def parse_duration(value) -> int:
    """Turn ``"7d"``, ``"36h"``, ``"1w 12h"`` or a plain number of seconds into seconds."""
    if isinstance(value, bool):
        raise UtilError(f"not a duration: {value!r}")
    if isinstance(value, (int, float)):
        return int(value)
    if not isinstance(value, str):
        raise UtilError(f"not a duration: {value!r}")

    text = value.strip().lower()
    if not text:
        raise UtilError("empty duration")
    if text.isdigit():
        return int(text)

    matches = list(_DURATION_RE.finditer(text))
    if not matches or "".join(m.group(0) for m in matches).replace(" ", "") != text.replace(" ", ""):
        raise UtilError(f"cannot parse duration {value!r} (try '30m', '12h', '7d')")
    return sum(int(m.group(1)) * _UNITS[m.group(2)] for m in matches)


def parse_size(value) -> int:
    """Turn ``"10MB"``, ``"512KiB"`` or a plain number of bytes into bytes."""
    if isinstance(value, bool):
        raise UtilError(f"not a size: {value!r}")
    if isinstance(value, (int, float)):
        return int(value)
    if not isinstance(value, str):
        raise UtilError(f"not a size: {value!r}")

    match = _SIZE_RE.match(value)
    if not match:
        raise UtilError(f"cannot parse size {value!r} (try '10MB', '512KiB', '2GiB')")
    number, unit = match.groups()
    unit = unit.lower()
    if unit not in _SIZE_UNITS:
        raise UtilError(f"unknown size unit in {value!r}")
    return int(float(number) * _SIZE_UNITS[unit])


def human_duration(seconds: float | None) -> str:
    if seconds is None:
        return "never"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    if seconds < 86400:
        return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"
    return f"{seconds // 86400}d{(seconds % 86400) // 3600:02d}h"


def human_bytes(count: float | None) -> str:
    if count is None:
        return "-"
    step = 1024.0
    value = float(count)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(value) < step or unit == "TiB":
            return f"{value:.0f}{unit}" if unit == "B" else f"{value:.1f}{unit}"
        value /= step
    return f"{value:.1f}TiB"


@dataclass
class CommandResult:
    """Outcome of a single subprocess invocation."""

    argv: list[str]
    returncode: int
    stdout: str
    stderr: str
    duration: float
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    def tail(self, lines: int = 8) -> str:
        """Last few lines of stderr (falling back to stdout) for error messages."""
        text = (self.stderr or self.stdout or "").strip()
        if not text:
            return ""
        return "\n".join(text.splitlines()[-lines:])


class CommandError(RuntimeError):
    def __init__(self, result: CommandResult):
        self.result = result
        what = " ".join(result.argv[:4])
        if result.timed_out:
            super().__init__(f"command timed out after {result.duration:.0f}s: {what}")
        else:
            super().__init__(f"command failed (rc={result.returncode}): {what}\n{result.tail()}")


def run(
    argv: list[str],
    *,
    timeout: float | None = None,
    env: dict[str, str] | None = None,
    cwd: str | Path | None = None,
    stdin_text: str | None = None,
    check: bool = False,
) -> CommandResult:
    """Run a command, capturing output. Never raises on non-zero unless ``check``."""
    full_env = dict(os.environ)
    if env:
        full_env.update({k: str(v) for k, v in env.items()})

    started = time.monotonic()
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=full_env,
            cwd=str(cwd) if cwd else None,
            input=stdin_text,
        )
        result = CommandResult(
            argv=list(argv),
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
            duration=time.monotonic() - started,
        )
    except subprocess.TimeoutExpired as exc:
        result = CommandResult(
            argv=list(argv),
            returncode=-1,
            stdout=_decode(exc.stdout),
            stderr=_decode(exc.stderr),
            duration=time.monotonic() - started,
            timed_out=True,
        )
    except FileNotFoundError as exc:
        raise CommandError(
            CommandResult(list(argv), 127, "", str(exc), time.monotonic() - started)
        ) from exc

    if check and not result.ok:
        raise CommandError(result)
    return result


def _decode(raw) -> str:
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", "replace")
    return str(raw)


def require_binary(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise CommandError(CommandResult([name], 127, "", f"{name} not found in PATH", 0.0))
    return path


@dataclass
class DirStats:
    files: int = 0
    dirs: int = 0
    bytes: int = 0
    newest_mtime: float | None = None
    largest: tuple[str, int] | None = None


def dir_stats(path: Path) -> DirStats:
    """Walk a directory once and collect the numbers every verifier wants."""
    stats = DirStats()
    if not path.exists():
        return stats
    for root, dirnames, filenames in os.walk(path):
        stats.dirs += len(dirnames)
        for name in filenames:
            file_path = Path(root) / name
            try:
                info = file_path.lstat()
            except OSError:
                continue
            stats.files += 1
            if not os.path.islink(file_path):
                stats.bytes += info.st_size
                if stats.largest is None or info.st_size > stats.largest[1]:
                    stats.largest = (str(file_path.relative_to(path)), info.st_size)
            if stats.newest_mtime is None or info.st_mtime > stats.newest_mtime:
                stats.newest_mtime = info.st_mtime
    return stats


def rmtree_quiet(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def expand_placeholders(value, mapping: dict[str, str]):
    """Recursively replace ``{restore_dir}``-style placeholders in strings/lists/dicts."""
    if isinstance(value, str):
        out = value
        for key, replacement in mapping.items():
            out = out.replace("{" + key + "}", str(replacement))
        return out
    if isinstance(value, list):
        return [expand_placeholders(item, mapping) for item in value]
    if isinstance(value, dict):
        return {k: expand_placeholders(v, mapping) for k, v in value.items()}
    return value


@dataclass
class Logger:
    """Deliberately tiny logger: stdout, optional verbosity, remembers lines for reports.

    Locked because parallel jobs log from several threads; without it lines
    interleave mid-sentence and the output becomes unreadable exactly when
    something has gone wrong.
    """

    verbose: bool = False
    quiet: bool = False
    lines: list[str] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def _emit(self, prefix: str, message: str) -> None:
        line = f"{prefix} {message}" if prefix else message
        with self._lock:
            self.lines.append(line)
            if not self.quiet:
                print(line, flush=True)

    def info(self, message: str) -> None:
        self._emit("  ", message)

    def step(self, message: str) -> None:
        self._emit("::", message)

    def ok(self, message: str) -> None:
        self._emit(" +", message)

    def fail(self, message: str) -> None:
        self._emit(" !", message)

    def debug(self, message: str) -> None:
        if self.verbose:
            self._emit("  .", message)
