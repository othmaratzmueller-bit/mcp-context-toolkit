from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

RuleType = Literal[
    "security",
    "workflow",
    "code_quality",
    "frontend",
    "architecture",
    "infrastructure",
    "module",
]
RuleScope = Literal["backend", "frontend", "database", "infrastructure", "docs", "all"]
RulePriority = Literal["non_negotiable", "mandatory", "recommended"]
RuleLanguage = Literal["python", "javascript", "css", "html", "yaml", "sql"]
# Which layer a rule was loaded from. 'project' = the repo-local rule set
# (CONTEXT_RULES_DIR), 'shared' = the org grundregeln (CONTEXT_SHARED_RULES_DIR).
# Default 'project' keeps single-tier callers byte-identical. Mirrors MemoryTier.
RuleTier = Literal["shared", "project", "unknown"]


class RuleApplyTo(BaseModel):
    files: list[str] = Field(min_length=1)
    language: RuleLanguage | None = None
    excludes: list[str] = []
    triggers: list[str] = []


class RuleComplianceRef(BaseModel):
    standard: str
    clause: str | None = None
    article: str | None = None
    section: str | None = None
    description: str


class RuleConflict(BaseModel):
    rule: str
    resolution: str = Field(min_length=10)


class RuleExample(BaseModel):
    code: str
    why: str


class RuleExamples(BaseModel):
    forbidden: RuleExample | None = None
    required: RuleExample | None = None


class RuleReferenceFile(BaseModel):
    path: str
    why: str


class RuleReferences(BaseModel):
    files: list[RuleReferenceFile] = []
    related_rules: list[str] = []


class Rule(BaseModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    title: str
    short_id: str | None = None

    type: RuleType
    scope: RuleScope
    priority: RulePriority
    modules: list[str] = Field(min_length=1)

    summary: str = Field(min_length=10)
    content: str

    applies_to: RuleApplyTo
    examples: RuleExamples | None = None
    references: RuleReferences | None = None
    compliance_refs: list[RuleComplianceRef] = []
    conflicts_with: list[RuleConflict] = []

    tags: list[str] = []
    owner: str | None = None
    created: date
    last_reviewed: date | None = None
    review_interval_days: int | None = None

    tier: RuleTier = "project"
    source_path: str | None = None


DecisionStatus = Literal["draft", "accepted", "rejected", "superseded", "deprecated"]


class DecisionAppliesTo(BaseModel):
    modules: list[str] = []
    files: list[str] = []


class Decision(BaseModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    title: str
    date: date
    status: DecisionStatus

    applies_to: DecisionAppliesTo
    reason: str = Field(min_length=10)

    supersedes: str | None = None
    source_path: str | None = None
