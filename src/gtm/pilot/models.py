"""Schemas for the opportunity-report pilot.

Each stage writes its own file in a run folder and never edits an earlier one:
criteria (input) -> discovered.json -> verified.json -> scored.json -> report.csv / report.html.
No field describes a person's characteristics; opportunities are about properties and signals.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SignalType = Literal[
    "fsbo_listing",
    "public_intent_to_sell",
    "business_relocation_with_property",
    "expired_or_withdrawn_listing",
    "active_listing_price_reduction",
    "buyer_seeking_agent_or_financing",
]
# What the source says about an agent: an owner selling on their own is a listing opportunity;
# a property already represented is not.
Representation = Literal["no_agent_stated", "agent_involved", "unknown"]
PropertyType = Literal[
    "single_family", "condo", "townhouse", "multi_family", "commercial", "land", "unknown"
]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --- criteria (pilot/criteria.yaml) --------------------------------------------------------


class SignalRule(Strict):
    enabled: bool
    description: str


class SearchTask(Strict):
    id: str = Field(pattern=r"^[a-z0-9_]{2,40}$")
    signal: SignalType
    task: str = Field(min_length=10, max_length=500)


class Pricing(Strict):
    checked_on: str
    input_usd_per_mtok: float = Field(ge=0)
    output_usd_per_mtok: float = Field(ge=0)
    web_search_usd_per_request: float = Field(ge=0)


class Criteria(Strict):
    schema_version: Literal["pilot_criteria.v1"]
    market: str
    areas: list[str] = Field(min_length=1)
    priority_areas: list[str] = Field(default_factory=list)
    lookback_days: int = Field(ge=1, le=365)
    max_candidates: int = Field(ge=1, le=30)
    max_per_search: int = Field(ge=1, le=10)
    report_size: int = Field(ge=1, le=15)
    model: str
    reasoning_effort: Literal["none", "low", "high", "max"] = "low"
    max_output_tokens: int = Field(ge=500, le=16_000)
    pricing: Pricing
    signals: dict[SignalType, SignalRule]
    searches: list[SearchTask] = Field(min_length=1, max_length=10)

    @model_validator(mode="after")
    def _consistent(self) -> Criteria:
        if missing := [a for a in self.priority_areas if a not in self.areas]:
            raise ValueError(f"priority_areas must also be in areas: {missing}")
        ids = [t.id for t in self.searches]
        if len(ids) != len(set(ids)):
            raise ValueError("search ids must be unique")
        if unknown := sorted({t.signal for t in self.searches} - set(self.signals)):
            raise ValueError(f"searches use signals with no rule: {unknown}")
        return self


# --- what the discovery model returns ------------------------------------------------------


class Evidence(Strict):
    url: str = Field(min_length=8, max_length=1000)
    quote: str = Field(min_length=10, max_length=600)
    date: str | None = Field(default=None, max_length=40)


class PropertyContext(Strict):
    address: str | None = Field(default=None, max_length=200)
    city: str = Field(min_length=2, max_length=80)
    neighborhood: str | None = Field(default=None, max_length=80)
    property_type: PropertyType = "unknown"
    asking_price: str | None = Field(default=None, max_length=40)


class CandidateOut(Strict):
    signal: SignalType
    summary: str = Field(min_length=10, max_length=300)
    property: PropertyContext
    representation: Representation = "unknown"
    evidence: list[Evidence] = Field(min_length=1, max_length=5)
    missing_info: list[str] = Field(default_factory=list, max_length=8)


class DiscoveryOut(Strict):
    candidates: list[CandidateOut] = Field(max_length=10)


class Citation(BaseModel):
    """One web search result as Venice returns it in ``venice_parameters.web_search_citations``."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    url: str
    title: str = ""
    date: str | None = None
    content: str | None = None


# --- discovered.json -----------------------------------------------------------------------


class EvidenceCheck(Strict):
    url: str
    url_in_search_results: bool
    quote_in_search_result: bool
    search_result_title: str | None
    search_result_date: str | None


class Provenance(Strict):
    run_id: str
    search_id: str
    request_id: str | None
    model: str
    prompt_version: str
    raw_response_file: str
    retrieved_at: str
    request_usd: float  # the whole request's cost, shared by every candidate it returned


class Discovered(Strict):
    schema_version: Literal["discovered.v1"] = "discovered.v1"
    opportunity_id: str
    signal: SignalType
    summary: str
    property: PropertyContext
    representation: Representation
    evidence: list[Evidence]
    missing_info: list[str]
    discovery_checks: list[EvidenceCheck]
    provenance: Provenance


# --- verified.json -------------------------------------------------------------------------


DateBasis = Literal["page_metadata", "search_result", "model_stated", "none"]
VerifyStatus = Literal["verified", "rejected", "unverified", "duplicate"]


class SourceCheck(Strict):
    url: str
    fetched: bool
    http_status: int | None
    final_url: str | None
    snapshot_file: str | None
    quote_found: bool
    source_date: str | None
    date_basis: DateBasis
    area_on_page: bool


class Verified(Strict):
    schema_version: Literal["verified.v1"] = "verified.v1"
    opportunity_id: str
    # unverified: no source could be fetched (blocked or down); a person can check it by hand.
    status: VerifyStatus
    reasons: list[str]
    warnings: list[str]
    duplicate_of: str | None
    newest_source_date: str | None
    checks: list[SourceCheck]
    candidate: Discovered


# --- scored.json (written by the score-leads skill) ----------------------------------------


class Dimensions(Strict):
    intent: int = Field(ge=0, le=40)
    evidence: int = Field(ge=0, le=25)
    relevance: int = Field(ge=0, le=20)
    recency: int = Field(ge=0, le=15)


class Fact(Strict):
    statement: str = Field(min_length=5, max_length=300)
    source_url: str


class Scored(Strict):
    schema_version: Literal["scored.v1"] = "scored.v1"
    opportunity_id: str
    score: int = Field(ge=0, le=100)
    dimensions: Dimensions
    explanation: str = Field(min_length=10, max_length=600)
    facts: list[Fact] = Field(min_length=1)
    inferences: list[str]
    contradictions: list[str]
    missing_info: list[str]
    confidence: Literal["high", "medium", "low"]
    recommendation: Literal["include", "research_more", "reject"]
    next_step: str = Field(min_length=5, max_length=300)
    scored_by: str


# Published in pilot/schemas/ by `gtm contracts export`.
EXPORTED: dict[str, type[BaseModel]] = {
    "criteria": Criteria,
    "discovery-output": DiscoveryOut,
    "discovered": Discovered,
    "verified": Verified,
    "scored": Scored,
}
