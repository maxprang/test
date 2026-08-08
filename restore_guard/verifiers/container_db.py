"""Shared machinery for verifying a database dump in a throwaway container.

`postgres` and `mysql` differ only in which image they start, how they wait for
readiness, how a dump is fed in, and how a scalar query is issued. Everything
around that — locating the dump, decompression, container lifecycle, running
the check list, assembling the result — is identical, so it lives here.
"""

from __future__ import annotations

import time
from abc import abstractmethod
from pathlib import Path
from typing import Any

from ..docker import Docker, DockerError
from ..util import human_duration, shell_quote
from . import VerifyContext, VerifyError, VerifyResult, Verifier
from .checks import QueryFailed, run_checks, validate_check_specs

#: Streaming decompressors, keyed by file suffix. All are present in the
#: official postgres and mariadb images except zstd, which fails with a clear
#: error from the shell if absent.
DECOMPRESSORS = {
    ".gz": "gunzip -c",
    ".bz2": "bunzip2 -c",
    ".xz": "xz -dc",
    ".zst": "zstd -dc",
}

PLAIN_SUFFIXES = (".sql", ".psql")

#: Where the restore is mounted inside the container.
MOUNT = "/restore"


class ContainerDbVerifier(Verifier):
    """Base for verifiers that replay a dump into a disposable database."""

    #: Default image, overridden per subclass.
    default_image = ""
    #: Config key holding the check list (`checks` today for both subclasses).
    checks_key = "checks"

    # -- configuration -------------------------------------------------

    def validate(self) -> None:
        self._required("dump")
        self.check_specs = validate_check_specs(
            self.spec.get(self.checks_key), f"verify.{self.type}.{self.checks_key}"
        )
        self.validate_engine()

    def validate_engine(self) -> None:
        """Subclass hook for engine-specific config validation."""

    @property
    def image(self) -> str:
        return str(self.spec.get("image", self.default_image))

    @property
    def database(self) -> str:
        return str(self.spec.get("database", "verify"))

    @property
    def password(self) -> str:
        return str(self.spec.get("password", "verify"))

    # -- subclass hooks ------------------------------------------------

    @abstractmethod
    def container_env(self) -> dict[str, str]:
        """Environment that makes the image come up with an empty database."""

    @abstractmethod
    def readiness_probe(self) -> list[str]:
        """A command, run inside the container, that succeeds once it accepts queries."""

    @abstractmethod
    def load_command(self, dump: str) -> list[str]:
        """A command that feeds ``dump`` (a path inside the container) into the database."""

    @abstractmethod
    def query_command(self, sql: str) -> list[str]:
        """A command that prints the first cell of ``sql`` and nothing else."""

    @abstractmethod
    def count_tables_sql(self) -> str:
        """SQL returning the number of user tables in the restored database."""

    def container_command(self) -> list[str] | None:
        """Extra arguments for the image's entrypoint (e.g. durability flags)."""
        return None

    def extra_problems(self, docker: Docker, container: str, ctx: VerifyContext) -> list[str]:
        """Engine-specific checks that run after the standard ones."""
        return []

    # -- the shared flow -----------------------------------------------

    def run(self, ctx: VerifyContext) -> VerifyResult:
        started = time.monotonic()
        docker = Docker(str(self.spec.get("docker_binary", "docker")), ctx.log)
        try:
            docker.require()
        except DockerError as exc:
            raise VerifyError(str(exc)) from exc

        dump_path = self._find_dump(ctx)
        in_container = f"{MOUNT}/" + str(dump_path.relative_to(ctx.restore_dir))

        problems: list[str] = []
        details: dict[str, Any] = {
            "dump": str(dump_path.relative_to(ctx.restore_dir)),
            "dump_bytes": dump_path.stat().st_size,
            "image": self.image,
        }

        try:
            with docker.ephemeral(
                self.image,
                env=self.container_env(),
                mounts=[(ctx.restore_dir, MOUNT, True)],
                command=self.container_command(),
                pull=bool(self.spec.get("pull", False)),
                timeout=min(600, ctx.timeout),
            ) as container:
                ready_timeout = min(float(self.spec.get("ready_timeout", 180)), ctx.timeout)
                docker.wait_healthy(container, self.readiness_probe(), timeout=ready_timeout)
                details["startup_seconds"] = round(time.monotonic() - started, 1)

                details["load_seconds"] = round(
                    self._load(docker, container, in_container, ctx), 1
                )

                results, check_problems = run_checks(
                    self.check_specs,
                    lambda spec: self._scalar(docker, container, spec, ctx),
                )
                problems += check_problems
                if results:
                    details["checks"] = results

                if self.spec.get("min_tables") is not None:
                    tables = self._count_tables(docker, container, ctx)
                    details["tables"] = tables
                    if tables < int(self.spec["min_tables"]):
                        problems.append(
                            f"{tables} tables restored, expected >= {self.spec['min_tables']}"
                        )

                problems += self.extra_problems(docker, container, ctx)
        except DockerError as exc:
            raise VerifyError(f"{self.type} verification could not run: {exc}") from exc

        headline = f"{dump_path.name} loaded in {human_duration(details.get('load_seconds', 0))}"
        summary = f"{headline}; " + ("; ".join(problems) if problems else "all checks passed")
        result = self.failed(summary, **details) if problems else self.ok(summary, **details)
        result.duration = time.monotonic() - started
        return result

    # -- steps ---------------------------------------------------------

    def _find_dump(self, ctx: VerifyContext) -> Path:
        matches = [m for m in sorted(ctx.restore_dir.glob(str(self.spec["dump"]))) if m.is_file()]
        if not matches:
            raise VerifyError(
                f"no dump matching {self.spec['dump']!r} in the restore — "
                "the backup did not contain what the config expects"
            )
        # Newest wins when the pattern matches a whole directory of dumps.
        return max(matches, key=lambda path: path.stat().st_mtime)

    def _load(self, docker: Docker, container: str, dump: str, ctx: VerifyContext) -> float:
        started = time.monotonic()
        timeout = min(float(self.spec.get("load_timeout", ctx.timeout)), ctx.timeout)

        result = docker.exec(container, self.load_command(dump), timeout=timeout)
        if not result.ok:
            detail = "timed out" if result.timed_out else result.tail(15)
            raise VerifyError(f"loading {Path(dump).name} into {self.type} failed:\n{detail}")
        return time.monotonic() - started

    def _scalar(
        self, docker: Docker, container: str, spec: dict[str, Any], ctx: VerifyContext
    ) -> str:
        result = docker.exec(
            container,
            self.query_command(str(spec["sql"])),
            timeout=min(float(spec.get("timeout", 120)), ctx.timeout),
        )
        if not result.ok:
            raise QueryFailed(result.tail(4) or "query failed")
        lines = [line for line in result.stdout.strip().splitlines() if line.strip()]
        return lines[0].split("\t")[0].strip() if lines else ""

    def _count_tables(self, docker: Docker, container: str, ctx: VerifyContext) -> int:
        try:
            value = self._scalar(
                docker, container, {"sql": self.count_tables_sql()}, ctx
            )
        except QueryFailed:
            return 0
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0


def decompression_pipeline(dump: str) -> str:
    """Shell fragment that writes the dump's plain SQL to stdout."""
    suffix = Path(dump).suffix.lower()
    if suffix in DECOMPRESSORS:
        return f"{DECOMPRESSORS[suffix]} {shell_quote(dump)}"
    return f"cat {shell_quote(dump)}"
