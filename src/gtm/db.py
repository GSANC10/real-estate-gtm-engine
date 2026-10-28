"""Database connections and tenant-scoped transactions.

Every tenant-protected read or write runs inside ``tenant_tx``, which sets ``app.tenant_id``
transaction-locally. Row level security returns nothing when the scope is missing, and the
setting never carries over to the next transaction on a pooled or reused connection.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

Conn = psycopg.Connection[dict[str, Any]]

DSN_ENV = {
    "worker": "GTM_WORKER_DATABASE_URL",
    "app": "GTM_APP_DATABASE_URL",
    "trigger": "GTM_TRIGGER_DATABASE_URL",
}


def connect(dsn: str) -> Conn:
    return psycopg.connect(dsn, autocommit=True, row_factory=dict_row)


def dsn_from_env(role: str) -> str:
    name = DSN_ENV[role]
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"{name} is not set")
    return value


@contextmanager
def tenant_tx(conn: Conn, tenant_id: UUID) -> Iterator[Conn]:
    with conn.transaction():
        conn.execute("SELECT set_config('app.tenant_id', %s, true)", (str(tenant_id),))
        yield conn


def one(conn: Conn, query: str, params: tuple[Any, ...] | dict[str, Any] = ()) -> dict[str, Any]:
    row = conn.execute(query, params).fetchone()
    if row is None:
        raise LookupError("expected one row, found none")
    return row


def maybe_one(
    conn: Conn, query: str, params: tuple[Any, ...] | dict[str, Any] = ()
) -> dict[str, Any] | None:
    return conn.execute(query, params).fetchone()


def all_rows(
    conn: Conn, query: str, params: tuple[Any, ...] | dict[str, Any] = ()
) -> list[dict[str, Any]]:
    return conn.execute(query, params).fetchall()
