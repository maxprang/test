import pytest

from restore_guard.config import ConfigError, build_config, expand_env
from restore_guard.util import parse_duration, parse_size


def minimal_job(**overrides):
    job = {
        "name": "demo",
        "source": {"type": "local", "repository": "/tmp/x"},
        "verify": [{"type": "files", "min_files": 1}],
    }
    job.update(overrides)
    return job


def test_durations_and_sizes():
    assert parse_duration("7d") == 604800
    assert parse_duration("1h30m") == 5400
    assert parse_duration(90) == 90
    assert parse_size("10MB") == 10_000_000
    assert parse_size("512KiB") == 524288
    with pytest.raises(ValueError):
        parse_duration("soon")


def test_job_defaults_are_inherited():
    config = build_config(
        {
            "version": 1,
            "defaults": {"workdir": "/var/tmp/rg", "max_age": "2d", "timeout": "10m"},
            "jobs": [minimal_job(), minimal_job(name="other", max_age="12h")],
        }
    )
    assert config.jobs[0].max_age == 172800
    assert config.jobs[0].timeout == 600
    assert config.jobs[1].max_age == 43200


def test_verify_is_mandatory():
    with pytest.raises(ConfigError, match="proves nothing"):
        build_config({"version": 1, "jobs": [{"name": "x", "source": {"type": "local"}}]})


def test_duplicate_job_names_rejected():
    with pytest.raises(ConfigError, match="duplicate"):
        build_config({"version": 1, "jobs": [minimal_job(), minimal_job()]})


def test_unknown_notify_event_rejected():
    with pytest.raises(ConfigError, match="unknown event"):
        build_config(
            {
                "version": 1,
                "jobs": [minimal_job()],
                "notify": {"ntfy": {"url": "https://x", "on": ["explosion"]}},
            }
        )


def test_unsupported_version():
    with pytest.raises(ConfigError, match="unsupported config version"):
        build_config({"version": 99, "jobs": [minimal_job()]})


def test_env_expansion(monkeypatch):
    monkeypatch.setenv("RG_SECRET", "hunter2")
    assert expand_env({"a": ["${RG_SECRET}"]}) == {"a": ["hunter2"]}
    assert expand_env("${RG_MISSING:-default}") == "default"


def test_env_expansion_fails_loudly(monkeypatch):
    monkeypatch.delenv("RG_ABSENT", raising=False)
    with pytest.raises(ConfigError, match="not set"):
        expand_env("${RG_ABSENT}")
