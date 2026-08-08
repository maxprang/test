"""Load a restored dump into a throwaway PostgreSQL container and query it.

This is the check that actually answers "could I bring the service back?".
A dump file that exists and even gunzips cleanly can still be a truncated
transaction or an empty schema; only replaying it into a real server proves it.
"""

from __future__ import annotations

from pathlib import Path

from ..util import shell_quote
from . import VerifyError, register
from .container_db import (
    DECOMPRESSORS,
    PLAIN_SUFFIXES,
    ContainerDbVerifier,
    decompression_pipeline,
)

_CUSTOM_SUFFIXES = (".dump", ".custom", ".pgdump", ".backup")


@register
class PostgresVerifier(ContainerDbVerifier):
    type = "postgres"
    default_image = "postgres:16-alpine"

    @property
    def db_user(self) -> str:
        return str(self.spec.get("user", "verify"))

    def container_env(self) -> dict[str, str]:
        return {
            "POSTGRES_USER": self.db_user,
            "POSTGRES_PASSWORD": self.password,
            "POSTGRES_DB": self.database,
            # We throw the cluster away; durability is wasted time here.
            "PGOPTIONS": "-c fsync=off -c full_page_writes=off -c synchronous_commit=off",
        }

    def readiness_probe(self) -> list[str]:
        return ["pg_isready", "-U", self.db_user, "-d", self.database]

    def load_command(self, dump: str) -> list[str]:
        suffix = Path(dump).suffix.lower()
        credentials = (
            f"PGUSER={shell_quote(self.db_user)} PGDATABASE={shell_quote(self.database)} "
        )

        if suffix in DECOMPRESSORS:
            inner = Path(dump).with_suffix("").suffix.lower()
            if inner in _CUSTOM_SUFFIXES:
                raise VerifyError(
                    f"{Path(dump).name}: compressed custom-format dumps are not supported "
                    "(pg_dump -Fc is already compressed); point 'dump' at a plain .sql[.gz] "
                    "or at the uncompressed .dump"
                )
            script = f"{decompression_pipeline(dump)} | psql -v ON_ERROR_STOP=1 -q"
        elif suffix in PLAIN_SUFFIXES:
            script = f"psql -v ON_ERROR_STOP=1 -q -f {shell_quote(dump)}"
        else:
            # pg_dump custom/directory format
            script = (
                "pg_restore --exit-on-error --no-owner --no-privileges "
                f"-d {shell_quote(self.database)} {shell_quote(dump)}"
            )
        return ["sh", "-c", credentials + script]

    def query_command(self, sql: str) -> list[str]:
        return ["psql", "-U", self.db_user, "-d", self.database, "-tAc", sql]

    def count_tables_sql(self) -> str:
        return (
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema NOT IN ('pg_catalog', 'information_schema')"
        )
