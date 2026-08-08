"""Load a restored dump into a throwaway MySQL/MariaDB container and query it.

Same idea as the postgres verifier: a `mysqldump` that exists and gunzips
cleanly can still be a dump that died halfway through, or one taken without
--single-transaction that caught the tables mid-write.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from ..config import ConfigError
from ..docker import Docker, DockerError
from ..util import human_duration
from . import VerifyContext, VerifyError, VerifyResult, Verifier, register

_PLAIN_SUFFIXES = (".sql", ".dump")
_DECOMPRESSORS = {
    ".gz": "gunzip -c",
    ".bz2": "bunzip2 -c",
    ".xz": "xz -dc",
    ".zst": "zstd -dc",
}


@register
class MysqlVerifier(Verifier):
    type = "mysql"

    def validate(self) -> None:
        self._required("dump")
        for position, check in enumerate(self.spec.get("checks") or []):
            if not isinstance(check, dict) or not check.get("sql"):
                raise ConfigError(
                    f"job {self.job.name!r}: verify.mysql.checks[{position}] needs 'sql'"
                )

    @property
    def image(self) -> str:
        return str(self.spec.get("image", "mariadb:11"))

    @property
    def db_name(self) -> str:
        return str(self.spec.get("database", "verify"))

    @property
    def root_password(self) -> str:
        return str(self.spec.get("password", "verify"))

    def run(self, ctx: VerifyContext) -> VerifyResult:
        started = time.monotonic()
        docker = Docker(str(self.spec.get("docker_binary", "docker")), ctx.log)
        try:
            docker.require()
        except DockerError as exc:
            raise VerifyError(str(exc)) from exc

        dump_path = self._find_dump(ctx)
        in_container = "/restore/" + str(dump_path.relative_to(ctx.restore_dir))

        # MariaDB and MySQL images share these variable names.
        env = {
            "MARIADB_ROOT_PASSWORD": self.root_password,
            "MYSQL_ROOT_PASSWORD": self.root_password,
            "MARIADB_DATABASE": self.db_name,
            "MYSQL_DATABASE": self.db_name,
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
                # Durability is pointless for a cluster we throw away.
                command=["--skip-innodb-doublewrite", "--innodb-flush-log-at-trx-commit=0"],
                timeout=min(600, ctx.timeout),
            ) as container:
                ready_timeout = min(float(self.spec.get("ready_timeout", 180)), ctx.timeout)
                docker.wait_healthy(
                    container,
                    ["sh", "-c", self._client("-e 'SELECT 1'")],
                    timeout=ready_timeout,
                )
                details["startup_seconds"] = round(time.monotonic() - started, 1)

                details["load_seconds"] = round(
                    self._load(docker, container, in_container, ctx), 1
                )

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

                if self.spec.get("check_tables", False):
                    broken = self._check_tables(docker, container, ctx)
                    details["corrupt_tables"] = broken
                    if broken:
                        problems.append(f"CHECK TABLE reported problems: {', '.join(broken[:5])}")
        except DockerError as exc:
            raise VerifyError(f"mysql verification could not run: {exc}") from exc

        headline = f"{dump_path.name} loaded in {human_duration(details.get('load_seconds', 0))}"
        summary = f"{headline}; " + ("; ".join(problems) if problems else "all checks passed")
        result = self.failed(summary, **details) if problems else self.ok(summary, **details)
        result.duration = time.monotonic() - started
        return result

    # -- steps ---------------------------------------------------------

    def _client(self, args: str) -> str:
        """Build a client invocation.

        MariaDB 11 ships `mariadb`, MySQL ships `mysql`, and several images
        provide only one of the two — so pick whichever exists at runtime.
        """
        credentials = f"-u root -p{_shell_quote(self.root_password)} {_shell_quote(self.db_name)}"
        return (
            f"if command -v mariadb >/dev/null 2>&1; then CLIENT=mariadb; else CLIENT=mysql; fi; "
            f"$CLIENT {credentials} {args}"
        )

    def _find_dump(self, ctx: VerifyContext) -> Path:
        matches = [m for m in sorted(ctx.restore_dir.glob(str(self.spec["dump"]))) if m.is_file()]
        if not matches:
            raise VerifyError(
                f"no dump matching {self.spec['dump']!r} in the restore — "
                "the backup did not contain what the config expects"
            )
        return max(matches, key=lambda p: p.stat().st_mtime)

    def _load(self, docker: Docker, container: str, dump: str, ctx: VerifyContext) -> float:
        started = time.monotonic()
        timeout = min(float(self.spec.get("load_timeout", ctx.timeout)), ctx.timeout)
        suffix = Path(dump).suffix.lower()

        if suffix in _DECOMPRESSORS:
            feed = f"{_DECOMPRESSORS[suffix]} {_shell_quote(dump)}"
        elif suffix in _PLAIN_SUFFIXES:
            feed = f"cat {_shell_quote(dump)}"
        else:
            raise VerifyError(
                f"{Path(dump).name}: unsupported dump format for mysql "
                "(expected .sql or .sql.gz/.bz2/.xz/.zst)"
            )

        result = docker.exec(
            container, ["sh", "-c", f"{feed} | " + self._client("")], timeout=timeout
        )
        if not result.ok:
            detail = "timed out" if result.timed_out else result.tail(15)
            raise VerifyError(f"loading {Path(dump).name} into mysql failed:\n{detail}")
        return time.monotonic() - started

    def _query(
        self, docker: Docker, container: str, check: dict[str, Any], ctx: VerifyContext
    ) -> dict[str, Any]:
        sql = str(check["sql"])
        label = str(check.get("name") or sql[:60])
        result = docker.exec(
            container,
            ["sh", "-c", self._client(f"-N -B -e {_shell_quote(sql)}")],
            timeout=min(float(check.get("timeout", 120)), ctx.timeout),
        )
        if not result.ok:
            return {"name": label, "value": None, "problem": f"{label}: {result.tail(4)}"}

        lines = [line for line in result.stdout.strip().splitlines() if line.strip()]
        value = lines[0].split("\t")[0].strip() if lines else ""
        return {"name": label, "value": value, "problem": _compare(label, value, check)}

    def _count_tables(self, docker: Docker, container: str, ctx: VerifyContext) -> int:
        outcome = self._query(
            docker,
            container,
            {
                "sql": "SELECT count(*) FROM information_schema.tables "
                f"WHERE table_schema = '{self.db_name}'",
                "name": "table count",
            },
            ctx,
        )
        try:
            return int(outcome["value"])
        except (TypeError, ValueError):
            return 0

    def _check_tables(self, docker: Docker, container: str, ctx: VerifyContext) -> list[str]:
        """Run CHECK TABLE over every table; return the ones that are not OK."""
        listing = self._query(
            docker,
            container,
            {
                "sql": "SELECT table_name FROM information_schema.tables "
                f"WHERE table_schema = '{self.db_name}' AND table_type = 'BASE TABLE'",
                "name": "table list",
            },
            ctx,
        )
        if listing["value"] in (None, ""):
            return []

        result = docker.exec(
            container,
            [
                "sh",
                "-c",
                self._client(
                    "-N -B -e "
                    + _shell_quote(
                        "SELECT GROUP_CONCAT(CONCAT('`', table_name, '`')) "
                        "FROM information_schema.tables "
                        f"WHERE table_schema = '{self.db_name}' AND table_type = 'BASE TABLE'"
                    )
                ),
            ],
            timeout=min(120, ctx.timeout),
        )
        tables = result.stdout.strip() if result.ok else ""
        if not tables or tables == "NULL":
            return []

        checked = docker.exec(
            container,
            ["sh", "-c", self._client(f"-N -B -e {_shell_quote(f'CHECK TABLE {tables}')}")],
            timeout=min(float(self.spec.get("check_timeout", 600)), ctx.timeout),
        )
        broken = []
        for line in checked.stdout.splitlines():
            columns = line.split("\t")
            # Table  Op  Msg_type  Msg_text
            if len(columns) >= 4 and columns[2].strip() == "status" and columns[3].strip() != "OK":
                broken.append(f"{columns[0].strip()} ({columns[3].strip()})")
        return broken


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


def _shell_quote(value: str) -> str:
    return "'" + str(value).replace("'", "'\\''") + "'"
