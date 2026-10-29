"""Starting a run, the do-not-contact list, and lead outcomes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from gtm.config import ResolvedConfig
from gtm.db import Conn, all_rows, one
from gtm.util import email_identity_key, json_sha256


@dataclass(frozen=True)
class RunStart:
    run_id: UUID
    jobs_created: int
    jobs_reused: int
    suppressed: int


def start_run(conn: Conn, config: ResolvedConfig, rerun: int = 0) -> RunStart:
    """Save the config on a new run and queue research for each lead's latest record.

    Research is keyed by record and rerun number, so leads that were already researched reuse
    that work. Pass a higher ``rerun`` to research everyone again (for example after a config change).
    """
    content = config.model_dump(mode="json")
    with conn.transaction():
        run_id: UUID = one(
            conn,
            "INSERT INTO gtm.runs (config, config_sha256, budget_cap_usd) VALUES (%s, %s, %s) RETURNING id",
            (Jsonb(content), json_sha256(content), config.campaign.budget_cap_usd),
        )["id"]
        latest = all_rows(
            conn,
            """SELECT * FROM (
                 SELECT DISTINCT ON (r.lead_id) r.lead_id, r.id AS record_id, r.content->>'full_name' AS name,
                        EXISTS (SELECT 1 FROM gtm.suppressions s WHERE s.value_hash = l.identity_key) AS suppressed
                   FROM gtm.lead_records r JOIN gtm.leads l ON l.id = r.lead_id
                  ORDER BY r.lead_id, r.version DESC) latest
               ORDER BY name, lead_id""",
        )
        created = reused = suppressed = 0
        for row in latest:
            if row["suppressed"]:
                conn.execute(
                    "UPDATE gtm.leads SET state = 'suppressed', state_reason = 'suppressed' WHERE id = %s",
                    (row["lead_id"],),
                )
                suppressed += 1
                continue
            inserted = conn.execute(
                """INSERT INTO gtm.jobs (run_id, lead_id, lead_record_id, stage, job_key, rerun)
                   VALUES (%s, %s, %s, 'research', %s, %s) ON CONFLICT (job_key) DO NOTHING RETURNING id""",
                (
                    run_id,
                    row["lead_id"],
                    row["record_id"],
                    f"research/{row['record_id']}/r{rerun}",
                    rerun,
                ),
            ).fetchone()
            if inserted:
                created += 1
                conn.execute(
                    "UPDATE gtm.leads SET state = 'in_progress', state_reason = NULL WHERE id = %s",
                    (row["lead_id"],),
                )
            else:
                reused += 1
    return RunStart(run_id, created, reused, suppressed)


def suppress_email(conn: Conn, email: str, reason: str = "operator_request") -> None:
    conn.execute(
        "INSERT INTO gtm.suppressions (value_hash, reason) VALUES (%s, %s) ON CONFLICT DO NOTHING",
        (email_identity_key(email), reason),
    )


def lead_outcomes(conn: Conn) -> list[dict[str, Any]]:
    return all_rows(
        conn,
        """SELECT l.id, r.content->>'full_name' AS name, l.state, l.state_reason
             FROM gtm.leads l
             JOIN LATERAL (SELECT content FROM gtm.lead_records WHERE lead_id = l.id
                            ORDER BY version DESC LIMIT 1) r ON true
            ORDER BY r.content->>'full_name', l.id""",
    )
