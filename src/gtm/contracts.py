"""Versioned contracts for stored records and for every model input and output.

JSON Schema copies are exported to docs/contracts/ (``gtm contracts export``); a test keeps
them in sync with these models.
"""

from __future__ import annotations

from datetime import date
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

Fit = Literal["met", "not_met", "unknown"]
Qualification = Literal["qualified", "insufficient_evidence", "disqualified", "needs_review"]
FindingUse = Literal["qualification", "personalization"]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --- lead_record.v1: one immutable version of a supplied contact --------------------------


class RecordedNeed(Contract):
    category: str = Field(pattern=r"^[a-z][a-z_]{2,39}$")
    note: str = Field(min_length=1, max_length=1000)
    # Where the person wants to buy, sell, or rent. Service fit uses this, not residence.
    property_location: str | None = Field(default=None, max_length=120)
    recorded_at: date
    source: str = Field(min_length=1, max_length=80)


class LeadRecord(Contract):
    schema_version: Literal["lead_record.v1"] = "lead_record.v1"
    email: str = Field(min_length=3, max_length=254)
    full_name: str = Field(min_length=1, max_length=120)
    organization: str | None = Field(default=None, max_length=120)
    # Identity identifier only.
    residence_location: str | None = Field(default=None, max_length=120)
    # Used only when the record states it.
    language_preference: str | None = Field(default=None, pattern=r"^[a-z]{2}$")
    recorded_need: RecordedNeed
    # Whether outreach may mention where the need was recorded (supplied by the brokerage).
    may_mention_inquiry: bool


# --- research.input.v1 / research.output.v1 -----------------------------------------------


class ServiceArea(Contract):
    places: list[str] = Field(min_length=1)
    postal_codes: list[str] = Field(default_factory=list)


class ResearchLead(Contract):
    lead_id: UUID
    full_name: str
    organization: str | None
    residence_location: str | None
    recorded_need: RecordedNeed


class ResearchCampaign(Contract):
    service_area: ServiceArea
    need_categories: list[str]
    evidence_max_age_days: int


class ResearchLimits(Contract):
    searches: int
    results_per_search: int
    page_reads: int
    page_chars: int


class ResearchInput(Contract):
    schema_version: Literal["research.input.v1"] = "research.input.v1"
    lead: ResearchLead
    campaign: ResearchCampaign
    limits: ResearchLimits


class IdentifiersSeen(Contract):
    name: str | None = Field(default=None, max_length=120)
    location: str | None = Field(default=None, max_length=120)
    organization: str | None = Field(default=None, max_length=120)


class ResearchCitation(Contract):
    key: str = Field(pattern=r"^C[0-9]{1,3}$")
    source: Literal["import_record", "web"]
    url: str | None = Field(default=None, max_length=500)
    quote: str = Field(min_length=1, max_length=400)
    published_at: date | None = None
    identifiers_seen: IdentifiersSeen = Field(default_factory=IdentifiersSeen)


class ResearchFinding(Contract):
    text: str = Field(min_length=1, max_length=200)
    citations: list[str] = Field(min_length=1, max_length=5)
    use: FindingUse


class Contradiction(Contract):
    text: str = Field(min_length=1, max_length=200)
    citations: list[str] = Field(default_factory=list, max_length=5)


class ProposedFit(Contract):
    service_area: Fit
    property_need: Fit
    rationale: str = Field(max_length=300)


class ResearchOutput(Contract):
    citations: list[ResearchCitation] = Field(min_length=1, max_length=12)
    findings: list[ResearchFinding] = Field(max_length=10)
    unknowns: list[str] = Field(default_factory=list, max_length=10)
    contradictions: list[Contradiction] = Field(default_factory=list, max_length=5)
    proposed_fit: ProposedFit


# --- claim_check.v1 ------------------------------------------------------------------------


class ClaimCheckPerson(Contract):
    name: str
    location: str | None
    organization: str | None


class ClaimCheckInput(Contract):
    schema_version: Literal["claim_check.v1"] = "claim_check.v1"
    claim: str = Field(max_length=200)
    quote: str = Field(max_length=400)
    published_at: date | None
    source_provenance: str
    person: ClaimCheckPerson


