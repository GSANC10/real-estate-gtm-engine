"""Company isolation, tested as the actual runtime roles (never as a superuser or bypass role)."""

from __future__ import annotations

import psycopg
import pytest

from gtm.db import connect, tenant_tx
from gtm.operator import enqueue_run, run_status
from gtm.worker import Worker
from tests.conftest import Env

TENANT_TABLES = (
    "tenants",
    "senders",
    "mailboxes",
    "campaigns",
    "config_versions",
    "leads",
    "lead_records",
    "contact_imports",
    "import_rows",
    "campaign_leads",
    "suppressions",
    "runs",
    "jobs",
    "run_leads",
    "context_snapshots",
    "provider_calls",
    "stage_results",
    "citations",
    "citation_checks",
    "qualification_decisions",
    "outreach_items",
)
PRIVILEGED = {
    "claim_job": "gtm_claim_owner",
    "enqueue_run": "gtm_enqueue_owner",
    "run_status": "gtm_enqueue_owner",
}


def _two_brokerages(env: Env) -> dict[str, object]:
    a = env.brokerage("example-homes")
    b = env.brokerage("sample-realty")
    imp_a = env.import_example(a, "example-homes")
    imp_b = env.import_example(b, "sample-realty")
    env.enqueue(a, imp_a, "a-1")
    env.enqueue(b, imp_b, "b-1")
    w = env.worker()
    w.run()
    w.close()
    return {"a": a, "b": b, "imp_a": imp_a, "imp_b": imp_b}


def test_every_tenant_table_forces_rls_with_a_policy(env: Env) -> None:
    rows = env.rows(
        """SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity,
                  (SELECT count(*) FROM pg_policies p WHERE p.schemaname = 'gtm' AND p.tablename = c.relname) AS policies
             FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'gtm' AND c.relkind = 'r'"""
    )
    by_name = {r["relname"]: r for r in rows}
    assert set(TENANT_TABLES) <= set(by_name)
    for table in TENANT_TABLES:
        r = by_name[table]
        assert r["relrowsecurity"] and r["relforcerowsecurity"] and r["policies"] >= 1, table
    # The only table without RLS holds no tenant data.
    assert {n for n, r in by_name.items() if not r["relforcerowsecurity"]} == {"workers"}


def test_runtime_connections_use_limited_roles(env: Env) -> None:
    w = env.worker()
    for conn, expected in (
        (env.app, "gtm_app"),
        (env.trigger, "gtm_trigger"),
        (w.conn, "gtm_worker"),
    ):
        row = conn.execute(
            """SELECT current_user AS u, r.rolbypassrls, r.rolsuper FROM pg_roles r WHERE r.rolname = current_user"""
        ).fetchone()
        assert row == {"u": expected, "rolbypassrls": False, "rolsuper": False}
    w.close()


def test_privileged_functions_are_hardened(env: Env) -> None:
    rows = env.rows(
        """SELECT p.proname, o.rolname AS owner, o.rolcanlogin, o.rolbypassrls, o.rolsuper, p.prosecdef, p.proconfig,
                  EXISTS (SELECT 1 FROM aclexplode(p.proacl) a WHERE a.grantee = 0) AS public_execute
             FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace JOIN pg_roles o ON o.oid = p.proowner
            WHERE n.nspname = 'gtm' AND p.proname = ANY(%s)""",
        (list(PRIVILEGED),),
    )
    assert len(rows) == len(PRIVILEGED)
    for r in rows:
        assert r["owner"] == PRIVILEGED[r["proname"]]
        assert not (r["rolcanlogin"] or r["rolbypassrls"] or r["rolsuper"])
        assert r["prosecdef"] is True
        assert r["proconfig"] == ["search_path=pg_catalog, pg_temp"]
        assert r["public_execute"] is False
    # No function in the schema is executable by PUBLIC.
    assert (
        env.scalar(
            """SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace,
                  LATERAL aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) a
            WHERE n.nspname = 'gtm' AND a.grantee = 0"""
        )
        == 0
    )


def test_shared_contact_is_isolated_between_brokerages(env: Env) -> None:
    ids = _two_brokerages(env)
    a, b = ids["a"], ids["b"]
    a_lead = env.lead_id(a.tenant_id, "jordan.lee@mail.example")  # type: ignore[attr-defined]
    b_lead = env.lead_id(b.tenant_id, "jordan.lee@mail.example")  # type: ignore[attr-defined]
    assert a_lead != b_lead

    worker = connect(env.dsns.worker)
    for conn in (env.app, worker):
        for tenant, other in ((a, b), (b, a)):
            with tenant_tx(conn, tenant.tenant_id):  # type: ignore[attr-defined]
                for table in TENANT_TABLES:
                    col = "id" if table == "tenants" else "tenant_id"
                    try:
                        with (
                            conn.transaction()
                        ):  # savepoint: a denied table must not abort the scope
                            seen = conn.execute(
                                f"SELECT DISTINCT {col} AS t FROM gtm.{table}"
                            ).fetchall()
                    except psycopg.errors.InsufficientPrivilege:
                        continue
                    assert {r["t"] for r in seen} <= {tenant.tenant_id}, (table, conn.info.user)  # type: ignore[attr-defined]
                assert conn.execute(
                    "SELECT count(*) AS n FROM gtm.leads WHERE tenant_id = %s",
                    (other.tenant_id,),  # type: ignore[attr-defined]
                ).fetchone() == {"n": 0}
    worker.close()


