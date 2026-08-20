from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

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

    Three of these fields spell out the search itself — what was looked for,
    what turned up, what was concluded:

        gesucht      WHAT was searched for
        gefunden     WHAT turned up, INCLUDING hits that were rejected
        entscheidung WHAT followed from it

    `gefunden` is deliberately separate from `nutzt`/`erweitert`/`baut`, and the
    reason is the rejected hit. "Mixin pattern found, discarded — no class here"
    belongs in none of the three: it is not used, not extended, not built. Yet
    that is the most valuable part of a reuse record, because it proves the
    search was not only run but weighed. Without a home for it, the honest
    answer has to be dropped or dressed up as something it isn't.
    """
    gesucht: list[str] = []      # the queries/terms actually searched for
    gefunden: list[str] = []     # what the search turned up, rejects included
    entscheidung: str = ""       # what followed from it, in one sentence
    nutzt: list[str] = []        # exists, will be called
    erweitert: list[str] = []    # exists, will be changed
    baut: list[str] = []         # does not exist yet, this item creates it
    modul: list[str] = []        # module/directory names, placeholders allowed

    def gesucht_worden(self) -> bool:
        """Was a search recorded at all? An empty `kanten:` block is not one.

        `entscheidung` alone does NOT count. It is a free-text sentence and thus
        the cheapest field here to fill without doing anything — letting it open
        the gate would hand back exactly the loophole `kanten: {}` was closed
        for. `gefunden` does count: a hit list is the result of a search.
        """
        return bool(self.gesucht or self.gefunden or self.nutzt
                    or self.erweitert or self.baut or self.modul)


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

    # Beide Felder HUMAN-ONLY: nur der Owner setzt sie, nie ein Agent (talos-
    # plan-keeper, talos-work-migrator) — ein Agent, der sich selbst von einer
    # Owner-Pflicht befreien koennte, waere derselbe Fehler wie ein Zyklus, der
    # `suppressions.md` schreiben darf. Ersetzt/ergaenzt die bisherige Textsuche
    # in `scripts/naechste_arbeit.py` (BRAUCHT_MENSCHEN/PLAN_VORBEHALT_AUFGEHOBEN)
    # nur fuer NEUE Records — der Altbestand bleibt bei der Textsuche.
    braucht_owner: bool = False
    entblockt_durch: str | None = None  # Key des Records/der Entscheidung, die einen Plan-Vorbehalt aufhebt

    # Vom Migrator gesetzt, nie vom Owner erwartet (anders als die zwei Felder
    # oben): macht das plan-keeper-Reuse-Gate (13e, `opened >= 2026-08-04`)
    # robust GEGEN die Konvention, auf der es bisher allein ruhte — `opened`
    # traegt bei einem migrierten Record das Datum der URSPRUENGLICHEN Idee,
    # nicht des Migrationslaufs, und nur DESHALB faellt der Altbestand unter
    # die Grenze. Kippte ein kuenftiger Migrator diese unausgesprochene
    # Konvention (stempelte `opened` mit dem Lauf-Datum), waeren alle
    # migrierten Records ploetzlich UEBER der Grenze — das Gate wuerde genau
    # die Records blockieren, fuer deren Freigabe es gebaut wurde, und zwar
    # STILL: kein Fehler, kein Lint-Treffer (Work-Record `work_migrator_
    # opened_stempel_traegt_das_gate`, Reviewer-Beobachtung 2026-08-05). Das
    # Gate prueft seither BEIDE: `opened < 2026-08-04 ODER aus_migration`.
    aus_migration: bool = False

    body: str = ""
    source_path: str | None = None
