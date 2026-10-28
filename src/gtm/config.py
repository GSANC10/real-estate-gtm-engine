"""Brokerage and campaign configuration.

YAML files are validated, resolved (role instructions are read from ``prompts/`` and pinned by
hash and text), and stored as immutable ``config_versions`` rows. Jobs pin a version, so later
edits never change queued work. Each stage's fingerprint covers only the settings that stage
uses, so changing the writing tone does not re-run research.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

import yaml
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from gtm.contracts import ServiceArea
from gtm.db import Conn, maybe_one, one, tenant_tx
from gtm.util import json_sha256, sha256_hex

PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"

# Code-side policy versions. Bump one when the matching checks change behavior; the bump
# changes job fingerprints, so new runs re-check instead of reusing results made under old rules.
RESEARCH_POLICY_VERSION = "research_policy.v1"
TOOL_SCHEMA_VERSION = "research_tools.v1"
CHECK_POLICY_VERSION = "check_policy.v1"
EMAIL_POLICY_VERSION = "email_policy.v1"

ROLE_NAMES = ("researcher", "claim_check", "writer", "email_check")


class Cfg(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Tenant(Cfg):
    id: UUID
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,62}$")
    name: str
    kind: Literal["fictional", "real"]


class Brokerage(Cfg):
    name: str
    postal_address: str


class SenderFactConfig(Cfg):
    id: str = Field(pattern=r"^sender_fact_[0-9]+$")
    text: str = Field(max_length=200)
    use_in_outreach: bool


class Sender(Cfg):
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,62}$")
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
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,62}$")
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
    tenant: Tenant
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
    """The stored form: configuration plus pinned instructions and stage fingerprints."""

    prompts: dict[str, PromptRef]
    fingerprints: dict[str, str]


def load_config_file(path: Path) -> BrokerageConfig:
    data = yaml.safe_load(path.read_text())
    return BrokerageConfig.model_validate(data)


def read_prompt(version: str, prompts_dir: Path = PROMPTS_DIR) -> PromptRef:
    text = (prompts_dir / f"{version}.md").read_text()
    return PromptRef(version=version, sha256=sha256_hex(text), text=text)


def stage_fingerprints(cfg: BrokerageConfig, prompts: dict[str, PromptRef]) -> dict[str, str]:
    def role(name: str) -> dict[str, Any]:
        r = cfg.roles.get(name)
        return {
            "model_id": r.model_id,
            "provider": r.provider,
            "prompt_sha256": prompts[name].sha256,
        }

    campaign = cfg.campaign
    research = {
        "policy": RESEARCH_POLICY_VERSION,
        "tool_schema": TOOL_SCHEMA_VERSION,
        "role": role("researcher"),
        "tools": cfg.research_tools.model_dump(mode="json", exclude={"pricing"}),
        "limits": cfg.limits.research.model_dump(mode="json"),
        "service_area": campaign.service_area.model_dump(mode="json"),
        "need_categories": campaign.need_categories,
        "evidence_max_age_days": campaign.evidence_max_age_days,
    }
    check = {
        "policy": CHECK_POLICY_VERSION,
        "role": role("claim_check"),
        "limits": cfg.limits.check.model_dump(mode="json"),
        "service_area": campaign.service_area.model_dump(mode="json"),
        "need_categories": campaign.need_categories,
        "evidence_max_age_days": campaign.evidence_max_age_days,
        "qualification": cfg.qualification.model_dump(mode="json"),
    }
    write = {
        "policy": EMAIL_POLICY_VERSION,
        "writer": role("writer"),
        "email_check": role("email_check"),
        "limits": cfg.limits.write.model_dump(mode="json"),
        "sender": {
            "name": cfg.sender.name,
            "title": cfg.sender.title,
            "facts": [f.model_dump(mode="json") for f in cfg.sender.facts if f.use_in_outreach],
        },
        "brokerage": cfg.brokerage.name,
        "campaign": campaign.model_dump(
            mode="json",
            include={"offer", "cta", "tone_rules", "tone_reference", "max_words", "language"},
        ),
    }
    return {
        "research": json_sha256(research),
        "check": json_sha256(check),
        "write": json_sha256(write),
    }


def resolve(cfg: BrokerageConfig, prompts_dir: Path = PROMPTS_DIR) -> ResolvedConfig:
    prompts = {
        name: read_prompt(cfg.roles.get(name).prompt_version, prompts_dir) for name in ROLE_NAMES
    }
    return ResolvedConfig(
        **cfg.model_dump(), prompts=prompts, fingerprints=stage_fingerprints(cfg, prompts)
    )


def parse_stored(content: dict[str, Any]) -> ResolvedConfig:
    return ResolvedConfig.model_validate(content)


@dataclass(frozen=True)
class AppliedConfig:
    tenant_id: UUID
    campaign_id: UUID
    config_version_id: UUID
    version: int
    created: bool


def apply_config(
    conn: Conn, cfg: BrokerageConfig, prompts_dir: Path = PROMPTS_DIR
) -> AppliedConfig:
    """Upsert tenant, sender, mailbox, and campaign, then store a new version if content changed."""
    resolved = resolve(cfg, prompts_dir)
    content = resolved.model_dump(mode="json")
    content_sha = json_sha256(content)
    tenant = cfg.tenant

    with tenant_tx(conn, tenant.id):
        existing = maybe_one(conn, "SELECT slug FROM gtm.tenants WHERE id = %s", (tenant.id,))
        if existing is None:
            conn.execute(
                "INSERT INTO gtm.tenants (id, slug, name, kind) VALUES (%s, %s, %s, %s)",
                (tenant.id, tenant.slug, tenant.name, tenant.kind),
            )
        elif existing["slug"] != tenant.slug:
            raise ValueError("tenant slug cannot change for an existing tenant id")
        else:
            conn.execute("UPDATE gtm.tenants SET name = %s WHERE id = %s", (tenant.name, tenant.id))

        sender_id = one(
            conn,
            """INSERT INTO gtm.senders (tenant_id, slug, display_name) VALUES (%s, %s, %s)
               ON CONFLICT (tenant_id, slug) DO UPDATE SET display_name = EXCLUDED.display_name
               RETURNING id""",
            (tenant.id, cfg.sender.slug, cfg.sender.name),
        )["id"]
        conn.execute(
            """INSERT INTO gtm.mailboxes (tenant_id, address, mode) VALUES (%s, %s, %s)
               ON CONFLICT (tenant_id, address) DO NOTHING""",
            (tenant.id, cfg.mailbox.address, cfg.mailbox.mode),
        )
        mailbox_id = one(
            conn, "SELECT id FROM gtm.mailboxes WHERE address = %s", (cfg.mailbox.address,)
        )["id"]
        campaign_id = one(
            conn,
            """INSERT INTO gtm.campaigns (tenant_id, slug, sender_id, mailbox_id) VALUES (%s, %s, %s, %s)
               ON CONFLICT (tenant_id, slug) DO UPDATE
                 SET sender_id = EXCLUDED.sender_id, mailbox_id = EXCLUDED.mailbox_id, updated_at = now()
               RETURNING id""",
            (tenant.id, cfg.campaign.slug, sender_id, mailbox_id),
        )["id"]

        same = maybe_one(
            conn,
            "SELECT id, version FROM gtm.config_versions WHERE campaign_id = %s AND content_sha256 = %s",
            (campaign_id, content_sha),
        )
        if same is not None:
            return AppliedConfig(tenant.id, campaign_id, same["id"], same["version"], created=False)

        version = one(
            conn,
            "SELECT coalesce(max(version), 0) + 1 AS v FROM gtm.config_versions WHERE campaign_id = %s",
            (campaign_id,),
        )["v"]
        cv_id = one(
            conn,
            """INSERT INTO gtm.config_versions (tenant_id, campaign_id, version, content, content_sha256)
               VALUES (%s, %s, %s, %s, %s) RETURNING id""",
            (tenant.id, campaign_id, version, Jsonb(content), content_sha),
        )["id"]
        return AppliedConfig(tenant.id, campaign_id, cv_id, version, created=True)


def campaign_id_for_slug(conn: Conn, tenant_id: UUID, slug: str) -> UUID:
    with tenant_tx(conn, tenant_id):
        row = maybe_one(conn, "SELECT id FROM gtm.campaigns WHERE slug = %s", (slug,))
    if row is None:
        raise LookupError(f"campaign {slug!r} not found for this tenant")
    campaign_id: UUID = row["id"]
    return campaign_id
