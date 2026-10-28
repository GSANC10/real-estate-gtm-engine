from __future__ import annotations

import csv
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import yaml

from gtm.config import AppliedConfig, BrokerageConfig, apply_config
from gtm.db import Conn, connect
from gtm.devdb import (
    Cluster,
    RoleDsns,
    clone_database,
    create_migrated_database,
    drop_database,
    role_dsns,
    start_cluster,
)
from gtm.faults import Faults
from gtm.operator import EnqueueResult, enqueue_run
from gtm.pipeline.importing import REQUIRED_COLUMNS, ImportSummary, import_contacts
from gtm.providers.base import Providers
from gtm.providers.fake import FakeModel, FakeResearchTools
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


@pytest.fixture(scope="session")
def cluster() -> Iterator[tuple[Cluster, str]]:
    c = start_cluster()
    template = f"gtm_tmpl_{uuid.uuid4().hex[:8]}"
    create_migrated_database(c, template)
    yield c, template
    drop_database(c, template)
    c.stop()


@pytest.fixture
def db(cluster: tuple[Cluster, str]) -> Iterator[RoleDsns]:
    c, template = cluster
    name = f"gtm_t_{uuid.uuid4().hex[:10]}"
    clone_database(c, template, name)
    yield role_dsns(c, name)
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
    dsns: RoleDsns
    tmp: Path
    app: Conn
    trigger: Conn
    admin: Conn  # superuser: test setup and assertions only, never evidence of isolation
    model: FakeModel = field(default_factory=FakeModel)
    tools: FakeResearchTools = field(default_factory=FakeResearchTools)

    @property
    def providers(self) -> Providers:
        return Providers(self.model, self.tools)

    def brokerage(
        self, name: str = "example-homes", overrides: dict[str, Any] | None = None
    ) -> AppliedConfig:
        data = yaml.safe_load((EXAMPLES / f"{name}.yaml").read_text())
        return apply_config(self.app, BrokerageConfig.model_validate(_merge(data, overrides or {})))

    def import_rows(
        self, applied: AppliedConfig, rows: list[dict[str, str]], label: str = "test"
    ) -> ImportSummary:
        path = self.tmp / f"contacts-{uuid.uuid4().hex[:6]}.csv"
        with path.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=sorted(REQUIRED_COLUMNS))
            writer.writeheader()
            writer.writerows(rows)
        return import_contacts(self.app, applied.tenant_id, applied.campaign_id, path, label)

    def import_example(self, applied: AppliedConfig, name: str = "example-homes") -> ImportSummary:
        return import_contacts(
            self.app,
            applied.tenant_id,
            applied.campaign_id,
            EXAMPLES / f"contacts-{name}.csv",
            name,
        )

    def enqueue(
        self,
        applied: AppliedConfig,
        imported: ImportSummary,
        trigger_id: str = "t1",
        rerun: int = 0,
    ) -> EnqueueResult:
        return enqueue_run(
            self.trigger,
            applied.tenant_id,
            applied.campaign_id,
            imported.import_id,
            trigger_id,
            rerun,
        )

    def worker(self, faults: Faults | None = None, **kwargs: Any) -> Worker:
        return Worker(
            self.dsns.worker,
            self.providers,
            today=lambda: TODAY,
            faults=faults or Faults(),
            **kwargs,
        )

    def rows(self, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        return self.admin.execute(query, params).fetchall()  # type: ignore[arg-type]

    def scalar(self, query: str, params: tuple[Any, ...] = ()) -> Any:
        row = self.admin.execute(query, params).fetchone()  # type: ignore[arg-type]
        return None if row is None else next(iter(row.values()))

    def lead_id(self, tenant_id: UUID, email: str) -> UUID:
        value: UUID = self.scalar(
            "SELECT id FROM gtm.leads WHERE tenant_id = %s AND identity_key = %s",
            (tenant_id, email_identity_key(email)),
        )
        return value

    def lead_state(self, tenant_id: UUID, email: str) -> tuple[str, str | None]:
        row = self.rows(
            "SELECT state, state_reason FROM gtm.campaign_leads WHERE tenant_id = %s AND lead_id = %s",
            (tenant_id, self.lead_id(tenant_id, email)),
        )[0]
        return row["state"], row["state_reason"]

    def expire_leases(self) -> None:
        self.admin.execute(
            "UPDATE gtm.jobs SET lease_expires_at = now() - interval '1 second' WHERE status = 'running'"
        )


@pytest.fixture
def env(db: RoleDsns, tmp_path: Path) -> Iterator[Env]:
    e = Env(db, tmp_path, connect(db.app), connect(db.trigger), connect(db.admin))
    yield e
    for conn in (e.app, e.trigger, e.admin):
        conn.close()


@pytest.fixture
def jordan_run(env: Env) -> tuple[Env, AppliedConfig, EnqueueResult]:
    """Example Homes with the single fictional lead Jordan, enqueued and not yet processed."""
    applied = env.brokerage()
    imported = env.import_rows(applied, [JORDAN])
    return env, applied, env.enqueue(applied, imported)
