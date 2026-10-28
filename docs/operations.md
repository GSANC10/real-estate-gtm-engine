# Operations

> Only the local parts of this page have been exercised. The Supabase steps follow the plan and
> Supabase's documentation, but they have **not** been run against a hosted project yet.

## Local development
- `uv run pytest` and `uv run gtm demo --fake-providers` start a throwaway PostgreSQL 17 cluster
  (`initdb` in a temp directory, loopback only, random port) and delete it afterwards.
- Set `GTM_PG_BIN` if the PostgreSQL 17 binaries are not in a standard Homebrew or Debian location.
- Set `GTM_TEST_ADMIN_DSN` to use an existing disposable server instead (CI does this). The tests
  create cluster-wide roles there, so never point it at a server that matters.

## Database roles (any environment)
The migration creates `gtm_worker`, `gtm_app`, and `gtm_trigger` as **NOLOGIN** roles. No
password ever appears in a migration. To enable a role in an environment, connect as the
migration role and set a generated password without saving it in shell history:

```sql
\password gtm_worker            -- psql prompts for the value
ALTER ROLE gtm_worker LOGIN;
```

Repeat for `gtm_app` (operator CLI) and `gtm_trigger` (trigger command). Supabase backups do
not preserve custom role passwords, so a restore must also reset them.

## Supabase (not yet verified)
1. Use a project on PostgreSQL 16 or later. The migration uses `GRANT … WITH INHERIT FALSE, SET TRUE`.
2. Apply `supabase/migrations/` with the Supabase CLI (`supabase db push`).
3. Keep the `gtm` schema out of the Data API's exposed schemas. The migration grants nothing to
   `anon`, `authenticated`, or `service_role`.
4. Connect the worker through the direct endpoint, or through the session pooler if the VM is
   IPv4-only. Never use the transaction pooler. Use `sslmode=verify-full` with the project's
   root certificate.
5. Run the isolation tests' role and ownership checks against the hosted project before the pilot.

## Environment variables
| Variable | Used by | Role |
|---|---|---|
| `GTM_WORKER_DATABASE_URL` | `gtm worker` | `gtm_worker` |
| `GTM_APP_DATABASE_URL` | `gtm config`, `import`, `trace`, `suppress`, `campaign` | `gtm_app` |
| `GTM_TRIGGER_DATABASE_URL` | `gtm enqueue`, `gtm status` | `gtm_trigger` |
| `GTM_PRIVATE_TERMS_FILE` | `scripts/check_public_safety.py` | none (a local file of real names that must never appear) |
| `GTM_TEST_ADMIN_DSN`, `GTM_PG_BIN` | tests and demo | throwaway databases only |

Values live on the VM in `/etc/gtm`, readable only by the service user. GitHub Actions holds none
of them.
