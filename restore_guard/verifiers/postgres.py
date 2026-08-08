"""Load a restored dump into a throwaway PostgreSQL container and query it.

This is the check that actually answers "could I bring the service back?".
A dump file that exists and even gunzips cleanly can still be a truncated
transaction or an empty schema; only replaying it into a real server proves it.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from ..config import ConfigError
from ..docker import Docker, DockerError
from ..util import human_duration
from . import VerifyContext, VerifyError, VerifyResult, Verifier, register

_PLAIN_SUFFIXES = (".sql", ".psql")
_CUSTOM_SUFFIXES = (".dump", ".custom", ".pgdump", ".backup")
_DECOMPRESSORS = {
    ".gz": "gunzip -c",
    ".bz2": "bunzip2 -c",
    ".xz": "xz -dc",
    ".zst": "zstd -dc",
}


@register
class PostgresVerifier(Verifier):
    type = "postgres"

    def validate(self) -> None:
        self._required("dump")
        for position, check in enumerate(self.spec.get("checks") or []):
            if not isinstance(check, dict) or not check.get("sql"):
                raise ConfigError(
                    f"job {self.job.name!r}: verify.postgres.checks[{position}] needs 'sql'"
                )

    # -- settings ------------------------------------------------------

    @property
    def image(self) -> str:
        return str(self.spec.get("image", "postgres:16-alpine"))

    @property
    def db_user(self) -> str:
        return str(self.spec.get("user", "verify"))

    @property
    def db_name(self) -> str:
        return str(self.spec.get("database", "verify"))

    # -- run -----------------------------------------------------------

    def run(self, ctx: VerifyContext) -> VerifyResult:
        started = time.monotonic()
        docker = Docker(str(self.spec.get("docker_binary", "docker")), ctx.log)
        try:
            docker.require()
        except DockerError as exc:
            raise VerifyError(str(exc)) from exc

        dump_path = self._find_dump(ctx)
        in_container = "/restore/" + str(dump_path.relative_to(ctx.restore_dir))

        env = {
            "POSTGRES_USER": self.db_user,
            "POSTGRES_PASSWORD": str(self.spec.get("password", "verify")),
            "POSTGRES_DB": self.db_name,
            # We throw the cluster away; durability is wasted time here.
            "PGOPTIONS": "-c fsync=off -c full_page_writes=off -c synchronous_commit=off",
        }

        problems: list[str] = []
        details: dict[str, Any] = {
            "dump": str(dump_path.relative_to(ctx.restore_dir)),
            "dump_bytes": dump_path.stat().st_size,
            "image": self.image,
        }

        try:
            with docker.ephemeral(
                self.image,
                env=env,
                mounts=[(ctx.restore_dir, "/restore", True)],
                pull=bool(self.spec.get("pull", False)),
                timeout=min(600, ctx.timeout),
            ) as container:
                ready_timeout = float(self.spec.get("ready_timeout", 120))
                docker.wait_healthy(
                    container,
                    ["pg_isready", "-U", self.db_user, "-d", self.db_name],
                    timeout=min(ready_timeout, ctx.timeout),
                )
                details["startup_seconds"] = round(time.monotonic() - started, 1)

                load_seconds = self._load(docker, container, in_container, ctx)
                details["load_seconds"] = round(load_seconds, 1)

                check_results = []
                for check in self.spec.get("checks") or []:
                    outcome = self._query(docker, container, check, ctx)
                    check_results.append(outcome)
                    if outcome["problem"]:
                        problems.append(outcome["problem"])
                if check_results:
                    details["checks"] = check_results

                if self.spec.get("min_tables") is not None:
                    tables = self._count_tables(docker, container, ctx)
                    details["tables"] = tables
                    if tables < int(self.spec["min_tables"]):
                        problems.append(
                            f"{tables} tables restored, expected >= {self.spec['min_tables']}"
                        )
        except DockerError as exc:
            raise VerifyError(f"postgres verification could not run: {exc}") from exc

        elapsed = time.monotonic() - started
        headline = f"{dump_path.name} loaded in {human_duration(details.get('load_seconds', 0))}"
        summary = f"{headline}; " + ("; ".join(problems) if problems else "all checks passed")
        result = self.failed(summary, **details) if problems else self.ok(summary, **details)
        result.duration = elapsed
        return result

    # -- steps ---------------------------------------------------------

    def _find_dump(self, ctx: VerifyContext) -> Path:
        matches = sorted(ctx.restore_dir.glob(str(self.spec["dump"])))
        matches = [m for m in matches if m.is_file()]
        if not matches:
            raise VerifyError(
                f"no dump matching {self.spec['dump']!r} in the restore — "
                "the backup did not contain what the config expects"
            )
        # Newest wins when a pattern matches a whole directory of dumps.
        return max(matches, key=lambda p: p.stat().st_mtime)

    def _load(self, docker: Docker, container: str, dump: str, ctx: VerifyContext) -> float:
        started = time.monotonic()
        timeout = float(self.spec.get("load_timeout", ctx.timeout))
        suffix = Path(dump).suffix.lower()

        if suffix in _DECOMPRESSORS:
            inner = Path(dump).with_suffix("").suffix.lower()
            if inner in _CUSTOM_SUFFIXES:
                raise VerifyError(
                    f"{Path(dump).name}: compressed custom-format dumps are not supported "
                    "(pg_dump -Fc is already compressed); point 'dump' at a plain .sql[.gz] "
                    "or at the uncompressed .dump"
                )
            pipeline = f"{_DECOMPRESSORS[suffix]} {_quote(dump)} | psql -v ON_ERROR_STOP=1 -q"
            command = ["sh", "-c", pipeline]
        elif suffix in _PLAIN_SUFFIXES:
            command = ["sh", "-c", f"psql -v ON_ERROR_STOP=1 -q -f {_quote(dump)}"]
        else:
            # pg_dump custom/directory format
            command = [
                "sh",
                "-c",
                f"pg_restore --exit-on-error --no-owner --no-privileges "
                f"-d {_quote(self.db_name)} {_quote(dump)}",
            ]

        env_prefix = f"PGUSER={_quote(self.db_user)} PGDATABASE={_quote(self.db_name)} "
        command[-1] = env_prefix + command[-1]

        result = docker.exec(container, command, timeout=min(timeout, ctx.timeout))
        if not result.ok:
            detail = "timed out" if result.timed_out else result.tail(15)
            raise VerifyError(f"loading {Path(dump).name} into postgres failed:\n{detail}")
        return time.monotonic() - started

    def _query(
        self, docker: Docker, container: str, check: dict[str, Any], ctx: VerifyContext
    ) -> dict[str, Any]:
        sql = str(check["sql"])
        label = str(check.get("name") or sql[:60])
        result = docker.exec(
            container,
            ["psql", "-U", self.db_user, "-d", self.db_name, "-tAc", sql],
            timeout=min(float(check.get("timeout", 120)), ctx.timeout),
        )
        if not result.ok:
            return {"name": label, "value": None, "problem": f"{label}: {result.tail(4)}"}

        value = result.stdout.strip().splitlines()[0].strip() if result.stdout.strip() else ""
        return {
            "name": label,
            "value": value,
            "problem": _compare(label, value, check),
        }

    def _count_tables(self, docker: Docker, container: str, ctx: VerifyContext) -> int:
        outcome = self._query(
            docker,
            container,
            {
                "sql": "SELECT count(*) FROM information_schema.tables "
                "WHERE table_schema NOT IN ('pg_catalog', 'information_schema')",
                "name": "table count",
            },
            ctx,
        )
        try:
            return int(outcome["value"])
        except (TypeError, ValueError):
            return 0


def _compare(label: str, value: Any, check: dict[str, Any]) -> str | None:
    if "expect_min" in check or "expect_max" in check:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return f"{label}: expected a number, got {value!r}"
        if check.get("expect_min") is not None and numeric < float(check["expect_min"]):
            return f"{label}: {value} < expected minimum {check['expect_min']}"
        if check.get("expect_max") is not None and numeric > float(check["expect_max"]):
            return f"{label}: {value} > expected maximum {check['expect_max']}"
    if "expect_equals" in check and str(value) != str(check["expect_equals"]):
        return f"{label}: got {value!r}, expected {check['expect_equals']!r}"
    if "expect_contains" in check and str(check["expect_contains"]) not in str(value):
        return f"{label}: {value!r} does not contain {check['expect_contains']!r}"
    return None


def _quote(value: str) -> str:
    return "'" + str(value).replace("'", "'\\''") + "'"
