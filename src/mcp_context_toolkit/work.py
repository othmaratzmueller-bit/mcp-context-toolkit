"""Work items — plans, roadmaps, in-flight vs. superseded ideas.

Same on-disk shape as memory (a directory of frontmatter+markdown files),
different relationships: memory is recalled by relevance and links freely
in prose; work items form a small typed graph (`part_of` / `amends` /
`supersedes` / `blocked_by`) that a lint pass can walk mechanically. Read-only
— this engine never writes; a migration agent or a human does.

Built after two failures observed in the pre-existing stores this replaces:

- `.claude/decisions/` (395 records) went eight weeks without a single write.
  It had a read path (`query_decisions_for_file`, engine.py) and nothing that
  forced a write. `supersedes` exists on the model and was used exactly ONCE —
  everywhere else someone wrote "ACHTUNG — AMENDMENT, bitte VOR dem Rest
  lesen" as prose into `reason` instead of using the field.
- `.todo.json` (126 items) grew a single item to 21,758 characters — 13
  chronological "NACHTRAG" paragraphs stacked in one string field — DESPITE
  already linking to a 902-line plan file that covered the same ground.
  Measured across the whole file: 96 of 126 items already name a plan file,
  and 21 of those still carry >3,000 characters of duplicated prose next to
  it. Two places claiming the same truth drift; that duplication is exactly
  where the store's "todo says open, code says built" contradictions came
  from.

Hierarchy is NOT three fixed levels (master file -> sub file -> item): the
median work item measured on the real backlog has ZERO sub-points, while a
handful have 30-60+. Fixed levels would force ~126 near-empty "master files"
for items that will only ever be one record. Instead hierarchy is a relation
(`part_of`) that a small item simply never uses.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from pydantic import ValidationError

from mcp_context_toolkit.core import LINK_RE as _LINK_RE
from mcp_context_toolkit.core import (
    KnowledgeLoadError,
    bm25_scores,
    build_bm25_index,
    clamp_limit,
    iter_files,
    parse_frontmatter,
)
from mcp_context_toolkit.core import norm_link as _norm_link
from mcp_context_toolkit.models import (
    WorkEdges,
    WorkEvidence,
    WorkItem,
    WorkStatus,
)

# Statuses that count as "resolved" for an x/y summary — done AND dropped both
# leave the open count, but are reported separately (see WorkEngine.rollup):
# "7 of 24 dropped" says something about the idea, "10 of 24 done" only about
# pace. Collapsing them into one number was the exact complaint that motivated
# this store.
_CLOSED_STATUSES: frozenset[WorkStatus] = frozenset({"done", "dropped", "superseded"})

# Recall field weights — same BM25F shape as memory.py, different fields: a
# work item has no tags/description, just a title and a prose body. Title
# hits count more heavily; searching for a plan by its subject should surface
# the item named after it before one that merely mentions it in passing.
_W_TITLE, _W_BODY = 3.0, 1.0

# Same relevance-floor rationale as memory.py's _RECALL_FLOOR_RATIO: without
# it, a query with only one strong match still drags in the entire weak tail
# (anything sharing even one common word), which is unusable once a store
# grows past a handful of items.
_RECALL_FLOOR_RATIO = 0.25

# `_LINK_RE`/`_norm_link` (imported from core.py above) parse a [[name]] in a
# WORK item's prose exactly like memory.py's own links — same syntax, same
# normalization (padding/`.md`-suffix/kebab-case). What differs is the
# RESOLUTION target: a work-item [[name]] must resolve to a work KEY, never
# to a memory name (Owner-decision 2026-08-16, work-record
# work_lint_prueft_prosa_links_nicht — a Prosa-[[...]] pointing at a memory
# read exactly like a dangling work reference and neither `dangling_refs`
# above nor memory.py's `broken_links` ever checked it, because each store
# only walks its own tree). A memory reference in work-item prose is written
# out in plain text instead.


def _meta_get(meta: dict, key: str):
    """Read a frontmatter key top-level, falling back to a nested ``metadata:``
    block. Mirrors memory.py's ``_meta_get`` — same two coexisting frontmatter
    styles in this store family, kept consistent on purpose."""
    if meta.get(key) is not None:
        return meta[key]
    nested = meta.get("metadata")
    if isinstance(nested, dict):
        return nested.get(key)
    return None


def _als_liste(wert, feld: str, path: Path) -> list:
    """YAML-Wert -> Liste, aber ein String ist ein FEHLER, keine Zeichenliste.

    `list("anonymize PII")` ergibt 13 Einzelbuchstaben. Bei `evidence`/`blocked_by`
    ist das haesslich; bei `kanten.gesucht` ist es fail-OPEN: der vergessene
    Bindestrich (`gesucht: anonymize PII` statt `gesucht:\n  - anonymize PII`)
    fuellt das Feld mit 13 Tokens, das Reuse-Gate gilt als erfuellt, und weder
    Loader noch Lint sagen etwas. Genau der Zustand, den der Entwurf ausschliesst:
    Feld gefuellt, Suche nie passiert. (Reviewer-Fund 2026-08-04.)

    Deshalb hier LAUT statt tolerant — ein Tippfehler soll den Record in
    `load_errors` schieben, nicht stillschweigend ein Gate oeffnen.
    """
    if wert is None:
        return []
    if isinstance(wert, str):
        raise KnowledgeLoadError(
            path, f"`{feld}` ist ein String ({wert!r}) statt einer Liste — "
                  f"fehlt ein `- ` vor dem Wert?")
    if not isinstance(wert, (list, tuple)):
        raise KnowledgeLoadError(
            path, f"`{feld}` ist {type(wert).__name__} statt einer Liste")
    return list(wert)


def _als_text(wert, feld: str, path: Path) -> str:
    """YAML-Wert -> String, aber eine Liste/Map ist ein FEHLER, kein `str()`.

    Gegenstueck zu `_als_liste` fuer die Textfelder, und aus demselben Grund:
    `str(["Neubau", "Modul X erweitern"])` ergibt die Zeichenkette
    `"['Neubau', 'Modul X erweitern']"` — eine Python-repr, die als Nachweis
    ausgeliefert wird und wie ein Satz aussieht, ohne einer zu sein. Kein
    Ladefehler, kein Lint-Treffer, nur eine kleine Unwahrheit ueber die Form.

    Das ist genau die stille Toleranz, gegen die `_als_liste` daneben gebaut
    wurde. Sie war in `kanten.entscheidung` wieder eingezogen, weil ein
    `str(...)` beim Schreiben harmlos aussieht. (Reviewer-Fund 2026-08-11.)
    """
    if wert is None:
        return ""
    if not isinstance(wert, str):
        raise KnowledgeLoadError(
            path, f"`{feld}` ist {type(wert).__name__} statt eines Textes — "
                  f"mehrere Punkte gehoeren in EINEN Satz, nicht in eine Liste")
    return wert.strip()


def _parse_work_item(path: Path) -> WorkItem:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:  # pragma: no cover - filesystem edge
        raise KnowledgeLoadError(path, f"read failed: {e}") from e
    meta, body = parse_frontmatter(text)

    # Unbekannte Top-Level-Schluessel LAUT machen — dieselbe Bauart wie der
    # kanten-Zweig unten, eine Ebene hoeher. Der Loader liest die WorkItem-
    # Felder NAMENTLICH auf; ein `extra="forbid"` am Model wuerde hier NICHT
    # greifen, weil Unbekanntes das Model nie erreicht. Ohne diese Pruefung
    # verschwindet ein vertippter oder erfundener Schluessel spurlos — genau
    # der Fund von 2026-08-11 (`status_hinweis:` auf WorkItem-Ebene, kein
    # Fehler, kein Lint-Treffer, das Feld war einfach weg). `source_path`
    # gehoert zu `WorkItem.model_fields`, wird aber nie aus dem Frontmatter
    # gelesen (immer der echte Pfad) — als Frontmatter-Schluessel erlaubt
    # (89 Alt-Records tragen ein redundantes, wirkungsloses `source_path:`),
    # nur eben folgenlos. `body` dagegen NICHT erlaubt, trotz gleicher
    # Modellzugehoerigkeit: ein Frontmatter-`body:` waere kein Echo des
    # echten Inhalts wie bei `source_path`, sondern ERSETZTE ihn lautlos —
    # exakt die 108-Dateien-Klasse aus v1.4.314 (`body: |` als YAML-Key
    # statt als Markdown-Text nach dem schliessenden `---`, W8-Reviewer-Fund).
    # `metadata` ist der Container fuer den zweiten Frontmatter-Stil (siehe
    # `_meta_get`), kein WorkItem-Feld.
    LEGACY_TOP_ALIASES = {"kanten", "braucht_owner", "entblockt_durch", "aus_migration"}
    erlaubt_top = (set(WorkItem.model_fields) - {"body"}) | {"metadata"} | LEGACY_TOP_ALIASES
    unbekannt_top = sorted(set(meta) - erlaubt_top)
    if unbekannt_top:
        raise KnowledgeLoadError(
            path,
            f"unbekannte Schluessel im Frontmatter: {', '.join(unbekannt_top)} — "
            f"erlaubt sind {', '.join(sorted(erlaubt_top))}. "
            f"Ein unbekannter Schluessel wird sonst still verworfen."
        )

    evidence_raw = _meta_get(meta, "evidence") or {}
    if not isinstance(evidence_raw, dict):
        evidence_raw = {}

    # `reuse` / `kanten` bleibt None, wenn der Block fehlt ODER leer ist — beides heisst
    # "nie gesucht". Ein leerer Block darf NICHT als gesuchte-und-nichts-
    # gefunden durchgehen, sonst genuegt ein `reuse: {}` um das Gate zu
    # umgehen, und der Unterschied, um den es geht, waere wieder weg.
    kanten_raw = _meta_get(meta, "reuse") if _meta_get(meta, "reuse") is not None else _meta_get(meta, "kanten")
    kanten = None
    if isinstance(kanten_raw, dict):
        # Unbekannte Schluessel LAUT machen. Erlaubte Namen aus dem Model ableiten
        # sowie abwärtskompatible deutsche Alt-Namen zuschalten.
        erlaubt = set(WorkEdges.model_fields) | {"gesucht", "gefunden", "entscheidung", "nutzt", "erweitert", "baut", "modul"}
        unbekannt = sorted(set(kanten_raw) - erlaubt)
        if unbekannt:
            raise KnowledgeLoadError(
                path,
                f"unbekannte Schluessel unter `reuse:` / `kanten:`: {', '.join(unbekannt)} — "
                f"erlaubt sind {', '.join(sorted(erlaubt))}. "
                f"Ein unbekannter Schluessel wird sonst still verworfen."
            )
        blk = "reuse" if _meta_get(meta, "reuse") is not None else "kanten"
        sf_key = "searched_for" if "searched_for" in kanten_raw else "gesucht"
        fnd_key = "found" if "found" in kanten_raw else "gefunden"
        conc_key = "conclusion" if "conclusion" in kanten_raw else "entscheidung"
        uses_key = "uses" if "uses" in kanten_raw else "nutzt"
        ext_key = "extends" if "extends" in kanten_raw else "erweitert"
        crt_key = "creates" if "creates" in kanten_raw else "baut"
        mod_key = "modules" if "modules" in kanten_raw else "modul"

        kandidat = WorkEdges(
            searched_for=_als_liste(kanten_raw.get(sf_key), f"{blk}.{sf_key}", path),
            found=_als_liste(kanten_raw.get(fnd_key), f"{blk}.{fnd_key}", path),
            conclusion=_als_text(kanten_raw.get(conc_key), f"{blk}.{conc_key}", path),
            uses=_als_liste(kanten_raw.get(uses_key), f"{blk}.{uses_key}", path),
            extends=_als_liste(kanten_raw.get(ext_key), f"{blk}.{ext_key}", path),
            creates=_als_liste(kanten_raw.get(crt_key), f"{blk}.{crt_key}", path),
            modules=_als_liste(kanten_raw.get(mod_key), f"{blk}.{mod_key}", path),
        )
        kanten = kandidat if kandidat.searched() else None

    # `theme`/`opened` haben (anders als `kind`/`status`) kein `or`-Fallback —
    # ohne diese zwei Schranken bleibt ihr Typ fuer Pyright `Unknown | None`
    # (aus `_meta_get`s letztem Zweig `return None`), obwohl beide Felder am
    # Model REQUIRED sind. Die Schranken narrowen den Typ UND liefern eine
    # klarere Meldung als Pydantics generische ValidationError fuer ein
    # fehlendes Pflichtfeld (Q12-Naehe: in derselben Funktion angefasst).
    theme_wert = _meta_get(meta, "theme")
    if theme_wert is None:
        raise KnowledgeLoadError(path, "`theme` fehlt")
    opened_wert = _meta_get(meta, "opened")
    if opened_wert is None:
        raise KnowledgeLoadError(path, "`opened` fehlt")

    try:
        return WorkItem(
            key=str(_meta_get(meta, "key") or path.stem),
            title=str(_meta_get(meta, "title") or "").strip(),
            kind=_meta_get(meta, "kind") or "goal",
            status=_meta_get(meta, "status") or "open",
            theme=theme_wert,
            opened=opened_wert,
            closed=_meta_get(meta, "closed"),
            part_of=_meta_get(meta, "part_of"),
            amends=_meta_get(meta, "amends"),
            supersedes=_meta_get(meta, "supersedes"),
            requires_owner=bool(_meta_get(meta, "requires_owner") or _meta_get(meta, "braucht_owner") or False),
            unblocked_by=_meta_get(meta, "unblocked_by") or _meta_get(meta, "entblockt_durch"),
            migrated=bool(_meta_get(meta, "migrated") or _meta_get(meta, "aus_migration") or False),
            # Auch hier `_als_liste`: `evidence: commits: abc123` (ohne `- `)
            # wuerde sonst zu sechs Buchstaben und `missing_evidence`
            # faelschlich befriedigen — dieselbe Klasse wie bei `kanten`, nur
            # mit leiserer Folge. Der Helfer war ohnehin da (Reviewer-Rest,
            # Runde 2).
            blocked_by=_als_liste(_meta_get(meta, "blocked_by"), "blocked_by", path),
            evidence=WorkEvidence(
                commits=_als_liste(evidence_raw.get("commits"),
                                   "evidence.commits", path),
                files=_als_liste(evidence_raw.get("files"), "evidence.files", path),
            ),
            reuse=kanten,
            plan_path=_meta_get(meta, "plan_path"),
            body=body,
            source_path=str(path),
        )
    except ValidationError as e:
        raise KnowledgeLoadError(path, str(e)) from e


class WorkEngine:
    """Loads work-item frontmatter files from one root and answers
    get/list/children/rollup/lint. Read-only, single-tier (unlike Memory's
    project/user/core split) — work items are project-scoped by nature; a
    cross-project "how do I generally plan" tier was not asked for and would
    be pure speculation (see W20 — ask before inventing)."""

    def __init__(self) -> None:
        self._items: list[WorkItem] = []
        self._root: Path | None = None
        self._by_key_cache: dict[str, WorkItem] | None = None
        self._children_cache: dict[str, list[str]] | None = None
        self._bm25_cache: dict | None = None
        self._errors: list[str] = []

    @classmethod
    def from_directory(cls, root: str | Path, *, strict: bool = False) -> WorkEngine:
        """``strict=False`` (default): a malformed file is skipped, its error
        appended to ``load_directory``'s return dict AND kept on the engine
        (see ``errors``) — matches Memory/Rules' lenient serving path (one
        broken record must not blank the whole store), but a caller that only
        keeps the engine (every production caller — ``mcp_server.py`` never
        keeps ``load_directory``'s return dict) must still be able to see
        that something was dropped. ``strict=True`` (tests, migration
        validation, CI) raises on the first bad file instead of silently
        vanishing it — this store's own stated principle is to fail loud, and
        a caller that wants that guarantee needs a way to actually ask for
        it."""
        engine = cls()
        engine.load_directory(root, strict=strict)
        return engine

    def load_directory(self, root: str | Path, *, strict: bool = False) -> dict:
        """Load one root. Unlike MemoryEngine this REPLACES rather than
        accumulates on a second call — work items have no multi-tier
        precedence concept, so there is nothing to accumulate onto.

        A dropped record is not a cosmetic gap: ``lint()``'s ``closeable_goals``
        counts a goal's CURRENT children, so a goal whose only still-open
        child failed to load looks fully closeable — the exact inverted
        failure mode this store exists to catch. ``self._errors`` is
        therefore kept on the engine (not just returned here), so every reader
        of this store — ``work_lint``, ``work_status``, the startup banner —
        can surface "N records dropped" instead of silently ranking against an
        incomplete tree.
        """
        root_path = Path(root).expanduser()
        if not root_path.exists():
            raise FileNotFoundError(f"Work directory not found: {root_path}")
        self._root = root_path

        items: list[WorkItem] = []
        seen: set[str] = set()
        errors: list[str] = []
        for md in iter_files(root_path, ".md"):
            try:
                item = _parse_work_item(md)
            except KnowledgeLoadError as e:
                if strict:
                    raise
                errors.append(str(e))
                continue
            if item.key in seen:
                if strict:
                    raise KnowledgeLoadError(md, f"duplicate key: {item.key}")
                errors.append(f"{md}: duplicate key '{item.key}', skipped")
                continue
            seen.add(item.key)
            items.append(item)

        self._items = items
        self._by_key_cache = None
        self._children_cache = None
        self._bm25_cache = None
        self._errors = errors
        return {"added": len(items), "root": str(root_path), "errors": errors}

    @property
    def errors(self) -> list[str]:
        """Errors from the last ``load_directory`` call (skipped/invalid
        files, duplicate keys) — empty when nothing was dropped. Survives
        regardless of whether the caller went through ``from_directory`` or
        ``load_directory`` directly (see the load-error visibility rationale
        on ``load_directory``)."""
        return list(self._errors)

    @property
    def items(self) -> list[WorkItem]:
        return list(self._items)

    def _by_key(self) -> dict[str, WorkItem]:
        if self._by_key_cache is None:
            self._by_key_cache = {i.key: i for i in self._items}
        return self._by_key_cache

    def get(self, key: str) -> WorkItem | None:
        return self._by_key().get(key)

    def recall(
        self, query: str, limit: int = 8,
        exclude_kinds: frozenset[str] = frozenset(),
    ) -> list[WorkItem]:
        """Rank work items by keyword overlap with the query — same BM25F
        ranking as MemoryEngine.recall(), via the shared core.bm25_scores (see
        core.py's module docstring for why this lives in one place: the
        double-counting bug found in the 2026-07-26 review would otherwise
        have to be independently avoided, and independently re-broken, in two
        files).

        No frecency/backlink boost, unlike memory: those are usage- and
        link-graph-derived signals nothing in this store's design asked for
        (W20 — ask before inventing). If a caller later needs a "hot work
        item" concept it can be added the same way memory's was — a {key:
        factor} map layered on the base score, not a change to this method's
        core ranking.

        `exclude_kinds` drops matching items from the CANDIDATE POOL, before
        the floor-ratio cut and the `limit` slice — not a post-hoc filter on
        the returned top-N. Etappe D (W8-Reviewer-Fund ERROR-2, 2026-08-20):
        `work_recall` is the tool reuse-search and planning agents use
        to find an EXISTING capability before building a new one; after the
        changelog backfill (2377 new `kind: changelog` cards), a query for
        real functionality returned up to 8/8 changelog hits and zero of the
        actual sibling records — a historical changelog line is never "an
        existing item to reuse". A post-hoc filter on the top-`limit` results
        would still under-fill (measured: some queries had 0 of 8 survive);
        excluding before the cut keeps the requested `limit` meaningful.

        `limit` is clamped via `core.clamp_limit` — an LLM-supplied tool
        parameter is not trusted input, and a bare `scored[:limit]` treats
        a negative `limit` as a valid, reversed Python slice (the same gap
        docs.py's recall() had before it was clamped).
        """
        bases = bm25_scores(self._bm25_index(), query)
        if not bases:
            return []

        by_key = self._by_key()
        scored = [(base, by_key[key]) for key, base in bases.items() if base > 0]
        if exclude_kinds:
            scored = [s for s in scored if s[1].kind not in exclude_kinds]
        scored.sort(key=lambda s: (-s[0], s[1].key))

        # Same relative floor as memory.py — see _RECALL_FLOOR_RATIO.
        if scored:
            floor = scored[0][0] * _RECALL_FLOOR_RATIO
            scored = [s for s in scored if s[0] >= floor]
        limit = clamp_limit(limit)
        return [i for _, i in scored[:limit]]

    def _bm25_index(self) -> dict:
        """Inverted index for BM25F scoring (built once, cached; invalidated
        on every load_directory). Title counts _W_TITLE times, body once —
        see core.build_bm25_index for the index shape and core.bm25_scores
        for how it's ranked."""
        if self._bm25_cache is None:
            fields_by_key: dict[str, list[tuple[str, float]]] = {
                i.key: [(i.title, _W_TITLE), (i.body, _W_BODY)]
                for i in self._items
            }
            self._bm25_cache = build_bm25_index(fields_by_key)
        return self._bm25_cache

    def list(
        self,
        *,
        theme: str | None = None,
        status: str | None = None,
        kind: str | None = None,
        include_archived: bool = False,
    ) -> list[WorkItem]:
        """Default view is the LIVE backlog — `done`/`dropped`/`superseded`
        (`_CLOSED_STATUSES`) are excluded unless `status` names one of them
        explicitly or `include_archived=True` is passed. Closed items are
        never deleted or moved (see module docstring — this engine never
        writes); they stay findable via an explicit `status=` filter or
        `include_archived=True`, just not in the "what's open" default that
        callers reach for most often."""
        result = self._items
        if theme is not None:
            result = [i for i in result if i.theme == theme]
        if status is not None:
            result = [i for i in result if i.status == status]
        elif not include_archived:
            result = [i for i in result if i.status not in _CLOSED_STATUSES]
        if kind is not None:
            result = [i for i in result if i.kind == kind]
        return sorted(result, key=lambda i: i.key)

    def _children_map(self) -> dict[str, list[str]]:
        """``part_of`` parent key -> [child keys], cached."""
        if self._children_cache is None:
            m: dict[str, list[str]] = {}
            for i in self._items:
                if i.part_of:
                    m.setdefault(i.part_of, []).append(i.key)
            self._children_cache = m
        return self._children_cache

    def children(self, key: str) -> list[WorkItem]:
        """Direct children of a goal via ``part_of`` — the hierarchy is this
        relation, not a folder or a fixed level."""
        by_key = self._by_key()
        return [by_key[k] for k in self._children_map().get(key, []) if k in by_key]

    def rollup(self, key: str) -> dict:
        """x/y summary for one goal, computed from its children's status —
        NEVER stored, so it cannot drift from what the children actually say.
        This is the direct fix for the measured duplication: a goal's progress
        used to be re-typed as prose into .todo.json next to an existing plan
        file; here it is counted.

        Returns {total, open, active, done, dropped, superseded} — every
        status gets its own count, none collapsed into another. `active` used
        to be folded into `open` here; that quietly repeated, one level down,
        the exact kind of collapse this store exists to stop doing (done vs.
        dropped vs. superseded must not be one "resolved" number — the same
        argument applies to open vs. active).
        """
        kids = self.children(key)
        counts = {"total": len(kids), "open": 0, "active": 0, "done": 0,
                  "dropped": 0, "superseded": 0}
        for k in kids:
            if k.status == "open":
                counts["open"] += 1
            elif k.status == "active":
                counts["active"] += 1
            elif k.status in _CLOSED_STATUSES:
                counts[k.status] += 1
        return counts

    def fuer_datei(self, file_path: str, limit: int = 8) -> list[WorkItem]:
        """Deprecated alias for `for_file()` — kept for existing Python callers."""
        return self.for_file(file_path, limit=limit)

    def for_file(self, file_path: str, limit: int = 8) -> list[WorkItem]:
        """Offene Work-Items, die diese Datei nennen — fuer die Zustellung beim Edit.

        Aus "hier sind deine Regeln" wird damit "hier sind deine Regeln, die
        Entscheidungen dazu, und die N offenen Punkte, die jemand fuer diese Datei
        notiert hat".

        ZWEI QUELLEN, bewusst kein drittes Feld
        ---------------------------------------
        ``kanten.nutzt/erweitert/baut``  VORWAERTS — vor dem Bauen gesucht, `datei::Symbol`
        ``evidence.files``               eigentlich rueckwaerts, aber an einem OFFENEN
                                         Record faktisch eine Vorwaerts-Aussage: es ist,
                                         was der Autor anzufassen erwartete

        Gemessen 2026-08-04 ueber 1114 offene/aktive Records: 21 mit `kanten`
        (51 Anker), 374 mit `evidence.files` (496 aufloesbare Pfade, 214 tote).
        Ein eigenes ``applies_to.files`` waere am ersten Tag genauso leer wie
        `kanten` — und eine vierte Pflegestelle fuer dieselbe Auskunft.

        Nur `open`/`active`: ein geschlossener Punkt ist keine Arbeit mehr.
        Deckel bei `limit`, weil eine einzelne Datei sonst zweistellig viele Items
        zieht und die Zustellung zur Wand wird, die man ueberblaettert. `limit`
        laeuft durch `core.clamp_limit` — derselbe Grund wie bei `recall()`
        oben: ein negativer Wert ist sonst ein gueltiger, umgekehrter Slice.
        """
        ziel = file_path.strip().lstrip("./")
        if not ziel:
            return []
        treffer: list[tuple[int, WorkItem]] = []
        for i in self._items:
            if i.status not in ("open", "active"):
                continue
            # Reuse-Kanten zuerst: praeziser (Symbol-Ebene) und ausdruecklich vorwaerts.
            rang = None
            if i.reuse is not None:
                for anker in (i.reuse.uses + i.reuse.extends + i.reuse.creates):
                    if anker.split("::", 1)[0].strip().lstrip("./") == ziel:
                        rang = 0
                        break
            if rang is None:
                for f in i.evidence.files:
                    if f.strip().rstrip("/").lstrip("./") == ziel:
                        rang = 1
                        break
            if rang is not None:
                treffer.append((rang, i))
        # Kanten vor Evidence, danach aktive vor offenen, dann juengste zuerst.
        treffer.sort(key=lambda t: (t[0], 0 if t[1].status == "active" else 1,
                                    -t[1].opened.toordinal()))
        limit = clamp_limit(limit)
        return [i for _, i in treffer[:limit]]

    def lint(
        self, symbol_check: Callable[[str, str], tuple[str, str | None]] | None = None,
    ) -> dict:
        """Read-only hygiene report — mirrors memory.py's ``lint()`` shape so
        both stores are consumed the same way, but every rule here maps to a
        NAMED actor (see the plan's section 2: a finding without an executing
        party is inert, however correct — that is precisely how a real
        finding sat un-actioned).

        - dangling_refs: {key: [missing target keys]} for part_of/amends/
          supersedes/blocked_by/entblockt_durch pointing at a key that does
          not exist.
        - closeable_goals: goal keys whose children are ALL done/dropped/
          superseded but the goal itself is still open/active — the exact
          "empfohlen zu schliessen, niemand tat es" failure mode.
        - missing_evidence: keys with status=done and no commits/files in
          evidence — a closed item that asserts rather than shows. Excludes
          kind decision/changelog (Etappe D, W8-Reviewer-Fund WARNING-4): a
          migrated historical fact has no commit to point at by construction
          (the "evidence" concept describes work just claimed done, not a
          changelog line already true when it was written) — without the
          exclusion the 2377-card backfill buried the 78 real cases the
          counter exists to surface under 29x noise, the same reasoning
          already applied to `reuse_ungeprueft` in Etappe B.
        - unclosed_pairs: keys where `closed` is set but status is not a
          closed one, or vice versa — a half-closed record.
        - plan_path_broken: {key: plan_path} where the referenced plan file
          does not exist. This is the ONLY link between the two stores, and
          nothing checked it — measured 2026-08-04: 8 records pointed at
          `plans/todo/...` files that the plan-keeper had moved to
          `plans/begonnen/`, while this very lint reported `dangling_refs: 0`.
          `dangling_refs` above deliberately only covers WORK KEYS; a filesystem
          path is a different kind of reference and simply had no checker.
        - reuse_not_checked: keys with status=active that carry no `reuse`
          block — work that is being done RIGHT NOW without a recorded
          reuse search. Excludes kind decision/changelog (Etappe B):
          `reuse` is a FORWARD field, a migrated historical fact never
          searched before its own existence. Only `active`, not `open`: an
          open item has not reached the moment where the question changes
          anything, and listing all 878 open items would drown the ones
          that matter.
        - load_errors: files skipped at the last load (invalid schema,
          duplicate key) — surfaced here, not just at startup, because a
          dropped record silently corrupts closeable_goals/rollup above: a
          goal whose only still-open child failed to load looks fully
          closeable. See WorkEngine.errors / load_directory's docstring.
        - body_link_broken: {key: [missing [[targets]]]} for `[[name]]`
          references in the Markdown BODY that resolve to no work key.
          `dangling_refs` above only walks the structured fields
          (part_of/amends/supersedes/blocked_by); a `[[...]]` written into
          the prose reads exactly like a cross-record reference but was
          checked by nobody — not this lint's own structured-field pass, and
          not memory.py's `broken_links` either (that walks the memory tree
          only). Two real cases surfaced this (2026-08-15/16, work-record
          `work_lint_prueft_prosa_links_nicht`): both were `[[name]]` links
          that actually pointed at a MEMORY, not a work item. Owner decision
          (2026-08-16): a work-item body `[[...]]` may only resolve to
          another work key — a memory reference belongs in the prose as
          plain text, never as `[[...]]`, so this check has no cross-store
          case to special-case and simply treats every non-resolving link as
          broken. Excludes kind decision/changelog (Etappe E): their body is
          VERBATIM migrated text, never written under this convention — a
          quoted `[[TALOS_THOUGHT]]` placeholder or `` `[[link]]` `` mention
          from an old changelog line reads like a cross-record reference but
          is prose about code, same reasoning as the missing_evidence/
          reuse_ungeprueft exclusions above.
        - broken_reuse_refs: {key: [{kind, target, state, hint}, ...]} for
          `uses`/`extends`/`creates`/`modules` anchors whose state is not
          "ok" or "geplant" (the two healthy states, see `symbol_check`
          below). ``None`` when no `symbol_check` was passed, an empty dict
          when it WAS passed and found nothing — the two must stay
          distinguishable, same reasoning as `searched_for` set vs. no
          `reuse` block at all (never let "not checked" read as "checked,
          clean").
          This engine stays project-agnostic (no `scripts/`, no TALOS
          knowledge) by construction, so the actual anchor lookup is
          INJECTED, never imported here — same pattern as the tokenizer in
          capability.py. `symbol_check(typ, ziel) -> (zustand, hinweis)`;
          the caller closes over its own symbol inventory (in TALOS:
          `scripts/symbol_inventory.py` + `scripts/plan_graph.py::_zustand`)
          and decides how expensive building it is — this lint pass itself
          does zero I/O for it. `gesucht` entries are never checked: they
          are search terms, not anchors, and cannot "break" (Work-Record
          `work_kanten_ohne_ankerpruefung`).
        """
        by_key = self._by_key()
        dangling: dict[str, list[str]] = {}
        for i in self._items:
            # `entblockt_durch` gehoert hier dazu (Etappe B, W8-Reviewer-Fund
            # WARNING-2): es ist der Key eines Records, der einen
            # `plan_vorbehalt` aufhebt (siehe scripts/naechste_arbeit.py) —
            # ohne diese Pruefung hebt JEDER truthy String, auch ein
            # Tippfehler, den Owner-Riegel lautlos auf. Dieselbe Fehlerklasse,
            # fuer die `plan_path_broken` unten gebaut wurde.
            missing = [
                ref for ref in (i.part_of, i.amends, i.supersedes, i.unblocked_by)
                if ref and ref not in by_key
            ]
            missing += [b for b in i.blocked_by if b not in by_key]
            if missing:
                dangling[i.key] = missing

        closeable: list[str] = []
        for i in self._items:
            if i.kind != "goal" or i.status not in ("open", "active"):
                continue
            kids = self.children(i.key)
            if kids and all(k.status in _CLOSED_STATUSES for k in kids):
                closeable.append(i.key)

        missing_evidence = [
            i.key for i in self._items
            if i.status == "done" and not (i.evidence.commits or i.evidence.files)
            and i.kind not in ("decision", "changelog")
        ]

        unclosed_pairs = [
            i.key for i in self._items
            if (i.closed is not None) != (i.status in _CLOSED_STATUSES)
        ]

        # Gegen den Dateibaum, nicht gegen die Key-Tabelle: `plan_path` ist ein
        # Pfad. Relativ zur Wurzel des Work-Stores hoch (…/.claude/work -> …/),
        # weil die Pfade so im Frontmatter stehen (`.claude/plans/...`).
        plan_path_broken: dict[str, str] = {}
        if self._root is not None:
            # `.resolve()`: `load_directory` macht nur `expanduser()`. Gemessen
            # 2026-08-04 mit vier Aufrufformen — bei einem RELATIVEN Store-Pfad
            # aus einem Unterverzeichnis (`../work` aus `.claude/engine`) meldete
            # diese Pruefung 1563 Falschtreffer statt 0, lautlos. Heute
            # unerreichbar (`_discover_work_dir` baut absolut), ueber
            # `CONTEXT_WORK_DIR` aber jederzeit erreichbar.
            projekt = Path(self._root).resolve().parent.parent
            for i in self._items:
                if i.plan_path and not (projekt / i.plan_path).is_file():
                    plan_path_broken[i.key] = i.plan_path

        # `decision`/`changelog` ausgenommen (Etappe B, W8-Reviewer-Fund
        # WARNING-4): `kanten` ist ein VORWAERTS-Feld, eine migrierte
        # historische Tatsache hat vor ihrem eigenen Entstehen nichts gesucht.
        # Dieselbe Ausnahme steht bereits in der Migrations-Doku fuer die
        # anlegenden Agenten — ohne sie widersprechen sich Doku und
        # Engine-Lint ab dem ersten migrierten Record (Etappe C).
        reuse_not_checked = [
            i.key for i in self._items
            if i.status == "active" and i.reuse is None
            and i.kind not in ("decision", "changelog")
        ]

        # Same resolution as memory.py's broken_links: report the RAW
        # matched text (so the record author sees exactly what they typed),
        # but resolve through `_norm_link` (strips padding/`.md`, folds
        # kebab->snake) so `[[ foo ]]`/`[[foo.md]]`/`[[foo-bar]]` all match
        # an existing `foo`/`foo_bar` key. Reviewer-fund (2026-08-16): the
        # first cut compared raw text and flagged those three spellings as
        # broken even though the target key existed. Only the TARGET side is
        # normalized, not `by_key` itself (memory.py normalizes both) — safe
        # because WorkItem.key is schema-enforced to `^[a-z][a-z0-9_]*$`
        # (models.py), so a key never carries the padding/`.md`/kebab-case
        # that norm_link would need to fold; a memory NAME has no such
        # constraint, which is why memory.py's own check normalizes both
        # sides.
        # `decision`/`changelog` ausgenommen (Etappe E, gefunden beim ersten
        # Lauf gegen den frisch geflossenen Changelog-Backfill): ihr Body ist
        # VERBATIM migrierter Alttext, nie unter der `[[name]]`-Konvention
        # geschrieben. Ein "[[TALOS_THOUGHT]]"-Platzhalter oder ein
        # "`[[link]]`-Kanten" in Anfuehrungszeichen aus dem Original-Changelog
        # sieht syntaktisch wie ein Cross-Record-Verweis aus, ist aber Prosa
        # ueber Code, kein Work-Store-Bezug. Gleiche Ausnahme-Logik wie
        # missing_evidence/reuse_ungeprueft oben, dieselbe Begruendung.
        body_link_broken: dict[str, list[str]] = {}
        for i in self._items:
            if i.kind in ("decision", "changelog"):
                continue
            targets = sorted(set(_LINK_RE.findall(i.body)))
            missing = [t for t in targets if _norm_link(t) not in by_key]
            if missing:
                body_link_broken[i.key] = missing

        # `None` (not run) vs. `{}` (run, nothing broken) is deliberate — see
        # the docstring entry above. Only computed when the caller injects a
        # checker, so a plain `WorkEngine.lint()` stays as cheap as before.
        broken_reuse_refs: dict[str, list[dict]] | None = None
        if symbol_check is not None:
            broken_reuse_refs = {}
            for i in self._items:
                if i.reuse is None:
                    continue
                funde = []
                for typ, ziele in (
                    ("uses", i.reuse.uses),
                    ("extends", i.reuse.extends),
                    ("creates", i.reuse.creates),
                    ("modules", i.reuse.modules),
                ):
                    for ziel in ziele:
                        zustand, hinweis = symbol_check(typ, ziel)
                        if zustand not in ("ok", "geplant"):
                            funde.append({
                                "kind": typ, "target": ziel,
                                "state": zustand, "hint": hinweis,
                            })
                if funde:
                    broken_reuse_refs[i.key] = funde

        return {
            "total": len(self._items),
            "dangling_refs": dangling,
            "closeable_goals": sorted(closeable),
            "missing_evidence": sorted(missing_evidence),
            "unclosed_pairs": sorted(unclosed_pairs),
            "plan_path_broken": plan_path_broken,
            "reuse_not_checked": sorted(reuse_not_checked),
            "body_link_broken": body_link_broken,
            "broken_reuse_refs": broken_reuse_refs,
            "load_errors": list(self._errors),
        }
