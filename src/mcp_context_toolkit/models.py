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


# ---------------------------------------------------------------------------
# Work items — plans, roadmaps, in-flight vs. superseded ideas.
#
# Same lesson as Decision (which went eight weeks without a single write): a
# store survives only if something forces writes, and it stays legible only
# if status is a FIELD rather than a folder move. Prose lives in `body`
# (frontmatter files, like Memory) rather than as YAML strings — plans are
# ~70% prose by line count, unlike Rules/Decisions which are mostly structure.
# ---------------------------------------------------------------------------

WorkKind = Literal["goal", "step", "note"]
WorkStatus = Literal["open", "active", "done", "dropped", "superseded"]

# Closed vocabulary on purpose (see the Memory `type: package` lesson: 73 of
# 119 real memories carried a type the schema didn't know, and both readers
# silently degraded to "misc" instead of surfacing the gap loudly). Extend
# this list deliberately when a new theme is needed — never let an unknown
# value pass through un-flagged.
WorkTheme = Literal[
    "engine", "prometheus", "chat", "kb-ingest", "coder", "themis",
    "infra", "workflow", "abas", "mail", "daidalos", "security", "misc",
]


class WorkEvidence(BaseModel):
    """What backs a `done` status — commits and/or files, so a closed item is
    verifiable rather than asserted. Optional: a `note`/`open` item has none."""
    commits: list[str] = []
    files: list[str] = []


class WorkEdges(BaseModel):
    """What ALREADY EXISTS that this item uses, extends or will build.

    Deliberately NOT the same thing as `WorkEvidence`, and the difference is
    the whole point:

        evidence  BACKWARD — what this work touched, filled when closing
        kanten    FORWARD  — what was found BEFORE building, filled when planning

    Without the forward half, the reuse question lands at the end again, where
    it can only produce regret. Anchors are ``pfad/zur/datei.py::Symbol`` —
    never line numbers, those are wrong two edits later.

    `gesucht` is the load-bearing field, not the edge lists. It records WHAT was
    searched for, and it is what separates the two states that look identical
    from the outside:

        no `kanten` block at all        -> never searched
        `gesucht` set, all lists empty  -> searched, found nothing (honest)

    Without that distinction a mandatory `kanten` field would simply be filled
    with invented edges — the field is cheap to fake, the search is not.
    """
    gesucht: list[str] = []      # the queries/terms actually searched for
    nutzt: list[str] = []        # exists, will be called
    erweitert: list[str] = []    # exists, will be changed
    baut: list[str] = []         # does not exist yet, this item creates it
    modul: list[str] = []        # module/directory names, placeholders allowed

    def gesucht_worden(self) -> bool:
        """Was a search recorded at all? An empty `kanten:` block is not one."""
        return bool(self.gesucht or self.nutzt or self.erweitert
                    or self.baut or self.modul)


class WorkItem(BaseModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    title: str
    kind: WorkKind = "goal"
    status: WorkStatus = "open"
    theme: WorkTheme

    opened: date
    closed: date | None = None

    # Structural relationships — this IS the hierarchy (no fixed levels; see
    # the plan's rationale). A `step` sets `part_of` to its `goal`'s key.
    part_of: str | None = None
    amends: str | None = None       # points at the record this supersedes-in-place
    supersedes: str | None = None   # points at the idea this replaces outright
    blocked_by: list[str] = []

    evidence: WorkEvidence = WorkEvidence()
    # Reuse-Suche VOR dem Bauen. None = nie gesucht (siehe WorkEdges).
    kanten: WorkEdges | None = None
    plan_path: str | None = None    # the pre-existing plan file this was cut from, if any

    body: str = ""
    source_path: str | None = None
