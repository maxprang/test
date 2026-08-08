"""Load a restored dump into a throwaway MySQL/MariaDB container and query it.

Same idea as the postgres verifier: a `mysqldump` that exists and gunzips
cleanly can still be a dump that died halfway through, or one taken without
--single-transaction that caught the tables mid-write.
"""

from __future__ import annotations

from pathlib import Path

from ..docker import Docker
from ..util import shell_quote
from . import VerifyContext, VerifyError, register
from .container_db import (
    DECOMPRESSORS,
    PLAIN_SUFFIXES,
    ContainerDbVerifier,
    decompression_pipeline,
)


@register
class MysqlVerifier(ContainerDbVerifier):
    type = "mysql"
    default_image = "mariadb:11"

    def container_env(self) -> dict[str, str]:
        # MariaDB and MySQL images read different variable names; set both.
        return {
            "MARIADB_ROOT_PASSWORD": self.password,
            "MYSQL_ROOT_PASSWORD": self.password,
            "MARIADB_DATABASE": self.database,
            "MYSQL_DATABASE": self.database,
        }

    def container_command(self) -> list[str] | None:
        # Durability is pointless for a cluster we throw away.
        return ["--skip-innodb-doublewrite", "--innodb-flush-log-at-trx-commit=0"]

    def readiness_probe(self) -> list[str]:
        return ["sh", "-c", self._client("-e 'SELECT 1'")]

    def load_command(self, dump: str) -> list[str]:
        suffix = Path(dump).suffix.lower()
        if suffix not in DECOMPRESSORS and suffix not in PLAIN_SUFFIXES and suffix != ".dump":
            raise VerifyError(
                f"{Path(dump).name}: unsupported dump format for mysql "
                "(expected .sql or .sql.gz/.bz2/.xz/.zst)"
            )
        return ["sh", "-c", f"{decompression_pipeline(dump)} | " + self._client("")]

    def query_command(self, sql: str) -> list[str]:
        return ["sh", "-c", self._client(f"-N -B -e {shell_quote(sql)}")]

    def count_tables_sql(self) -> str:
        return (
            "SELECT count(*) FROM information_schema.tables "
            f"WHERE table_schema = '{self.database}'"
        )

    def extra_problems(self, docker: Docker, container: str, ctx: VerifyContext) -> list[str]:
        if not self.spec.get("check_tables", False):
            return []
        broken = self._check_tables(docker, container, ctx)
        if broken:
            return [f"CHECK TABLE reported problems: {', '.join(broken[:5])}"]
        return []

    # -- engine specifics ----------------------------------------------

    def _client(self, args: str) -> str:
        """Build a client invocation.

        MariaDB 11 ships `mariadb`, MySQL ships `mysql`, and several images
        provide only one of the two — so pick whichever exists at runtime.
        """
        credentials = (
            f"-u root -p{shell_quote(self.password)} {shell_quote(self.database)}"
        )
        return (
            "if command -v mariadb >/dev/null 2>&1; then CLIENT=mariadb; else CLIENT=mysql; fi; "
            f"$CLIENT {credentials} {args}"
        )

    def _check_tables(self, docker: Docker, container: str, ctx: VerifyContext) -> list[str]:
        """Run CHECK TABLE over every table; return the ones that are not OK."""
        listing = docker.exec(
            container,
            self.query_command(
                "SELECT GROUP_CONCAT(CONCAT('`', table_name, '`')) "
                "FROM information_schema.tables "
                f"WHERE table_schema = '{self.database}' AND table_type = 'BASE TABLE'"
            ),
            timeout=min(120, ctx.timeout),
        )
        tables = listing.stdout.strip() if listing.ok else ""
        if not tables or tables == "NULL":
            return []

        checked = docker.exec(
            container,
            self.query_command(f"CHECK TABLE {tables}"),
            timeout=min(float(self.spec.get("check_timeout", 600)), ctx.timeout),
        )
        broken = []
        for line in checked.stdout.splitlines():
            columns = line.split("\t")
            # Table  Op  Msg_type  Msg_text
            if len(columns) >= 4 and columns[2].strip() == "status" and columns[3].strip() != "OK":
                broken.append(f"{columns[0].strip()} ({columns[3].strip()})")
        return broken
