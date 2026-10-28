"""Provider-neutral interfaces for hosted models and research tools.

Messages use the OpenAI-compatible chat shape (system/user/assistant/tool), which the hosted
model adapters translate as needed. Only read-only research tools exist in M1.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Protocol


class ProviderError(Exception):
    """A provider call failed. ``code`` is a short error code safe for logs."""

    code = "provider_error"


class ProviderTimeout(ProviderError):
    """No usable response. The request may still have been processed and billed."""

    code = "provider_timeout"


class ProviderRejected(ProviderError):
    """A documented, definitive rejection before any work was done."""

    code = "provider_rejected"


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class ModelRequest:
    # ``role`` is local metadata for routing and fakes. Real adapters do not send it.
    role: str
    model_id: str
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] = field(default_factory=list)
    max_output_tokens: int = 1000


@dataclass(frozen=True)
class ModelResponse:
    content: str | None
    tool_calls: list[ToolCall]
    usage: Usage
    request_id: str | None


class ModelClient(Protocol):
    provider: str

    def complete(self, request: ModelRequest) -> ModelResponse: ...


@dataclass(frozen=True)
class SearchResult:
    url: str
    title: str
    published_at: date | None
    snippet: str


@dataclass(frozen=True)
class PageContent:
    url: str
    title: str
    published_at: date | None
    text: str


class ResearchTools(Protocol):
    provider: str

    def search(
        self, query: str, num_results: int, idempotency_key: str
    ) -> tuple[list[SearchResult], str | None]: ...

    def read_page(
        self, url: str, max_chars: int, idempotency_key: str
    ) -> tuple[PageContent, str | None]: ...


@dataclass(frozen=True)
class Providers:
    model: ModelClient
    tools: ResearchTools
