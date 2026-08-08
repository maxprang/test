"""Status page, parallel execution and the runner's cleanup contract."""

from restore_guard.config import build_config
from restore_guard.report import collect, html_report
from restore_guard.runner import Runner
from restore_guard.state import STATUS_OK, State
from restore_guard.util import Logger

from conftest import make_config


def jobs_for(backup_dir, count, min_bytes=None):
    jobs = []
    for index in range(count):
        verify = {"type": "files", "min_files": 1}
        if min_bytes:
            verify = {"type": "files", "min_bytes": min_bytes}
        jobs.append(
            {
                "name": f"job-{index}",
                "source": {"type": "local", "repository": str(backup_dir)},
                "verify": [verify],
            }
        )
    return jobs


# -- parallel ---------------------------------------------------------


def test_parallel_run_verifies_every_job(backup_dir, tmp_path):
    config = build_config(make_config(tmp_path / "work", jobs_for(backup_dir, 6)))
    state = State(config.state_path)
    outcomes = Runner(config, state, Logger(quiet=True), parallel=4).run_all()

    assert len(outcomes) == 6
    assert all(o.record.status == STATUS_OK for o in outcomes)
    assert {o.record.job for o in outcomes} == {f"job-{i}" for i in range(6)}
    state.close()


def test_parallel_results_keep_config_order(backup_dir, tmp_path):
    config = build_config(make_config(tmp_path / "work", jobs_for(backup_dir, 5)))
    state = State(config.state_path)
    outcomes = Runner(config, state, Logger(quiet=True), parallel=5).run_all()

    assert [o.record.job for o in outcomes] == [f"job-{i}" for i in range(5)]
    state.close()


def test_parallel_history_is_complete(backup_dir, tmp_path):
    """Every thread's record must survive the shared SQLite connection."""
    config = build_config(make_config(tmp_path / "work", jobs_for(backup_dir, 8)))
    state = State(config.state_path)
    Runner(config, state, Logger(quiet=True), parallel=8).run_all()

    assert len(state.history(limit=100)) == 8
    assert sorted(state.jobs_seen()) == sorted(f"job-{i}" for i in range(8))
    state.close()


def test_parallel_isolates_failures(backup_dir, tmp_path):
    jobs = jobs_for(backup_dir, 2)
    jobs.append(
        {
            "name": "doomed",
            "source": {"type": "local", "repository": str(tmp_path / "nope")},
            "verify": [{"type": "files", "min_files": 1}],
        }
    )
    config = build_config(make_config(tmp_path / "work", jobs))
    state = State(config.state_path)
    outcomes = Runner(config, state, Logger(quiet=True), parallel=3).run_all()

    by_name = {o.record.job: o.record.status for o in outcomes}
    assert by_name["job-0"] == STATUS_OK
    assert by_name["job-1"] == STATUS_OK
    assert by_name["doomed"] == "error"
    state.close()


# -- html report ------------------------------------------------------


def test_html_report_is_self_contained(backup_dir, tmp_path):
    config = build_config(make_config(tmp_path / "work", jobs_for(backup_dir, 1)))
    state = State(config.state_path)
    Runner(config, state, Logger(quiet=True)).run_all()
    page = html_report(collect(config, state))
    state.close()

    assert page.startswith("<!doctype html>")
    assert "job-0" in page
    assert 'class="pill OK"' in page
    # No network dependency: an outage page must render during an outage.
    assert "http://" not in page and "https://" not in page
    assert "<script" not in page


def test_html_report_marks_failures(backup_dir, tmp_path):
    """FAIL means 'used to verify, now broken' — a job that never worked is NEVER."""
    workdir = tmp_path / "work"
    healthy = build_config(make_config(workdir, jobs_for(backup_dir, 1)))
    state = State(healthy.state_path)
    Runner(healthy, state, Logger(quiet=True)).run_all()
    assert 'class="pill OK"' in html_report(collect(healthy, state))
    state.close()

    broken = build_config(make_config(workdir, jobs_for(backup_dir, 1, min_bytes="10GiB")))
    state = State(broken.state_path)
    Runner(broken, state, Logger(quiet=True)).run_all()
    page = html_report(collect(broken, state))
    state.close()

    assert 'class="pill FAIL"' in page
    assert "need attention" in page
    assert "expected &gt;=" in page  # the failure reason is on the page, escaped


def test_html_report_escapes_job_names(backup_dir, tmp_path):
    jobs = [
        {
            "name": "<script>alert(1)</script>",
            "source": {"type": "local", "repository": str(backup_dir)},
            "verify": [{"type": "files", "min_files": 1}],
        }
    ]
    config = build_config(make_config(tmp_path / "work", jobs))
    state = State(config.state_path)
    Runner(config, state, Logger(quiet=True)).run_all()
    page = html_report(collect(config, state))
    state.close()

    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


def test_html_handles_a_job_that_never_ran(backup_dir, tmp_path):
    config = build_config(make_config(tmp_path / "work", jobs_for(backup_dir, 1)))
    state = State(config.state_path)
    page = html_report(collect(config, state))
    state.close()

    assert 'class="pill NEVER"' in page
    assert "never successfully verified" in page
