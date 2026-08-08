"""End-to-end: config -> restore -> verify -> recorded verdict."""

import time

from restore_guard.config import build_config
from restore_guard.report import collect, prometheus_metrics, status_table
from restore_guard.runner import Runner
from restore_guard.state import STATUS_ERROR, STATUS_FAILED, STATUS_OK, State
from restore_guard.util import Logger

from conftest import make_config


def run_jobs(raw_config, tmp_path):
    config = build_config(raw_config)
    state = State(config.state_path)
    runner = Runner(config, state, Logger(quiet=True))
    outcomes = runner.run_all()
    return config, state, outcomes


def test_successful_verification_is_recorded(backup_dir, tmp_path):
    raw = make_config(
        tmp_path / "work",
        [
            {
                "name": "files-job",
                "source": {"type": "local", "repository": str(backup_dir)},
                "verify": [
                    {"type": "files", "min_files": 2, "must_exist": ["etc/config.yaml"]}
                ],
            }
        ],
    )
    config, state, outcomes = run_jobs(raw, tmp_path)

    assert len(outcomes) == 1
    record = outcomes[0].record
    assert record.status == STATUS_OK
    assert record.snapshot_id == "snap-2026-08-08"
    assert record.files == 2
    assert record.bytes > 0
    assert state.last_success("files-job").id == record.id
    state.close()


def test_failed_check_marks_job_failed(backup_dir, tmp_path):
    raw = make_config(
        tmp_path / "work",
        [
            {
                "name": "too-small",
                "source": {"type": "local", "repository": str(backup_dir)},
                "verify": [{"type": "files", "min_bytes": "10GiB"}],
            }
        ],
    )
    config, state, outcomes = run_jobs(raw, tmp_path)

    assert outcomes[0].record.status == STATUS_FAILED
    assert state.last_success("too-small") is None
    assert "expected >=" in outcomes[0].record.message
    state.close()


def test_unreachable_repository_is_an_error_not_a_failure(tmp_path):
    raw = make_config(
        tmp_path / "work",
        [
            {
                "name": "missing-repo",
                "source": {"type": "local", "repository": str(tmp_path / "nope")},
                "verify": [{"type": "files", "min_files": 1}],
            }
        ],
    )
    config, state, outcomes = run_jobs(raw, tmp_path)

    assert outcomes[0].record.status == STATUS_ERROR
    assert "does not exist" in outcomes[0].record.message
    state.close()


def test_one_broken_job_does_not_stop_the_others(backup_dir, tmp_path):
    raw = make_config(
        tmp_path / "work",
        [
            {
                "name": "broken",
                "source": {"type": "local", "repository": str(tmp_path / "nope")},
                "verify": [{"type": "files", "min_files": 1}],
            },
            {
                "name": "healthy",
                "source": {"type": "local", "repository": str(backup_dir)},
                "verify": [{"type": "files", "min_files": 1}],
            },
        ],
    )
    config, state, outcomes = run_jobs(raw, tmp_path)

    assert [o.record.status for o in outcomes] == [STATUS_ERROR, STATUS_OK]
    state.close()


def test_restore_directory_is_cleaned_up_on_success(backup_dir, tmp_path):
    raw = make_config(
        tmp_path / "work",
        [
            {
                "name": "tidy",
                "source": {"type": "local", "repository": str(backup_dir)},
                "verify": [{"type": "files", "min_files": 1}],
            }
        ],
    )
    config, state, _ = run_jobs(raw, tmp_path)
    leftovers = list(config.restores_path.glob("*")) if config.restores_path.exists() else []
    assert leftovers == []
    state.close()


def test_failed_restore_is_kept_for_inspection(backup_dir, tmp_path):
    raw = make_config(
        tmp_path / "work",
        [
            {
                "name": "keepme",
                "source": {"type": "local", "repository": str(backup_dir)},
                "verify": [{"type": "files", "min_bytes": "10GiB"}],
            }
        ],
        keep_on_failure=True,
    )
    config, state, outcomes = run_jobs(raw, tmp_path)
    assert outcomes[0].restore_dir is not None
    assert outcomes[0].restore_dir.exists()
    state.close()


def test_dry_run_selects_without_restoring(backup_dir, tmp_path):
    config = build_config(
        make_config(
            tmp_path / "work",
            [
                {
                    "name": "dry",
                    "source": {"type": "local", "repository": str(backup_dir)},
                    "verify": [{"type": "files", "min_files": 1}],
                }
            ],
        )
    )
    state = State(config.state_path)
    outcomes = Runner(config, state, Logger(quiet=True), dry_run=True).run_all()
    assert outcomes[0].record.status == "skipped"
    assert outcomes[0].record.snapshot_id == "snap-2026-08-08"
    state.close()


def test_recovery_is_detected(backup_dir, tmp_path):
    workdir = tmp_path / "work"
    failing = make_config(
        workdir,
        [
            {
                "name": "flappy",
                "source": {"type": "local", "repository": str(backup_dir)},
                "verify": [{"type": "files", "min_bytes": "10GiB"}],
            }
        ],
    )
    config, state, outcomes = run_jobs(failing, tmp_path)
    assert outcomes[0].is_new_failure
    state.close()

    passing = make_config(
        workdir,
        [
            {
                "name": "flappy",
                "source": {"type": "local", "repository": str(backup_dir)},
                "verify": [{"type": "files", "min_files": 1}],
            }
        ],
    )
    config, state, outcomes = run_jobs(passing, tmp_path)
    assert outcomes[0].is_recovery
    state.close()


def test_status_reports_stale_after_max_age(backup_dir, tmp_path):
    raw = make_config(
        tmp_path / "work",
        [
            {
                "name": "aging",
                "source": {"type": "local", "repository": str(backup_dir)},
                "verify": [{"type": "files", "min_files": 1}],
                "max_age": "1h",
            }
        ],
    )
    config, state, _ = run_jobs(raw, tmp_path)

    fresh = collect(config, state)[0]
    assert fresh.label == "OK"
    assert not fresh.stale

    # Age the recorded success past max_age.
    state._conn.execute(
        "UPDATE runs SET finished_at = ?, started_at = ?", (time.time() - 7200, time.time() - 7300)
    )
    state._conn.commit()

    aged = collect(config, state)[0]
    assert aged.stale
    assert aged.label == "STALE"
    assert "STALE" in status_table([aged])
    state.close()


def test_prometheus_metrics_render(backup_dir, tmp_path):
    raw = make_config(
        tmp_path / "work",
        [
            {
                "name": "metrics-job",
                "source": {"type": "local", "repository": str(backup_dir)},
                "verify": [{"type": "files", "min_files": 1}],
            }
        ],
    )
    config, state, _ = run_jobs(raw, tmp_path)
    text = prometheus_metrics(collect(config, state))
    assert 'restore_guard_stale{job="metrics-job"} 0' in text
    assert 'restore_guard_last_run_success{job="metrics-job"} 1' in text
    state.close()


def test_history_is_pruned(backup_dir, tmp_path):
    raw = make_config(
        tmp_path / "work",
        [
            {
                "name": "chatty",
                "source": {"type": "local", "repository": str(backup_dir)},
                "verify": [{"type": "files", "min_files": 1}],
            }
        ],
        history_limit=3,
    )
    config = build_config(raw)
    state = State(config.state_path)
    runner = Runner(config, state, Logger(quiet=True))
    for _ in range(5):
        runner.run_all()
    assert len(state.history("chatty", limit=50)) == 3
    state.close()
