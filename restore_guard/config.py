"""Loading and validation of the YAML configuration."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .util import parse_duration

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

DEFAULT_CONFIG_PATHS = (
    Path("/etc/restore-guard/config.yml"),
    Path("./restore-guard.yml"),
    Path("./config.yml"),
)


class ConfigError(Exception):
    """Configuration is malformed. Always carries the offending path in the message."""


@dataclass
class NotifyTarget:
    kind: str
    spec: dict[str, Any]
    events: list[str] = field(default_factory=lambda: ["failure", "recovery"])


@dataclass
class JobConfig:
    name: str
    source: dict[str, Any]
    verify: list[dict[str, Any]]
    enabled: bool = True
    max_age: int = 7 * 86400
    timeout: int = 1800
    keep_on_failure: bool | None = None
    tags: list[str] = field(default_factory=list)
    description: str = ""


@dataclass
class Config:
    workdir: Path
    jobs: list[JobConfig]
    notify: list[NotifyTarget] = field(default_factory=list)
    keep_on_failure: bool = True
    history_limit: int = 200
    parallel: int = 1
    metrics_file: Path | None = None
    report_file: Path | None = None
    source_path: Path | None = None

    @property
    def state_path(self) -> Path:
        return self.workdir / "state.db"

    @property
    def restores_path(self) -> Path:
        return self.workdir / "restores"

    def job(self, name: str) -> JobConfig:
        for job in self.jobs:
            if job.name == name:
                return job
        known = ", ".join(j.name for j in self.jobs) or "<none>"
        raise ConfigError(f"unknown job {name!r}; configured jobs: {known}")


def expand_env(value: Any) -> Any:
    """Expand ``${VAR}`` and ``${VAR:-fallback}`` inside every string of the tree.

    Keeps secrets (restic/borg passphrases, ntfy tokens) out of the config file.
    An unset variable without a fallback is an error rather than an empty string,
    because silently restoring with an empty passphrase fails in confusing ways.
    """
    if isinstance(value, str):
        def replace(match: re.Match[str]) -> str:
            name, fallback = match.group(1), match.group(2)
            if name in os.environ:
                return os.environ[name]
            if fallback is not None:
                return fallback
            raise ConfigError(
                f"environment variable ${{{name}}} referenced in config but not set"
            )

        return _ENV_RE.sub(replace, value)
    if isinstance(value, list):
        return [expand_env(item) for item in value]
    if isinstance(value, dict):
        return {key: expand_env(item) for key, item in value.items()}
    return value


def find_config(explicit: str | Path | None = None) -> Path:
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            raise ConfigError(f"config file not found: {path}")
        return path
    for candidate in DEFAULT_CONFIG_PATHS:
        if candidate.is_file():
            return candidate
    searched = ", ".join(str(p) for p in DEFAULT_CONFIG_PATHS)
    raise ConfigError(f"no config file found (looked at: {searched}); pass --config")


def load_config(path: str | Path | None = None) -> Config:
    config_path = find_config(path)
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{config_path}: invalid YAML: {exc}") from exc
    if raw is None:
        raise ConfigError(f"{config_path}: file is empty")
    if not isinstance(raw, dict):
        raise ConfigError(f"{config_path}: top level must be a mapping")

    raw = expand_env(raw)
    config = build_config(raw)
    config.source_path = config_path
    return config


def build_config(raw: dict[str, Any]) -> Config:
    version = raw.get("version", 1)
    if version != 1:
        raise ConfigError(f"unsupported config version {version!r} (this build understands 1)")

    defaults = raw.get("defaults") or {}
    _require_mapping(defaults, "defaults")

    workdir = Path(defaults.get("workdir", "/var/lib/restore-guard")).expanduser()
    keep_on_failure = bool(defaults.get("keep_on_failure", True))
    history_limit = int(defaults.get("history_limit", 200))
    default_timeout = _duration(defaults.get("timeout", "30m"), "defaults.timeout")
    default_max_age = _duration(defaults.get("max_age", "7d"), "defaults.max_age")

    metrics_file = defaults.get("metrics_file")
    metrics_path = Path(metrics_file).expanduser() if metrics_file else None
    report_file = defaults.get("report_file")
    report_path = Path(report_file).expanduser() if report_file else None

    parallel = int(defaults.get("parallel", 1))
    if parallel < 1:
        raise ConfigError(f"defaults.parallel must be >= 1, got {parallel}")

    raw_jobs = raw.get("jobs")
    if not raw_jobs:
        raise ConfigError("no jobs configured — nothing to verify")
    if not isinstance(raw_jobs, list):
        raise ConfigError("jobs must be a list")

    jobs: list[JobConfig] = []
    seen: set[str] = set()
    for index, raw_job in enumerate(raw_jobs):
        job = _build_job(raw_job, index, default_timeout, default_max_age)
        if job.name in seen:
            raise ConfigError(f"duplicate job name {job.name!r}")
        seen.add(job.name)
        jobs.append(job)

    return Config(
        workdir=workdir,
        jobs=jobs,
        notify=_build_notify(raw.get("notify")),
        keep_on_failure=keep_on_failure,
        history_limit=history_limit,
        parallel=parallel,
        metrics_file=metrics_path,
        report_file=report_path,
    )


def _build_job(raw_job: Any, index: int, default_timeout: int, default_max_age: int) -> JobConfig:
    where = f"jobs[{index}]"
    _require_mapping(raw_job, where)

    name = raw_job.get("name")
    if not name or not isinstance(name, str):
        raise ConfigError(f"{where}: 'name' is required and must be a string")
    where = f"job {name!r}"

    source = raw_job.get("source")
    _require_mapping(source, f"{where}.source")
    if not source.get("type"):
        raise ConfigError(f"{where}.source: 'type' is required (restic, borg, local)")

    verify = raw_job.get("verify")
    if not verify:
        raise ConfigError(
            f"{where}: 'verify' is required — a restore without a check proves nothing"
        )
    if not isinstance(verify, list):
        raise ConfigError(f"{where}.verify must be a list of checks")
    for position, check in enumerate(verify):
        _require_mapping(check, f"{where}.verify[{position}]")
        if not check.get("type"):
            raise ConfigError(f"{where}.verify[{position}]: 'type' is required")

    keep = raw_job.get("keep_on_failure")

    return JobConfig(
        name=name,
        source=source,
        verify=verify,
        enabled=bool(raw_job.get("enabled", True)),
        max_age=_duration(raw_job.get("max_age", default_max_age), f"{where}.max_age"),
        timeout=_duration(raw_job.get("timeout", default_timeout), f"{where}.timeout"),
        keep_on_failure=None if keep is None else bool(keep),
        tags=list(raw_job.get("tags") or []),
        description=str(raw_job.get("description") or ""),
    )


def _build_notify(raw_notify: Any) -> list[NotifyTarget]:
    if not raw_notify:
        return []
    _require_mapping(raw_notify, "notify")

    valid_events = {"failure", "recovery", "success", "stale"}
    targets: list[NotifyTarget] = []
    for kind, spec in raw_notify.items():
        _require_mapping(spec, f"notify.{kind}")
        events = spec.get("on") or ["failure", "recovery"]
        if isinstance(events, str):
            events = [events]
        unknown = set(events) - valid_events
        if unknown:
            raise ConfigError(
                f"notify.{kind}.on: unknown event(s) {sorted(unknown)}; "
                f"valid: {sorted(valid_events)}"
            )
        targets.append(NotifyTarget(kind=kind, spec=spec, events=list(events)))
    return targets


def _require_mapping(value: Any, where: str) -> None:
    if not isinstance(value, dict):
        raise ConfigError(f"{where} must be a mapping, got {type(value).__name__}")


def _duration(value: Any, where: str) -> int:
    try:
        return parse_duration(value)
    except ValueError as exc:
        raise ConfigError(f"{where}: {exc}") from exc
