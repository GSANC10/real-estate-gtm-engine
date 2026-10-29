"""Throwaway PostgreSQL databases for tests and the fictional demo.

Local runs start a private PostgreSQL 17 cluster (initdb in a temporary directory, on a free
loopback port), so tests never touch a developer's running server. CI passes
GTM_TEST_ADMIN_DSN to use its PostgreSQL 17 service container instead.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = REPO_ROOT / "supabase" / "migrations"
PG_MAJOR = 17

_PG_BIN_CANDIDATES = (
    "/opt/homebrew/opt/postgresql@17/bin",
    "/usr/local/opt/postgresql@17/bin",
    "/usr/lib/postgresql/17/bin",
)


class DevDatabaseError(RuntimeError):
    pass


def find_pg_bin() -> Path:
    configured = os.environ.get("GTM_PG_BIN")
    candidates = [configured] if configured else list(_PG_BIN_CANDIDATES)
    for candidate in candidates:
        if candidate and (Path(candidate) / "initdb").exists():
            return Path(candidate)
    raise DevDatabaseError(
        f"PostgreSQL {PG_MAJOR} binaries not found. Install postgresql@{PG_MAJOR} or set GTM_PG_BIN."
    )


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@dataclass
class Cluster:
    """A running throwaway cluster, or an external server given by an admin DSN."""

    admin_dsn: str
    _datadir: Path | None = None
    _pg_bin: Path | None = None
    _tmp: Path | None = None

    def dsn(self, dbname: str) -> str:
        params = conninfo_to_dict(self.admin_dsn)
        params["dbname"] = dbname
        return make_conninfo("", **params)

    def stop(self) -> None:
        if self._datadir is not None and self._pg_bin is not None:
            subprocess.run(
                [str(self._pg_bin / "pg_ctl"), "-D", str(self._datadir), "-m", "immediate", "stop"],
                check=False,
                capture_output=True,
            )
        if self._tmp is not None:
            shutil.rmtree(self._tmp, ignore_errors=True)


def start_cluster() -> Cluster:
    external = os.environ.get("GTM_TEST_ADMIN_DSN")
    if external:
        return Cluster(admin_dsn=external)

    pg_bin = find_pg_bin()
    tmp = Path(tempfile.mkdtemp(prefix="gtm-pg-"))
    datadir = tmp / "data"
    port = _free_port()
    subprocess.run(
        [
            str(pg_bin / "initdb"),
            "-D",
            str(datadir),
            "-U",
            "postgres",
            "-A",
            "trust",
            "-E",
            "UTF8",
            "--locale=C",
            "--no-sync",
        ],
        check=True,
        capture_output=True,
    )
    options = (
        f"-p {port} -c listen_addresses=127.0.0.1 -k {tmp} "
        "-c fsync=off -c synchronous_commit=off -c full_page_writes=off"
    )
    subprocess.run(
        [
            str(pg_bin / "pg_ctl"),
            "-D",
            str(datadir),
            "-o",
            options,
            "-l",
            str(tmp / "server.log"),
            "-w",
            "start",
        ],
        check=True,
        capture_output=True,
    )
    admin = make_conninfo(host="127.0.0.1", port=port, user="postgres", dbname="postgres")
    return Cluster(admin_dsn=admin, _datadir=datadir, _pg_bin=pg_bin, _tmp=tmp)


def apply_migrations(dsn: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            with conn.transaction():
                conn.execute(path.read_text())


def _admin(cluster: Cluster, statement: sql.Composed) -> None:
    with psycopg.connect(cluster.admin_dsn, autocommit=True) as admin:
        admin.execute(statement)


def create_database(cluster: Cluster, dbname: str, template: str | None = None) -> str:
    query = sql.SQL("CREATE DATABASE {}").format(sql.Identifier(dbname))
    if template:
        query = sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(
            sql.Identifier(dbname), sql.Identifier(template)
        )
    _admin(cluster, query)
    return cluster.dsn(dbname)


def drop_database(cluster: Cluster, dbname: str) -> None:
    _admin(
        cluster, sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(dbname))
    )


@contextmanager
def throwaway_database() -> Iterator[str]:
    """A fresh migrated database on a throwaway cluster, removed on exit. Yields its DSN."""
    cluster = start_cluster()
    dbname = f"gtm_demo_{uuid.uuid4().hex[:8]}"
    try:
        dsn = create_database(cluster, dbname)
        apply_migrations(dsn)
        yield dsn
    finally:
        try:
            drop_database(cluster, dbname)
        finally:
            cluster.stop()