class ClaimCheckOutput(Contract):
    about_person: Literal["yes", "no", "unclear"]
    supports_claim: Literal["yes", "no", "unclear"]
    note: str = Field(max_length=150)


# --- checked_research.v1 -------------------------------------------------------------------


class CheckedFinding(Contract):
    id: str = Field(pattern=r"^finding_[0-9]+$")
    text: str = Field(max_length=200)
    use: FindingUse
    citation_ids: list[UUID]
    source_date: date | None
    provenance: str


class ExcludedFinding(Contract):
    text: str = Field(max_length=200)
    use: FindingUse
    reasons: list[str]


class OutreachContext(Contract):
    may_mention_inquiry: bool


class CheckedResearch(Contract):
    schema_version: Literal["checked_research.v1"] = "checked_research.v1"
    research_result_id: UUID
    qualification: Qualification
    service_area: Fit
    property_need: Fit
    reasons: list[str]
    findings: list[CheckedFinding]
    excluded_findings: list[ExcludedFinding]
    unknowns: list[str]
    contradictions: list[str]
    outreach_context: OutreachContext


# --- writer.input.v1 / writer.output.v1 ----------------------------------------------------


class WriterRecipient(Contract):
    first_name: str
    language_preference: str | None


class WriterFinding(Contract):
    id: str
    text: str = Field(max_length=200)
    refs: list[str]
    recorded_at: date | None


class SenderFact(Contract):
    id: str = Field(pattern=r"^sender_fact_[0-9]+$")
    text: str = Field(max_length=200)


class WriterSender(Contract):
    name: str
    title: str
    brokerage: str
    facts: list[SenderFact]


class WriterCampaign(Contract):
    offer: str
    cta: str
    tone_rules: list[str]
    tone_reference: str
    max_words: int


class WriterInput(Contract):
    schema_version: Literal["writer.input.v1"] = "writer.input.v1"
    recipient: WriterRecipient
    findings: list[WriterFinding] = Field(max_length=5)
    unknowns: list[str]
    contradictions: list[str]
    sender: WriterSender
    campaign: WriterCampaign
    outreach_context: OutreachContext
    prior_outreach: list[str] = Field(default_factory=list)


class WriterClaim(Contract):
    sentence: str = Field(min_length=1, max_length=400)
    refs: list[str] = Field(default_factory=list, max_length=5)


class WriterOutput(Contract):
    subject: str = Field(min_length=1, max_length=120)
    body: str = Field(min_length=1, max_length=2000)
    language: str = Field(pattern=r"^[a-z]{2}$")
    claims: list[WriterClaim] = Field(max_length=20)


# --- email_check.v1 ------------------------------------------------------------------------


class TextRef(Contract):
    id: str
    text: str


class EmailCheckInput(Contract):
    schema_version: Literal["email_check.v1"] = "email_check.v1"
    subject: str
    body: str
    sentences: list[str]
    claims: list[WriterClaim]
    findings: list[TextRef]
    sender_facts: list[TextRef]
    unknowns: list[str]
    outreach_context: OutreachContext


class SentenceVerdict(Contract):
    text: str
    verdict: Literal["supported", "unsupported", "overstated", "not_factual"]
    note: str = Field(default="", max_length=150)


class EmailCheckOutput(Contract):
    sentences: list[SentenceVerdict]


# --- context_snapshot.v1 -------------------------------------------------------------------


class ContextSnapshot(Contract):
    schema_version: Literal["context_snapshot.v1"] = "context_snapshot.v1"
    job_id: UUID
    role: str
    payload: dict[str, object]
    prompt_version: str | None
    prompt_sha256: str | None
    model_id: str | None
    tool_schema_version: str | None
    config_version_id: UUID
    input_refs: dict[str, object]


EXPORTED: dict[str, type[BaseModel]] = {
    "lead_record.v1": LeadRecord,
    "research.input.v1": ResearchInput,
    "research.output.v1": ResearchOutput,
    "claim_check.input.v1": ClaimCheckInput,
    "claim_check.output.v1": ClaimCheckOutput,
    "checked_research.v1": CheckedResearch,
    "writer.input.v1": WriterInput,
    "writer.output.v1": WriterOutput,
    "email_check.input.v1": EmailCheckInput,
    "email_check.output.v1": EmailCheckOutput,
    "context_snapshot.v1": ContextSnapshot,
}
