"""Bounded, recorded provider calls.

Every model call and research tool call gets a ``provider_calls`` row before it is sent, with
its exact input and a cost estimate. A call goes out only while the run's spending plus that
estimate stays within its cap. Per-job limits on model calls, tokens, and JSON repairs stop
runaway agent loops. A timed-out call may still be billed, so its estimate counts as spent.
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
from gtm.db import Conn, maybe_one, one
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


@dataclass
class CallRecorder:
    conn: Conn
    job: Job
    config: ResolvedConfig
    limits: StageLimits
    providers: Providers
    model_calls: int = 0
    seq: int = 0

    def _start(
        self,
        *,
        round_no: int,
        provider: str,
        kind: str,
        operation: str,
        model_id: str | None,
        call_input: Any,
        estimate: Decimal,
    ) -> UUID:
        """Check the run's cap and save the call row, in one transaction."""
        self.seq += 1
        with self.conn.transaction():
            ok = maybe_one(
                self.conn,
                """SELECT 1 FROM gtm.runs WHERE id = %s AND status = 'active'
                     AND spent_usd + %s <= budget_cap_usd FOR UPDATE""",
                (self.job.run_id, estimate),
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
                         (run_id, job_id, stage, job_attempt, round, seq, provider, kind, operation, model_id,
                          input, input_sha256, state, idempotency_key, cost_estimate_usd)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'started', %s, %s)
                       RETURNING id""",
                    (
                        self.job.run_id,
                        self.job.id,
                        self.job.stage,
                        self.job.attempt,
                        round_no,
                        self.seq,
                        provider,
                        kind,
                        operation,
                        model_id,
                        Jsonb(call_input),
                        json_sha256(call_input),
                        f"{self.job.id}:{self.job.attempt}:{self.seq}",
                        estimate,
                    ),
                )["id"]
        if ok is None:
            raise BudgetExceeded()
        return call_id

    def _finish(
        self,
        call_id: UUID,
        *,
        state: str,
        cost: Decimal,
        output: Any = None,
        usage: dict[str, int] | None = None,
        request_id: str | None = None,
        error_code: str | None = None,
    ) -> None:
        with self.conn.transaction():
            self.conn.execute(
                """UPDATE gtm.provider_calls SET state = %s, cost_usd = %s, output = %s, usage = %s,
                          request_id = %s, error_code = %s, settled_at = now()
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
            self.conn.execute(
                "UPDATE gtm.runs SET spent_usd = spent_usd + %s, updated_at = now() WHERE id = %s",
                (cost, self.job.run_id),
            )

    def _dispatch[T](self, call_id: UUID, estimate: Decimal, send: Callable[[], T]) -> T:
        try:
            return send()
        except ProviderTimeout:
            self._finish(call_id, state="uncertain", cost=estimate, error_code="provider_timeout")
            raise
        except ProviderError as exc:
            self._finish(call_id, state="failed", cost=Decimal(0), error_code=exc.code)
            raise

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
        estimate = model_cost(role_cfg.pricing, estimated_in, self.limits.output_tokens)
        self.model_calls += 1
        request = ModelRequest(
            role=role,
            model_id=role_cfg.model_id,
            messages=messages,
            tools=tools or [],
            max_output_tokens=self.limits.output_tokens,
        )
        call_id = self._start(
            round_no=round_no,
            provider=self.providers.model.provider,
            kind="model",
            operation=role,
            model_id=role_cfg.model_id,
            call_input={"messages": messages, "tools": tools or []},
            estimate=estimate,
        )
        response = self._dispatch(call_id, estimate, lambda: self.providers.model.complete(request))
        usage = {
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
        }
        output = {
            "content": response.content,
            "tool_calls": [
                {"id": t.id, "name": t.name, "arguments": t.arguments} for t in response.tool_calls
            ],
        }
        self._finish(
            call_id,
            state="succeeded",
            cost=model_cost(
                role_cfg.pricing, response.usage.input_tokens, response.usage.output_tokens
            ),
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

        Each repair is a model call and counts against the job's limit. Returns the parsed output
        and the number of repairs used.
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
        pricing = self.config.research_tools.pricing
        cost = pricing.search_usd if name == "exa_search" else pricing.page_read_usd
        call_id = self._start(
            round_no=round_no,
            provider=self.providers.tools.provider,
            kind="tool",
            operation=name,
            model_id=None,
            call_input=arguments,
            estimate=cost,
        )
        key = f"{self.job.id}:{self.job.attempt}:{self.seq}"
        result, request_id = self._dispatch(call_id, cost, lambda: run(key))
        self._finish(call_id, state="succeeded", cost=cost, output=result, request_id=request_id)
        return result


def mark_interrupted_calls(conn: Conn, job: Job) -> int:
    """Calls a stopped program left 'started' may have been billed: mark them uncertain and count them."""
    with conn.transaction():
        rows = conn.execute(
            """UPDATE gtm.provider_calls SET state = 'uncertain', cost_usd = cost_estimate_usd,
                      error_code = 'interrupted', settled_at = now()
                WHERE job_id = %s AND job_attempt < %s AND state = 'started'
            RETURNING cost_estimate_usd""",
            (job.id, job.attempt),
        ).fetchall()
        if rows:
            conn.execute(
                "UPDATE gtm.runs SET spent_usd = spent_usd + %s, updated_at = now() WHERE id = %s",
                (sum((r["cost_estimate_usd"] for r in rows), Decimal(0)), job.run_id),
            )
    return len(rows)
