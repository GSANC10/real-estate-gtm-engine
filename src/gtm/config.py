"""Brokerage configuration.

A YAML file is validated and resolved: each role's instructions are read from ``prompts/`` and
copied in with their hash. The resolved config is saved on every run, and jobs read it from
their run, so editing the file never changes work that is already queued.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from gtm.contracts import ServiceArea
from gtm.util import sha256_hex

PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"
TOOL_SCHEMA_VERSION = "research_tools.v1"
ROLE_NAMES = ("researcher", "claim_check", "writer", "email_check")


class Cfg(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Brokerage(Cfg):
    name: str
    postal_address: str
    kind: Literal["fictional", "real"]


class SenderFactConfig(Cfg):
    id: str = Field(pattern=r"^sender_fact_[0-9]+$")
    text: str = Field(max_length=200)
    use_in_outreach: bool


class Sender(Cfg):
    name: str
    title: str
    email: str
    languages: list[str]
    # Private sender profile. Never sent to a model and never used to select or address recipients.
    background: str | None = None
    facts: list[SenderFactConfig]

    @field_validator("facts")
    @classmethod
    def _unique_fact_ids(cls, facts: list[SenderFactConfig]) -> list[SenderFactConfig]:
        ids = [f.id for f in facts]
        if len(ids) != len(set(ids)):
            raise ValueError("sender fact ids must be unique")
        return facts


class Mailbox(Cfg):
    address: str
    mode: Literal["draft_only"]


class Footer(Cfg):
    postal_address: str
    opt_out_line: str
    disclosures: list[str] = Field(default_factory=list)


class Campaign(Cfg):
    name: str
    service_area: ServiceArea
    need_categories: list[str] = Field(min_length=1)
    offer: str
    cta: str
    tone_rules: list[str]
    tone_reference: str = Field(max_length=2000)
    max_words: int = Field(ge=40, le=300)
    language: str = Field(default="en", pattern=r"^[a-z]{2}$")
    evidence_max_age_days: int = Field(ge=1, le=730)
    budget_cap_usd: Decimal = Field(ge=0)
    footer: Footer


class Pricing(Cfg):
    input_usd_per_mtok: Decimal = Field(ge=0)
    output_usd_per_mtok: Decimal = Field(ge=0)


class Role(Cfg):
    provider: Literal["fake", "venice"]
    model_id: str
    prompt_version: str = Field(pattern=r"^[a-z_]+\.v[0-9]+$")
    pricing: Pricing


class Roles(Cfg):
    researcher: Role
    claim_check: Role
    writer: Role
    email_check: Role

    def get(self, name: str) -> Role:
        role: Role = getattr(self, name)
        return role


class ToolPricing(Cfg):
    search_usd: Decimal = Field(ge=0)
    page_read_usd: Decimal = Field(ge=0)


class ResearchTools(Cfg):
    provider: Literal["fake", "deepline"]
    searches: int = Field(ge=0, le=3)
    results_per_search: int = Field(ge=1, le=5)
    page_reads: int = Field(ge=0, le=3)
    page_chars: int = Field(ge=500, le=4000)
    pricing: ToolPricing


class StageLimits(Cfg):
    model_calls: int = Field(ge=1, le=20)
    input_tokens: int = Field(ge=500, le=50_000)
    output_tokens: int = Field(ge=50, le=8_000)
    json_repairs: int = Field(ge=0, le=3)


class CheckLimits(StageLimits):
    web_claims: int = Field(ge=0, le=6)


class Limits(Cfg):
    research: StageLimits
    check: CheckLimits
    write: StageLimits


class QualificationRules(Cfg):
    criteria: list[Literal["service_area", "property_need"]]

    @model_validator(mode="after")
    def _both_criteria(self) -> QualificationRules:
        if sorted(self.criteria) != ["property_need", "service_area"]:
            raise ValueError(
                "qualification uses exactly two criteria: service_area and property_need"
            )
        return self


class BrokerageConfig(Cfg):
    schema_version: Literal["brokerage_config.v1"]
    brokerage: Brokerage
    sender: Sender
    mailbox: Mailbox
    campaign: Campaign
    qualification: QualificationRules
    roles: Roles
    research_tools: ResearchTools
    limits: Limits


class PromptRef(Cfg):
    version: str
    sha256: str
    text: str


class ResolvedConfig(BrokerageConfig):
    """The form saved on each run: the configuration plus the exact role instructions."""

    prompts: dict[str, PromptRef]


def load_config_file(path: Path) -> BrokerageConfig:
    return BrokerageConfig.model_validate(yaml.safe_load(path.read_text()))


def read_prompt(version: str, prompts_dir: Path = PROMPTS_DIR) -> PromptRef:
    text = (prompts_dir / f"{version}.md").read_text()
    return PromptRef(version=version, sha256=sha256_hex(text), text=text)


def resolve(cfg: BrokerageConfig, prompts_dir: Path = PROMPTS_DIR) -> ResolvedConfig:
    prompts = {
        name: read_prompt(cfg.roles.get(name).prompt_version, prompts_dir) for name in ROLE_NAMES
    }
    return ResolvedConfig(**cfg.model_dump(), prompts=prompts)


def parse_stored(content: dict[str, Any]) -> ResolvedConfig:
    return ResolvedConfig.model_validate(content)
