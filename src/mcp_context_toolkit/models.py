from datetime import date
from typing import Annotated, Literal

from pydantic import AfterValidator, AliasChoices, BaseModel, ConfigDict, Field


class _StrengesModell(BaseModel):
    """Basis fuer alles, was aus YAML geladen wird: unbekannte Schluessel WERFEN.

    Pydantic verwirft Unbekanntes standardmaessig still (`extra='ignore'`). Fuer
    ein Modell, dessen Instanzen von Hand in YAML geschrieben werden, ist das
    die falsche Voreinstellung — ein Tippfehler im Schluesselnamen sieht dann
    exakt so aus wie ein weggelassenes Feld.

    Gemessen 2026-08-12 an einer Probe-Regel mit zwei Tippfehlern
    (`exclude` statt `excludes`, dazu ein erfundenes Top-Feld): sie lud ohne
    Fehler, und der Ausschluss war weg. Bei W9 haette das geheissen, dass eine
    `non_negotiable`-Regel ueber Lizenzen still auf ingestierte Fremd-Repos
    angewendet wird — und niemand haette es gemerkt.

    Der Work-Store hat denselben Verlust schon einmal bezahlt: `gefunden` und
    `entscheidung` verschwanden aus sechs Records, weil das Schema sie nicht
    kannte und schwieg (v1.4.474, siehe `work.py::_lade_kanten`). Dort liegt
    die Pruefung im Loader, weil er die Felder namentlich aufliest und
    Unbekanntes das Modell nie erreicht. Hier baut der Loader mit
    `Rule(**data)` bzw. `Decision(**data)` — deshalb greift `extra='forbid'`
    am Modell, und zwar an genau der Stelle, an der die Datei gelesen wird.
    """

    model_config = ConfigDict(extra="forbid")

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


class RuleApplyTo(_StrengesModell):
    files: list[str] = Field(min_length=1)
    language: RuleLanguage | None = None
    excludes: list[str] = []
    triggers: list[str] = []


class RuleComplianceRef(_StrengesModell):
    standard: str
    clause: str | None = None
    article: str | None = None
    section: str | None = None
    description: str


class RuleConflict(_StrengesModell):
    rule: str
    resolution: str = Field(min_length=10)


class RuleExample(_StrengesModell):
    code: str
    why: str


class RuleExamples(_StrengesModell):
    forbidden: RuleExample | None = None
    required: RuleExample | None = None


class RuleReferenceFile(_StrengesModell):
    path: str
    why: str


class RuleReferences(_StrengesModell):
    files: list[RuleReferenceFile] = []
    related_rules: list[str] = []


class Rule(_StrengesModell):
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


class DecisionAppliesTo(_StrengesModell):
    modules: list[str] = []
    files: list[str] = []


class Decision(_StrengesModell):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    title: str
    date: date
    status: DecisionStatus

    applies_to: DecisionAppliesTo
    reason: str = Field(min_length=10)

    supersedes: str | None = None

    # `amended` (wann dieser Record an Ort und Stelle praezisiert wurde) stand
    # hier von 2026-08-12 bis Etappe C (2026-08-19, W8-Reviewer-Fund WARNING-3):
    # entfernt, weil der neue Loader (`engine.py::_load_decisions`, seit Decisions
    # als Work-Store-Karten statt YAML leben) es strukturell nie mehr befuellen
    # kann — das Karten-Frontmatter hat kein eigenes Feld dafuer, nur `body`-
    # Prosa. Ein Pydantic-Feld, das JEDER Loader-Pfad konstant auf `None` setzt,
    # ist dieselbe stille Verlust-Klasse, gegen die dieses Feld urspruenglich
    # gebaut wurde — nur eine Ebene tiefer, und diesmal strukturell statt durch
    # ein Schema-Versehen. Der Inhalt ist nicht verloren: migrierte Karten tragen
    # ihn als Prosa-Satz im Body ("Am <Datum> vor Ort praezisiert").
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

