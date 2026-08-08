"""Orchestration: pick a snapshot, restore it, verify it, record the verdict."""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from .config import Config, ConfigError, JobConfig
from .sources import Snapshot, SourceError, build_source
from .state import (
    STATUS_ERROR,
    STATUS_FAILED,
    STATUS_OK,
    STATUS_SKIPPED,
    RunRecord,
    State,
)
from .util import Logger, dir_stats, human_bytes, human_duration, rmtree_quiet
from .verifiers import VerifyError, VerifyResult, build_verifiers


@dataclass
class JobOutcome:
    record: RunRecord
    previous: RunRecord | None
    restore_dir: Path | None = None

    @property
    def is_recovery(self) -> bool:
        return self.record.ok and self.previous is not None and not self.previous.ok

    @property
    def is_new_failure(self) -> bool:
        return not self.record.ok and (self.previous is None or self.previous.ok)


class Runner:
    def __init__(
        self,
        config: Config,
        state: State,
        logger: Logger,
        dry_run: bool = False,
        parallel: int = 1,
    ):
        self.config = config
        self.state = state
        self.log = logger
        self.dry_run = dry_run
        self.parallel = max(1, int(parallel))

    def run_all(self, only: list[str] | None = None) -> list[JobOutcome]:
        jobs = self.config.jobs
        if only:
            jobs = [self.config.job(name) for name in only]

        selected = []
        for job in jobs:
            if not job.enabled and not only:
                self.log.info(f"{job.name}: disabled, skipping")
                continue
            selected.append(job)

        if self.parallel > 1 and len(selected) > 1:
            outcomes = self._run_parallel(selected)
        else:
            outcomes = [self.run_job(job) for job in selected]

        if self.config.history_limit:
            self.state.prune(self.config.history_limit)
        return outcomes

    def _run_parallel(self, jobs: list[JobConfig]) -> list[JobOutcome]:
        """Run jobs on a thread pool.

        Threads, not processes: every job spends its time waiting on restic,
        tar or docker, so the GIL is never the bottleneck. Results are returned
        in config order regardless of completion order, so the status table
        does not reshuffle itself between runs.
        """
        workers = min(self.parallel, len(jobs))
        self.log.info(f"running {len(jobs)} jobs, {workers} at a time")

        results: dict[str, JobOutcome] = {}
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="rg-job") as pool:
            futures = {pool.submit(self.run_job, job): job for job in jobs}
            for future in as_completed(futures):
                job = futures[future]
                results[job.name] = future.result()
        return [results[job.name] for job in jobs]

    def run_job(self, job: JobConfig) -> JobOutcome:
        previous = self.state.last_run(job.name)
        started = time.time()
        clock = time.monotonic()

        self.log.step(f"{job.name}: starting verification")
        restore_dir = self._restore_dir(job)
        snapshot: Snapshot | None = None
        source = None
        keep = job.keep_on_failure if job.keep_on_failure is not None else self.config.keep_on_failure

        try:
            source = build_source(job, self.log)
            verifiers = build_verifiers(job)

            source.preflight()
            snapshots = source.list_snapshots()
            snapshot = source.select(snapshots)
            self.log.info(
                f"{job.name}: {len(snapshots)} snapshot(s) available, "
                f"verifying {snapshot.short} ({source.selection})"
            )

            if self.dry_run:
                record = self._record(
                    job, STATUS_SKIPPED, started, clock, snapshot,
                    message="dry run: snapshot selected but not restored",
                )
                return JobOutcome(record, previous)

            restore_dir.mkdir(parents=True, exist_ok=True)
            budget = job.timeout
            outcome = source.restore(snapshot, restore_dir, timeout=budget)
            stats = dir_stats(restore_dir)
            self.log.info(
                f"{job.name}: restored {stats.files} files "
                f"({human_bytes(stats.bytes)}) in {human_duration(outcome.duration)}"
            )

            remaining = max(30.0, budget - outcome.duration)
            results = self._verify(job, verifiers, restore_dir, snapshot, remaining)

            failures = [r for r in results if not r.ok]
            status = STATUS_FAILED if failures else STATUS_OK
            message = (
                "; ".join(r.summary for r in failures)
                if failures
                else "; ".join(r.summary for r in results) or "no checks produced output"
            )
            record = self._record(
                job, status, started, clock, snapshot,
                message=message,
                files=stats.files,
                bytes=stats.bytes,
                details={
                    "restore_seconds": round(outcome.duration, 1),
                    "checks": [_result_dict(r) for r in results],
                    "snapshots_available": len(snapshots),
                    "selection": source.selection,
                },
            )
            if status == STATUS_OK:
                self.log.ok(f"{job.name}: VERIFIED from {snapshot.short}")
            else:
                self.log.fail(f"{job.name}: FAILED — {message}")

        except (SourceError, VerifyError, ConfigError) as exc:
            self.log.fail(f"{job.name}: ERROR — {exc}")
            record = self._record(
                job, STATUS_ERROR, started, clock, snapshot, message=str(exc)
            )
        except Exception as exc:  # unexpected, but a crashing job must not kill the run
            self.log.fail(f"{job.name}: ERROR — unexpected {type(exc).__name__}: {exc}")
            record = self._record(
                job, STATUS_ERROR, started, clock, snapshot,
                message=f"unexpected {type(exc).__name__}: {exc}",
            )

        kept = self._cleanup(source, restore_dir, record.status == STATUS_OK, keep, job)
        return JobOutcome(record, previous, restore_dir if kept else None)

    # -- internals -----------------------------------------------------

    def _verify(
        self,
        job: JobConfig,
        verifiers,
        restore_dir: Path,
        snapshot: Snapshot,
        budget: float,
    ) -> list[VerifyResult]:
        from .verifiers import VerifyContext

        results: list[VerifyResult] = []
        deadline = time.monotonic() + budget

        for verifier in verifiers:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                results.append(
                    VerifyResult(
                        False,
                        f"{verifier.name}: skipped, job timeout of "
                        f"{human_duration(job.timeout)} exhausted",
                        type=verifier.type,
                    )
                )
                continue

            ctx = VerifyContext(
                restore_dir=restore_dir,
                snapshot=snapshot,
                job=job,
                timeout=remaining,
                log=self.log,
            )
            try:
                result = verifier.run(ctx)
            except VerifyError as exc:
                result = VerifyResult(False, f"{verifier.name}: {exc}", type=verifier.type)
            results.append(result)
            marker = "ok" if result.ok else "FAIL"
            self.log.debug(f"{job.name}: [{marker}] {result.summary}")

        return results

    def _restore_dir(self, job: JobConfig) -> Path:
        stamp = time.strftime("%Y%m%dT%H%M%S")
        return self.config.restores_path / f"{job.name}-{stamp}-{os.getpid()}"

    def _cleanup(
        self,
        source,
        restore_dir: Path,
        succeeded: bool,
        keep_on_failure: bool,
        job: JobConfig,
    ) -> bool:
        """Release the restore. Returns True if it was deliberately left behind."""
        if not restore_dir.exists():
            return False

        if not succeeded and keep_on_failure:
            self.log.info(f"{job.name}: restore kept for inspection at {restore_dir}")
            return True

        # A source that mounts something (ZFS clone) must tear it down itself —
        # deleting the directory would delete through the mount into live data.
        if source is not None and getattr(source, "manages_destination", False):
            try:
                source.cleanup(restore_dir, succeeded)
            except Exception as exc:  # cleanup must never mask the job's verdict
                self.log.fail(f"{job.name}: cleanup failed: {exc}")
                return True
            # Only remove the mountpoint once the source has emptied it.
            if restore_dir.exists() and not any(restore_dir.iterdir()):
                rmtree_quiet(restore_dir)
            return False

        rmtree_quiet(restore_dir)
        return False

    def _record(
        self,
        job: JobConfig,
        status: str,
        started: float,
        clock: float,
        snapshot: Snapshot | None,
        *,
        message: str = "",
        files: int = 0,
        bytes: int = 0,
        details: dict | None = None,
    ) -> RunRecord:
        duration = time.monotonic() - clock
        record = RunRecord(
            job=job.name,
            status=status,
            started_at=started,
            finished_at=started + duration,
            duration=duration,
            snapshot_id=snapshot.id if snapshot else None,
            snapshot_time=snapshot.time if snapshot else None,
            files=files,
            bytes=bytes,
            message=message[:4000],
            details=details or {},
        )
        return self.state.record(record)


def _result_dict(result: VerifyResult) -> dict:
    return {
        "type": result.type,
        "ok": result.ok,
        "summary": result.summary,
        "duration": round(result.duration, 2),
        "details": result.details,
    }
