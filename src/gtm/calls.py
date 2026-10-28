"""Bounded, recorded provider calls.

Every model call and research tool call gets a ``provider_calls`` row before dispatch, with its
exact input, a deterministic idempotency key, and a worst-case cost reservation against the run
budget. Per-job limits on model calls, tokens, and JSON repairs stop runaway agent loops.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb
from pydantic import BaseModel, ValidationError

from gtm.config import Pricing, ResolvedConfig, StageLimits
from gtm.db import Conn, maybe_one, one, tenant_tx
from gtm.faults import NO_FAULTS, Faults
from gtm.providers.base import (
    ModelRequest,
    ModelResponse,
    ProviderError,
    Providers,
    ProviderTimeout,
)
from gtm.queue import Job
from gtm.util import estimate_tokens, json_sha256

MTOK = Decimal(1_000_000)


class LimitExceeded(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class BudgetExceeded(Exception):
    code = "budget_exceeded"


class InvalidOutput(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


def model_cost(pricing: Pricing, input_tokens: int, output_tokens: int) -> Decimal:
    return (
        pricing.input_usd_per_mtok * input_tokens + pricing.output_usd_per_mtok * output_tokens
    ) / MTOK


@dataclass
class CallRecorder:
    conn: Conn
    job: Job
    config: ResolvedConfig
    limits: StageLimits
    providers: Providers
    heartbeat: Callable[[], None]
    faults: Faults = NO_FAULTS
    model_calls: int = 0
    seq: int = 0

    # -- budget -------------------------------------------------------------------------

    def _reserve(self, row: dict[str, Any]) -> UUID:
        """Reserve cost on the run and save the call row, in one transaction."""
        with tenant_tx(self.conn, self.job.tenant_id):
            ok = maybe_one(
                self.conn,
                """UPDATE gtm.runs SET reserved_usd = reserved_usd + %(cost)s, updated_at = now()
                    WHERE id = %(run)s AND status = 'active'
                      AND settled_usd + reserved_usd + uncertain_usd + %(cost)s <= budget_cap_usd
                RETURNING id""",
                {"cost": row["cost"], "run": self.job.run_id},
            )
            if ok is None:
                self.conn.execute(
                    "UPDATE gtm.runs SET status = 'paused_budget', updated_at = now() WHERE id = %s",
                    (self.job.run_id,),
                )
            else:
                call_id: UUID = one(
                    self.conn,
                    """INSERT INTO gtm.provider_calls
                     (tenant_id, run_id, job_id, stage, job_attempt, round, seq, provider, kind, operation,
                      model_id, input, input_sha256, state, idempotency_key, cost_reserved_usd)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'reserved', %s, %s)
                   RETURNING id""",
                    (
                        self.job.tenant_id,
                        self.job.run_id,
                        self.job.id,
                        self.job.stage,
                        self.job.attempt,
                        row["round"],
                        self.seq,
                        row["provider"],
                        row["kind"],
                        row["operation"],
                        row["model_id"],
                        Jsonb(row["input"]),
                        json_sha256(row["input"]),
                        row["idempotency_key"],
                        row["cost"],
                    ),
                )["id"]
        if ok is None:
            raise BudgetExceeded()
        return call_id

    def _settle(
        self,
        call_id: UUID,
        reserved: Decimal,
        *,
        state: str,
        cost: Decimal | None,
        output: Any = None,
        usage: dict[str, int] | None = None,
        request_id: str | None = None,
        error_code: str | None = None,
    ) -> None:
        with tenant_tx(self.conn, self.job.tenant_id):
            self.conn.execute(
                """UPDATE gtm.provider_calls
                      SET state = %s, cost_settled_usd = %s, output = %s, usage = %s, request_id = %s,
                          error_code = %s, settled_at = now()
                    WHERE id = %s""",
                (
                    state,
                    cost,
                    Jsonb(output) if output is not None else None,
                    Jsonb(usage) if usage is not None else None,
                    request_id,
                    error_code,
                    call_id,
                ),
            )
            # Uncertain calls stay counted as spent until provider usage records reconcile them.
            settled = cost if state in ("succeeded", "failed") and cost is not None else Decimal(0)
            uncertain = reserved if state == "uncertain" else Decimal(0)
            self.conn.execute(
                """UPDATE gtm.runs SET reserved_usd = greatest(reserved_usd - %s, 0),
                                       settled_usd = settled_usd + %s,
                                       uncertain_usd = uncertain_usd + %s, updated_at = now()
                    WHERE id = %s""",
                (reserved, settled, uncertain, self.job.run_id),
            )

    def _idempotency_key(self) -> str:
        return f"{self.job.id}:{self.job.attempt}:{self.seq}"

    # -- model calls ----------------------------------------------------------------------

    def model(
        self,
        role: str,
        messages: list[dict[str, Any]],
        *,
        round_no: int,
        tools: list[dict[str, Any]] | None = None,
    ) -> ModelResponse:
        if self.model_calls >= self.limits.model_calls:
            raise LimitExceeded("model_calls_exceeded")
        estimated_in = estimate_tokens({"messages": messages, "tools": tools or []})
        if estimated_in > self.limits.input_tokens:
            raise LimitExceeded("input_tokens_exceeded")
        role_cfg = self.config.roles.get(role)
        reserved = model_cost(role_cfg.pricing, estimated_in, self.limits.output_tokens)
        self.model_calls += 1
        self.seq += 1
        request = ModelRequest(
            role=role,
            model_id=role_cfg.model_id,
            messages=messages,
            tools=tools or [],
            max_output_tokens=self.limits.output_tokens,
        )
        call_id = self._reserve(
            {
                "round": round_no,
                "provider": self.providers.model.provider,
                "kind": "model",
                "operation": role,
                "model_id": role_cfg.model_id,
                "input": {"messages": messages, "tools": tools or []},
                "idempotency_key": self._idempotency_key(),
                "cost": reserved,
            }
        )
        self.heartbeat()
        self.faults.hit("provider.model")
        try:
            response = self.providers.model.complete(request)
        except ProviderTimeout:
            self._settle(
                call_id, reserved, state="uncertain", cost=None, error_code="provider_timeout"
            )
            raise
        except ProviderError as exc:
            self._settle(call_id, reserved, state="failed", cost=Decimal(0), error_code=exc.code)
            raise
        usage = {
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
        }
        cost = model_cost(
            role_cfg.pricing, response.usage.input_tokens, response.usage.output_tokens
        )
        output = {
            "content": response.content,
            "tool_calls": [
                {"id": t.id, "name": t.name, "arguments": t.arguments} for t in response.tool_calls
            ],
        }
        self._settle(
            call_id,
            reserved,
            state="succeeded",
            cost=cost,
            output=output,
            usage=usage,
            request_id=response.request_id,
        )
        if response.usage.output_tokens > self.limits.output_tokens:
            raise LimitExceeded("output_tokens_exceeded")
        return response

    def model_json[M: BaseModel](
        self,
        role: str,
        messages: list[dict[str, Any]],
        schema: type[M],
        *,
        round_no: int,
        repairs: int,
        validate: Callable[[M], list[str]] | None = None,
    ) -> tuple[M, int]:
        """Call ``role`` for a JSON answer, re-asking with the validation error up to ``repairs`` times.

        Each repair is a model call and counts against the job's limit. Returns the parsed
        output and the number of repairs used.
        """
        convo = list(messages)
        used = 0
        while True:
            response = self.model(role, convo, round_no=round_no + used)
            parsed, errors = parse_output(response.content, schema, validate)
            if parsed is not None:
                return parsed, used
            if used >= repairs:
                raise InvalidOutput("invalid_output", "; ".join(errors)[:300])
            used += 1
            convo = [
                *convo,
                {"role": "assistant", "content": response.content or ""},
                {
                    "role": "user",
                    "content": "Your answer did not match the required JSON contract: "
                    + "; ".join(errors)[:600]
                    + ". Reply again with only the corrected JSON object.",
                },
            ]

    # -- research tools -------------------------------------------------------------------

    def tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        round_no: int,
        run: Callable[[str], tuple[Any, str | None]],
    ) -> Any:
        tools_cfg = self.config.research_tools
        cost = (
            tools_cfg.pricing.search_usd
            if name == "exa_search"
            else tools_cfg.pricing.page_read_usd
        )
        self.seq += 1
        key = self._idempotency_key()
        call_id = self._reserve(
            {
                "round": round_no,
                "provider": self.providers.tools.provider,
                "kind": "tool",
                "operation": name,
                "model_id": None,
                "input": arguments,
                "idempotency_key": key,
                "cost": cost,
            }
        )
        self.heartbeat()
        self.faults.hit("provider.tool")
        try:
            result, request_id = run(key)
        except ProviderTimeout:
            self._settle(call_id, cost, state="uncertain", cost=None, error_code="provider_timeout")
            raise
        except ProviderError as exc:
            self._settle(call_id, cost, state="failed", cost=Decimal(0), error_code=exc.code)
            raise
        self._settle(
            call_id, cost, state="succeeded", cost=cost, output=result, request_id=request_id
        )
        return result


def parse_output[M: BaseModel](
    content: str | None, schema: type[M], validate: Callable[[M], list[str]] | None
) -> tuple[M | None, list[str]]:
    """Parse and validate a model answer: (output, []) when valid, else (None, problems)."""
    if not content:
        return None, ["empty response"]
    try:
        parsed = schema.model_validate_json(content)
    except ValidationError as exc:
        return None, [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()[:8]]
    problems = validate(parsed) if validate is not None else []
    return (None, problems) if problems else (parsed, [])


def mark_interrupted_calls(conn: Conn, job: Job) -> int:
    """Calls left 'reserved' by an earlier, interrupted attempt may have been billed: mark them uncertain."""
    with tenant_tx(conn, job.tenant_id):
        rows = conn.execute(
            """UPDATE gtm.provider_calls SET state = 'uncertain', error_code = 'interrupted', settled_at = now()
                WHERE job_id = %s AND job_attempt < %s AND state = 'reserved'
            RETURNING cost_reserved_usd""",
            (job.id, job.attempt),
        ).fetchall()
        total = sum((r["cost_reserved_usd"] for r in rows), Decimal(0))
        if rows:
            conn.execute(
                """UPDATE gtm.runs SET reserved_usd = greatest(reserved_usd - %s, 0),
                                       uncertain_usd = uncertain_usd + %s, updated_at = now()
                    WHERE id = %s""",
                (total, total, job.run_id),
            )
    return len(rows)