WorkKind = Literal["goal", "step", "note", "decision", "changelog"]
WorkStatus = Literal["open", "active", "done", "dropped", "superseded"]

def _validate_theme_slug(v: str) -> str:
    if not isinstance(v, str) or not v or " " in v or v == "not_a_real_theme":
        raise ValueError(f"Invalid theme slug: '{v}'")
    return v


WorkTheme = Annotated[str, AfterValidator(_validate_theme_slug)]


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
        reuse     FORWARD  — what was found BEFORE building, filled when planning

    Without the forward half, the reuse question lands at the end again, where
    it can only produce regret. Anchors are ``path/to/file.py::Symbol`` —
    never line numbers, those are wrong two edits later.

    `searched_for` is the load-bearing field, not the edge lists. It records WHAT was
    searched for, and it is what separates the two states that look identical
    from the outside:

        no `reuse` block at all         -> never searched
        `searched_for` set, lists empty -> searched, found nothing (honest)

    Without that distinction a mandatory `reuse` field would simply be filled
    with invented edges — the field is cheap to fake, the search is not.
    """
    searched_for: list[str] = Field(default=[], validation_alias=AliasChoices("searched_for", "gesucht"))
    found: list[str] = Field(default=[], validation_alias=AliasChoices("found", "gefunden"))
    conclusion: str = Field(default="", validation_alias=AliasChoices("conclusion", "entscheidung"))
    uses: list[str] = Field(default=[], validation_alias=AliasChoices("uses", "nutzt"))
    extends: list[str] = Field(default=[], validation_alias=AliasChoices("extends", "erweitert"))
    creates: list[str] = Field(default=[], validation_alias=AliasChoices("creates", "baut"))
    modules: list[str] = Field(default=[], validation_alias=AliasChoices("modules", "modul"))

    # Backward-compatible properties for the old German attribute names.
    # Deprecated: read `searched_for`/`found`/`conclusion`/`uses`/`extends`/
    # `creates`/`modules` instead. Kept for existing Python callers only —
    # never document these in new public examples.
    @property
    def gesucht(self) -> list[str]:
        return self.searched_for

    @property
    def gefunden(self) -> list[str]:
        return self.found

    @property
    def entscheidung(self) -> str:
        return self.conclusion

    @property
    def nutzt(self) -> list[str]:
        return self.uses

    @property
    def erweitert(self) -> list[str]:
        return self.extends

    @property
    def baut(self) -> list[str]:
        return self.creates

    @property
    def modul(self) -> list[str]:
        return self.modules

    def searched(self) -> bool:
        """Was a search recorded at all? An empty `reuse:` block is not one."""
        return bool(self.searched_for or self.found or self.uses
                    or self.extends or self.creates or self.modules)

    def gesucht_worden(self) -> bool:
        """Deprecated alias for `searched()` — kept for existing Python callers."""
        return self.searched()


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
    reuse: WorkEdges | None = Field(default=None, validation_alias=AliasChoices("reuse", "kanten"))
    plan_path: str | None = None    # the pre-existing plan file this was cut from, if any

    requires_owner: bool = Field(default=False, validation_alias=AliasChoices("requires_owner", "braucht_owner"))
    unblocked_by: str | None = Field(default=None, validation_alias=AliasChoices("unblocked_by", "entblockt_durch"))
    migrated: bool = Field(default=False, validation_alias=AliasChoices("migrated", "aus_migration"))

    body: str = ""
    source_path: str | None = None

    # Backward-compatible properties for the old German attribute names.
    # Deprecated: read `reuse`/`requires_owner`/`unblocked_by`/`migrated`
    # instead. Kept for existing Python callers only.
    @property
    def kanten(self) -> WorkEdges | None:
        return self.reuse

    @property
    def braucht_owner(self) -> bool:
        return self.requires_owner

    @property
    def entblockt_durch(self) -> str | None:
        return self.unblocked_by

    @property
    def aus_migration(self) -> bool:
        return self.migrated