def test_missing_tenant_scope_sees_nothing(env: Env) -> None:
    _two_brokerages(env)
    assert env.scalar("SELECT count(*) FROM gtm.stage_results") > 0
    for conn in (env.app, connect(env.dsns.worker)):
        for table in TENANT_TABLES:
            try:
                n = conn.execute(f"SELECT count(*) AS n FROM gtm.{table}").fetchone()
            except psycopg.errors.InsufficientPrivilege:
                continue
            assert n == {"n": 0}, (table, conn.info.user)
        with conn.transaction():
            conn.execute("SELECT set_config('app.tenant_id', '', true)")
            assert conn.execute("SELECT count(*) AS n FROM gtm.leads").fetchone() == {"n": 0}


def test_cross_tenant_writes_and_references_are_rejected(env: Env) -> None:
    ids = _two_brokerages(env)
    a, b = ids["a"], ids["b"]
    b_lead = env.lead_id(b.tenant_id, "jordan.lee@mail.example")  # type: ignore[attr-defined]

    with pytest.raises(psycopg.errors.InsufficientPrivilege), tenant_tx(env.app, a.tenant_id):  # type: ignore[attr-defined]
        env.app.execute(
            "INSERT INTO gtm.leads (tenant_id, identity_key) VALUES (%s, 'x')", (b.tenant_id,)
        )  # type: ignore[attr-defined]

    with pytest.raises(psycopg.errors.ForeignKeyViolation), tenant_tx(env.app, a.tenant_id):  # type: ignore[attr-defined]
        env.app.execute(
            """INSERT INTO gtm.lead_records (tenant_id, lead_id, version, schema_version, content, content_sha256)
               VALUES (%s, %s, 9, 'lead_record.v1', '{}', 'x')""",
            (a.tenant_id, b_lead),  # type: ignore[attr-defined]
        )


def test_enqueue_rejects_mismatched_identities_and_is_idempotent(env: Env) -> None:
    ids = _two_brokerages(env)
    a, b, imp_a, imp_b = ids["a"], ids["b"], ids["imp_a"], ids["imp_b"]
    bad = [
        (a.tenant_id, b.campaign_id, imp_a.import_id),  # type: ignore[attr-defined]
        (a.tenant_id, a.campaign_id, imp_b.import_id),  # type: ignore[attr-defined]
        (b.tenant_id, a.campaign_id, imp_a.import_id),  # type: ignore[attr-defined]
    ]
    for tenant, campaign, imp in bad:
        with pytest.raises(psycopg.errors.UndefinedObject):
            enqueue_run(env.trigger, tenant, campaign, imp, "mismatch")
    first = enqueue_run(env.trigger, a.tenant_id, a.campaign_id, imp_a.import_id, "a-1")  # type: ignore[attr-defined]
    assert first.created is False
    with pytest.raises(psycopg.errors.UniqueViolation):
        # The same trigger id cannot be reused for different inputs.
        other_import = env.import_example(a, "example-homes")  # type: ignore[arg-type]
        enqueue_run(env.trigger, a.tenant_id, a.campaign_id, other_import.import_id, "a-1")  # type: ignore[attr-defined]
    status = run_status(env.trigger, a.tenant_id, first.run_id)  # type: ignore[attr-defined]
    # Five contacts (no suppression here): two qualify, so 5 research + 5 check + 2 write, 2 drafts queued.
    assert status["leads"] == 5 and status["jobs"] == {"completed": 12, "queued": 2}
    with pytest.raises(psycopg.errors.UndefinedObject):
        run_status(env.trigger, b.tenant_id, first.run_id)  # type: ignore[attr-defined]


def test_roles_have_only_their_operations(env: Env) -> None:
    applied = env.brokerage()
    worker = Worker(env.dsns.worker, env.providers)
    denied = [
        (env.trigger, "SELECT * FROM gtm.jobs"),
        (env.trigger, "SELECT * FROM gtm.claim_job(gen_random_uuid(), ARRAY['research'], 60)"),
        (env.trigger, "SELECT * FROM gtm.lead_records"),
        (env.app, "SELECT * FROM gtm.claim_job(gen_random_uuid(), ARRAY['research'], 60)"),
        (
            worker.conn,
            f"SELECT * FROM gtm.enqueue_run('{applied.tenant_id}', '{applied.campaign_id}', gen_random_uuid(), 'x')",
        ),
        (worker.conn, "DELETE FROM gtm.stage_results"),
        (worker.conn, "UPDATE gtm.jobs SET config_version_id = gen_random_uuid()"),
        (worker.conn, "UPDATE gtm.stage_results SET outcome = 'x'"),
        (worker.conn, "UPDATE gtm.jobs SET fencing_token = 99"),
        (env.app, "INSERT INTO gtm.stage_results (tenant_id) VALUES (gen_random_uuid())"),
        (env.app, "UPDATE gtm.config_versions SET version = 9"),
    ]
    for conn, statement in denied:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(statement)
    worker.close()


def test_immutable_rows_and_pinned_job_inputs_resist_even_the_owner(jordan_run: tuple) -> None:  # type: ignore[type-arg]
    env, _applied, _run = jordan_run
    w = env.worker()
    w.run()
    w.close()
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        env.admin.execute("UPDATE gtm.stage_results SET outcome = 'changed'")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        env.admin.execute("UPDATE gtm.jobs SET model_id = 'other-model'")
