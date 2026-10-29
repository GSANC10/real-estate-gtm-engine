"""Database connection helpers. One login, from GTM_DATABASE_URL."""

from __future__ import annotations

import os
from typing import Any

import psycopg
from psycopg.rows import dict_row

Conn = psycopg.Connection[dict[str, Any]]
Params = tuple[Any, ...] | dict[str, Any]

DSN_ENV = "GTM_DATABASE_URL"


def connect(dsn: str) -> Conn:
    return psycopg.connect(dsn, autocommit=True, row_factory=dict_row)


def dsn_from_env() -> str:
    value = os.environ.get(DSN_ENV)
    if not value:
        raise SystemExit(f"{DSN_ENV} is not set")
    return value


def one(conn: Conn, query: str, params: Params = ()) -> dict[str, Any]:
    row = conn.execute(query, params).fetchone()
    if row is None:
        raise LookupError("expected one row, found none")
    return row


def maybe_one(conn: Conn, query: str, params: Params = ()) -> dict[str, Any] | None:
    return conn.execute(query, params).fetchone()


def all_rows(conn: Conn, query: str, params: Params = ()) -> list[dict[str, Any]]:
    return conn.execute(query, params).fetchall()
