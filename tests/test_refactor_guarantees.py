"""Properties that only hold because the duplicated code was merged.

Each test here would have passed for one copy and failed for another before
the helpers were shared — that is exactly what makes them worth keeping.
"""

import sqlite3
import tarfile
import time

import pytest

from restore_guard import docker as docker_module
from restore_guard.config import ConfigError, build_config
from restore_guard.docker import Docker, reset_availability_cache
from restore_guard.report import collect, detail_text, html_report, render_table, status_table
from restore_guard.runner import Runner
from restore_guard.sources import build_source
from restore_guard.state import State
from restore_guard.util import CommandResult, Logger

from conftest import make_config


# -- one comparison implementation, every engine ----------------------


def test_sqlite_supports_expect_contains(tmp_path):
    """Regression: sqlite's private copy of the comparison logic lacked this."""
    from restore_guard.config import JobConfig
    from restore_guard.sources import Snapshot
    from restore_guard.verifiers import VerifyContext, build_verifiers

    restore = tmp_path / "restore"
    restore.mkdir()
    connection = sqlite3.connect(restore / "app.db")
    connection.execute("CREATE TABLE meta (schema_version TEXT)")
    connection.execute("INSERT INTO meta VALUES ('v16.2-homelab')")
    connection.commit()
    connection.close()

    def verify(expected):
        job = JobConfig(
            name="demo",
            source={"type": "local", "repository": "/tmp"},
            verify=[
                {
                    "type": "sqlite",
                    "path": "app.db",
                    "queries": [
                        {
                            "name": "schema",
                            "sql": "SELECT schema_version FROM meta",
                            "expect_contains": expected,
                        }
                    ],
                }
            ],
        )
        ctx = VerifyContext(
            restore_dir=restore,
            snapshot=Snapshot(id="x", time=time.time()),
            job=job,
            timeout=60,
            log=Logger(quiet=True),
        )
        return build_verifiers(job)[0].run(ctx)

    assert verify("v16").ok
    assert not verify("v17").ok


def test_misspelled_expectation_is_rejected_for_every_engine(tmp_path):
    """A silently-ignored typo means a check that can never fail."""
    from restore_guard.config import JobConfig
    from restore_guard.verifiers import build_verifiers

    for verify_spec in (
        {"type": "sqlite", "path": "*.db", "queries": [{"sql": "SELECT 1", "expect_mn": 1}]},
        {"type": "postgres", "dump": "*.sql", "checks": [{"sql": "SELECT 1", "expect_mn": 1}]},
        {"type": "mysql", "dump": "*.sql", "checks": [{"sql": "SELECT 1", "expect_mn": 1}]},
    ):
        job = JobConfig(name="d", source={"type": "local"}, verify=[verify_spec])
        with pytest.raises(ConfigError, match="unknown key"):
            build_verifiers(job)


# -- the restored tree is walked once per job -------------------------


def test_tree_is_walked_once_even_with_several_file_verifiers(backup_dir, tmp_path, monkeypatch):
    """The runner already walked it; verifiers must reuse that result.

    Counts real ``os.walk`` calls rather than patching a helper by name, so the
    test measures the property itself and cannot be satisfied by moving an
    import around. Before the stats were threaded through VerifyContext this
    was four walks (one runner + one per files verifier); now it is one.
    """
    import os

    walks = []
    original_walk = os.walk

    def counting_walk(path, *args, **kwargs):
        walks.append(str(path))
        return original_walk(path, *args, **kwargs)

    monkeypatch.setattr(os, "walk", counting_walk)

    config = build_config(
        make_config(
            tmp_path / "work",
            [
                {
                    "name": "walked-once",
                    "source": {"type": "local", "repository": str(backup_dir)},
                    "verify": [
                        {"type": "files", "min_files": 1},
                        {"type": "files", "min_bytes": "1KiB"},
                        {"type": "files", "must_exist": ["etc/config.yaml"]},
                    ],
                }
            ],
        )
    )
    state = State(config.state_path)
    outcomes = Runner(config, state, Logger(quiet=True)).run_all()
    state.close()

    assert outcomes[0].record.ok
    restore_walks = [w for w in walks if str(config.restores_path) in w]
    assert len(restore_walks) == 1, (
        f"restore tree walked {len(restore_walks)}x; three files verifiers "
        "should share the runner's single walk"
    )


def test_context_walks_lazily_when_the_runner_did_not(tmp_path):
    """Verifiers used outside the runner still work — they just walk once."""
    from restore_guard.config import JobConfig
    from restore_guard.sources import Snapshot
    from restore_guard.verifiers import VerifyContext

    restore = tmp_path / "restore"
    restore.mkdir()
    (restore / "a.txt").write_text("x" * 100)

    ctx = VerifyContext(
        restore_dir=restore,
        snapshot=Snapshot(id="x"),
        job=JobConfig(name="d", source={}, verify=[]),
        timeout=60,
        log=Logger(quiet=True),
    )
    first = ctx.tree_stats()
    assert first.files == 1
    assert ctx.tree_stats() is first  # cached, not recomputed


