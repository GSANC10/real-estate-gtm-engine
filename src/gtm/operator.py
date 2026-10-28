"""Operator and trigger actions: enqueue a run, suppress a contact, pause or resume a campaign."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from gtm.db import Conn, one, tenant_tx
from gtm.util import email_identity_key


@dataclass(frozen=True)
class EnqueueResult:
    run_id: UUID
    created: bool
    leads_enqueued: int
    jobs_created: int
    jobs_reused: int
    leads_skipped: int


def enqueue_run(
    conn: Conn, tenant_id: UUID, campaign_id: UUID, import_id: UUID, trigger_id: str, rerun: int = 0
) -> EnqueueResult:
    """Runs as gtm_trigger (or gtm_app) through the hardened ``gtm.enqueue_run`` function."""
    row = one(
        conn,
        "SELECT * FROM gtm.enqueue_run(%s, %s, %s, %s, %s)",
        (tenant_id, campaign_id, import_id, trigger_id, rerun),
    )
    return EnqueueResult(
        row["run_id"],
        row["created"],
        row["leads_enqueued"],
        row["jobs_created"],
        row["jobs_reused"],
        row["leads_skipped"],
    )


def run_status(conn: Conn, tenant_id: UUID, run_id: UUID) -> dict[str, Any]:
    return one(conn, "SELECT * FROM gtm.run_status(%s, %s)", (tenant_id, run_id))


def suppress_email(
    conn: Conn, tenant_id: UUID, email: str, reason: str = "operator_request"
) -> None:
    with tenant_tx(conn, tenant_id):
        conn.execute(
            """INSERT INTO gtm.suppressions (tenant_id, kind, value_hash, reason) VALUES (%s, 'email', %s, %s)
               ON CONFLICT (tenant_id, kind, value_hash) DO NOTHING""",
            (tenant_id, email_identity_key(email), reason),
        )


def set_campaign_status(conn: Conn, tenant_id: UUID, campaign_id: UUID, status: str) -> int:
    """Pause or resume. Resuming re-queues jobs that the pause had blocked; returns that count."""
    with tenant_tx(conn, tenant_id):
        conn.execute(
            "UPDATE gtm.campaigns SET status = %s, updated_at = now() WHERE id = %s",
            (status, campaign_id),
        )
        if status != "active":
            return 0
        rows = conn.execute(
            """UPDATE gtm.jobs SET status = 'queued', error_code = NULL, available_at = now(), updated_at = now()
                WHERE campaign_id = %s AND status = 'blocked' AND error_code = 'campaign_paused'
            RETURNING id""",
            (campaign_id,),
        ).fetchall()
    return len(rows)
