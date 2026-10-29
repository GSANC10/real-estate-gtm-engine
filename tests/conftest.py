from __future__ import annotations

import csv
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import yaml

from gtm.config import BrokerageConfig, ResolvedConfig, resolve
from gtm.db import Conn, connect
from gtm.devdb import Cluster, apply_migrations, create_database, drop_database, start_cluster
from gtm.pipeline.importing import REQUIRED_COLUMNS, ImportSummary, import_contacts
from gtm.providers.base import Providers
from gtm.providers.fake import FakeModel, FakeResearchTools
from gtm.runs import RunStart, start_run
from gtm.util import email_identity_key
from gtm.worker import Worker

EXAMPLES = Path(__file__).resolve().parents[1] / "config" / "examples"
TODAY = date(2026, 10, 27)

JORDAN = {
    "email": "jordan.lee@mail.example",
    "full_name": "Jordan Lee",
    "organization": "Cedar Lane Studio",
    "residence_location": "Seattle, WA",
    "language_preference": "",
    "need_category": "buy_primary_residence",
    "need_note": "I am looking for a three-bedroom home in Torrance. Can someone help with my search?",
    "property_location": "Torrance, CA",
    "recorded_at": "2026-10-20",
    "source": "website inquiry",
    "may_mention_inquiry": "true",
}


class Interrupted(BaseException):
    """Stands in for the program being killed: pipeline code cannot catch it."""


@pytest.fixture(scope="session")
def cluster() -> Iterator[tuple[Cluster, str]]:
    c = start_cluster()
    template = f"gtm_tmpl_{uuid.uuid4().hex[:8]}"
    apply_migrations(create_database(c, template))
    yield c, template
    drop_database(c, template)
    c.stop()


@pytest.fixture
def dsn(cluster: tuple[Cluster, str]) -> Iterator[str]:
    c, template = cluster
    name = f"gtm_t_{uuid.uuid4().hex[:10]}"
    yield create_database(c, name, template=template)
    drop_database(c, name)


def _merge(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in overrides.items():
        out[key] = (
            _merge(out[key], value)
            if isinstance(value, dict) and isinstance(out.get(key), dict)
            else value
        )
    return out


@dataclass
class Env:
    dsn: str
    tmp: Path
    conn: Conn
    model: FakeModel = field(default_factory=FakeModel)
    tools: FakeResearchTools = field(default_factory=FakeResearchTools)

    @property
    def providers(self) -> Providers:
        return Providers(self.model, self.tools)

    def config(self, overrides: dict[str, Any] | None = None) -> ResolvedConfig:
        data = yaml.safe_load((EXAMPLES / "example-homes.yaml").read_text())
        return resolve(BrokerageConfig.model_validate(_merge(data, overrides or {})))

    def import_rows(self, rows: list[dict[str, str]]) -> ImportSummary:
        path = self.tmp / f"contacts-{uuid.uuid4().hex[:6]}.csv"
        with path.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=sorted(REQUIRED_COLUMNS))
            writer.writeheader()
            writer.writerows(rows)
        return import_contacts(self.conn, path, "test")

    def import_example(self) -> ImportSummary:
        return import_contacts(self.conn, EXAMPLES / "contacts-example-homes.csv", "example")

    def start(self, overrides: dict[str, Any] | None = None, rerun: int = 0) -> RunStart:
        return start_run(self.conn, self.config(overrides), rerun)

    def worker(self) -> Worker:
        return Worker(self.dsn, self.providers, today=lambda: TODAY)

    def work(self, max_jobs: int | None = None) -> list[Any]:
        w = self.worker()
        try:
            return w.run(max_jobs)
        finally:
            w.close()

    def rows(self, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        return self.conn.execute(query, params).fetchall()

    def scalar(self, query: str, params: tuple[Any, ...] = ()) -> Any:
        row = self.conn.execute(query, params).fetchone()
        return None if row is None else next(iter(row.values()))

    def lead_id(self, email: str) -> Any:
        return self.scalar(
            "SELECT id FROM gtm.leads WHERE identity_key = %s", (email_identity_key(email),)
        )

    def lead_state(self, email: str) -> tuple[str, str | None]:
        row = self.rows(
            "SELECT state, state_reason FROM gtm.leads WHERE id = %s", (self.lead_id(email),)
        )[0]
        return row["state"], row["state_reason"]


@pytest.fixture
def env(dsn: str, tmp_path: Path) -> Iterator[Env]:
    e = Env(dsn, tmp_path, connect(dsn))
    yield e
    e.conn.close()


@pytest.fixture
def jordan_run(env: Env) -> Env:
    """The single fictional lead Jordan, imported and queued, not yet processed."""
    env.import_rows([JORDAN])
    env.start()
    return env