# -- docker availability is probed once -------------------------------


def test_docker_availability_is_cached(monkeypatch):
    reset_availability_cache()
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return CommandResult(list(argv), 0, "27.0.0", "", 0.01)

    monkeypatch.setattr(docker_module, "run", fake_run)
    monkeypatch.setattr(docker_module, "require_binary", lambda name: f"/usr/bin/{name}")

    assert Docker("docker").available()
    assert Docker("docker").available()  # a second verifier in the same job
    assert Docker("docker").available()

    assert len(calls) == 1, "docker info should be probed once per binary"
    reset_availability_cache()


def test_docker_availability_cache_is_per_binary(monkeypatch):
    reset_availability_cache()
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv[0])
        return CommandResult(list(argv), 0, "27.0.0", "", 0.01)

    monkeypatch.setattr(docker_module, "run", fake_run)
    monkeypatch.setattr(docker_module, "require_binary", lambda name: f"/usr/bin/{name}")

    Docker("docker").available()
    Docker("podman").available()
    assert calls == ["docker", "podman"]
    reset_availability_cache()


# -- one table renderer, one detail rule ------------------------------


def test_render_table_pads_and_rules():
    out = render_table(("A", "BB"), [("x", "yyyy"), ("longer", "z")])
    lines = out.splitlines()
    assert lines[0] == "A       BB"
    assert lines[1] == "------  ----"
    assert lines[2] == "x       yyyy"
    assert lines[3] == "longer  z"


def test_render_table_handles_no_rows():
    assert render_table(("A", "B"), []).splitlines() == ["A  B", "-  -"]


def test_terminal_and_html_explain_a_failure_the_same_way(backup_dir, tmp_path):
    """Both surfaces read the same rule; only the truncation length differs."""
    workdir = tmp_path / "work"
    jobs = [
        {
            "name": "svc",
            "source": {"type": "local", "repository": str(backup_dir)},
            "verify": [{"type": "files", "min_files": 1}],
        }
    ]
    config = build_config(make_config(workdir, jobs))
    state = State(config.state_path)
    Runner(config, state, Logger(quiet=True)).run_all()
    state.close()

    jobs[0]["verify"] = [{"type": "files", "min_bytes": "10GiB"}]
    config = build_config(make_config(workdir, jobs))
    state = State(config.state_path)
    Runner(config, state, Logger(quiet=True)).run_all()
    statuses = collect(config, state)
    state.close()

    short = detail_text(statuses[0], limit=60)
    long = detail_text(statuses[0], limit=200)
    assert short and long.startswith(short[:40])
    assert short in status_table(statuses)
    assert "expected &gt;=" in html_report(statuses)


# -- registry-derived error messages ----------------------------------


def test_missing_source_type_lists_every_registered_source():
    """The old hardcoded list had already gone stale — it never mentioned zfs."""
    with pytest.raises(ConfigError) as excinfo:
        build_config(
            {
                "version": 1,
                "jobs": [
                    {"name": "x", "source": {}, "verify": [{"type": "files", "min_files": 1}]}
                ],
            }
        )
    message = str(excinfo.value)
    for source_type in ("restic", "borg", "local", "zfs"):
        assert source_type in message


# -- one path-containment mechanism -----------------------------------


def test_tar_extraction_refuses_members_escaping_the_restore(tmp_path):
    """The archive check and the verifier check are now the same helper."""
    from restore_guard.sources import SourceError

    repo = tmp_path / "backups"
    repo.mkdir()
    payload = tmp_path / "evil.txt"
    payload.write_text("owned")

    archive_path = repo / "evil.tar"
    with tarfile.open(archive_path, "w") as archive:
        archive.add(payload, arcname="../../escaped.txt")

    job = {
        "name": "evil",
        "source": {"type": "local", "repository": str(repo), "mode": "tar"},
        "verify": [{"type": "files", "min_files": 1}],
    }
    config = build_config(make_config(tmp_path / "work", [job]))
    source = build_source(config.jobs[0], Logger(quiet=True))

    dest = tmp_path / "restore"
    dest.mkdir()
    assert source.list_snapshots(), "the malicious archive should be listed as a snapshot"

    # GNU tar strips leading "../" itself; the python fallback must refuse it.
    from restore_guard.sources.local import _safe_extract

    with tarfile.open(archive_path) as archive:
        with pytest.raises(SourceError, match="outside"):
            _safe_extract(archive, dest, archive_path.name)

    assert not (tmp_path / "escaped.txt").exists()
