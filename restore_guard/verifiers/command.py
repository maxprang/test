"""Escape hatch: run an arbitrary command against the restored data.

`sha256sum -c manifest.txt`, `gpg --verify`, `tar -tzf`, `python check_it.py` —
anything you can script. Placeholders {restore_dir}, {snapshot}, {job} are
substituted into argv and env.
"""

from __future__ import annotations

import time
from typing import Any

from ..config import ConfigError
from ..util import CommandError, as_list, expand_placeholders, run
from . import VerifyContext, VerifyError, VerifyResult, Verifier, register


@register
class CommandVerifier(Verifier):
    type = "command"

    def validate(self) -> None:
        command = self._required("run")
        if not isinstance(command, (str, list)):
            raise ConfigError(
                f"job {self.job.name!r}: verify.command.run must be a string or a list"
            )
        if isinstance(command, str) and not self.spec.get("shell", False):
            raise ConfigError(
                f"job {self.job.name!r}: verify.command.run is a string — pass a list "
                "(['sha256sum', '-c', 'sums.txt']) or set shell: true"
            )

    def run(self, ctx: VerifyContext) -> VerifyResult:
        started = time.monotonic()
        placeholders = {
            "restore_dir": str(ctx.restore_dir),
            "snapshot": ctx.snapshot.short,
            "snapshot_id": ctx.snapshot.id,
            "job": ctx.job.name,
        }

        command = expand_placeholders(self.spec["run"], placeholders)
        if self.spec.get("shell", False):
            argv = ["/bin/sh", "-c", command if isinstance(command, str) else " ".join(command)]
        else:
            argv = [str(part) for part in command]

        env = {
            "RESTORE_DIR": str(ctx.restore_dir),
            "RESTORE_SNAPSHOT": ctx.snapshot.short,
            "RESTORE_JOB": ctx.job.name,
        }
        env.update(
            {k: str(v) for k, v in expand_placeholders(self.spec.get("env") or {}, placeholders).items()}
        )

        workdir = self.spec.get("workdir")
        cwd = ctx.resolve(str(workdir)) if workdir else ctx.restore_dir
        timeout = min(float(self.spec.get("timeout", ctx.timeout)), ctx.timeout)

        try:
            result = run(argv, timeout=timeout, env=env, cwd=cwd)
        except CommandError as exc:
            raise VerifyError(str(exc)) from exc

        expected_rc = int(self.spec.get("expect_returncode", 0))
        problems: list[str] = []
        if result.timed_out:
            problems.append(f"timed out after {timeout:.0f}s")
        elif result.returncode != expected_rc:
            problems.append(f"exit code {result.returncode}, expected {expected_rc}")

        # Search the two streams separately rather than concatenating them:
        # a checksum manifest over a large restore produces one line per file,
        # and a joined copy would double that in memory for no benefit.
        streams = (result.stdout or "", result.stderr or "")
        for needle in as_list(self.spec.get("stdout_contains")):
            if not any(str(needle) in stream for stream in streams):
                problems.append(f"output does not contain {needle!r}")
        for needle in as_list(self.spec.get("stdout_excludes")):
            if any(str(needle) in stream for stream in streams):
                problems.append(f"output unexpectedly contains {needle!r}")

        details: dict[str, Any] = {
            "argv": argv,
            "returncode": result.returncode,
            "output_tail": result.tail(12),
        }
        label = self.spec.get("name") or " ".join(argv[:3])
        summary = f"{label}: " + ("; ".join(problems) if problems else "ok")
        verify_result = self.failed(summary, **details) if problems else self.ok(summary, **details)
        verify_result.duration = time.monotonic() - started
        return verify_result
