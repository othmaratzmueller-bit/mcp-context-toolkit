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

from pathlib import Path

from pydantic import ValidationError

from mcp_context_toolkit.core import (
    KnowledgeLoadError,
    bm25_scores,
    build_bm25_index,
    iter_files,
    parse_frontmatter,
)
from mcp_context_toolkit.models import (
    WorkEdges, WorkEvidence, WorkItem, WorkStatus,
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


def _parse_work_item(path: Path) -> WorkItem:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:  # pragma: no cover - filesystem edge
        raise KnowledgeLoadError(path, f"read failed: {e}") from e
    meta, body = parse_frontmatter(text)

    evidence_raw = _meta_get(meta, "evidence") or {}
    if not isinstance(evidence_raw, dict):
        evidence_raw = {}

    # `kanten` bleibt None, wenn der Block fehlt ODER leer ist — beides heisst
    # "nie gesucht". Ein leerer Block darf NICHT als gesuchte-und-nichts-
    # gefunden durchgehen, sonst genuegt ein `kanten: {}` um das Gate zu
    # umgehen, und der Unterschied, um den es geht, waere wieder weg.
    kanten_raw = _meta_get(meta, "kanten")
    kanten = None
    if isinstance(kanten_raw, dict):
        kandidat = WorkEdges(
            gesucht=_als_liste(kanten_raw.get("gesucht"), "kanten.gesucht", path),
            nutzt=_als_liste(kanten_raw.get("nutzt"), "kanten.nutzt", path),
            erweitert=_als_liste(kanten_raw.get("erweitert"), "kanten.erweitert", path),
            baut=_als_liste(kanten_raw.get("baut"), "kanten.baut", path),
            modul=_als_liste(kanten_raw.get("modul"), "kanten.modul", path),
        )
        kanten = kandidat if kandidat.gesucht_worden() else None

    try:
        return WorkItem(
            key=str(_meta_get(meta, "key") or path.stem),
            title=str(_meta_get(meta, "title") or "").strip(),
            kind=_meta_get(meta, "kind") or "goal",
            status=_meta_get(meta, "status") or "open",
            theme=_meta_get(meta, "theme"),
            opened=_meta_get(meta, "opened"),
            closed=_meta_get(meta, "closed"),
            part_of=_meta_get(meta, "part_of"),
            amends=_meta_get(meta, "amends"),
            supersedes=_meta_get(meta, "supersedes"),
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
            kanten=kanten,
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
        zolltarif failure mode this store exists to catch. ``self._errors`` is
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

    def recall(self, query: str, limit: int = 8) -> list[WorkItem]:
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
        """
        bases = bm25_scores(self._bm25_index(), query)
        if not bases:
            return []

        by_key = self._by_key()
        scored = [(base, by_key[key]) for key, base in bases.items() if base > 0]
        scored.sort(key=lambda s: (-s[0], s[1].key))

        # Same relative floor as memory.py — see _RECALL_FLOOR_RATIO.
        if scored:
            floor = scored[0][0] * _RECALL_FLOOR_RATIO
            scored = [s for s in scored if s[0] >= floor]
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
    ) -> list[WorkItem]:
        result = self._items
        if theme is not None:
            result = [i for i in result if i.theme == theme]
        if status is not None:
            result = [i for i in result if i.status == status]
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
        zieht und die Zustellung zur Wand wird, die man ueberblaettert.
        """
        ziel = file_path.strip().lstrip("./")
        if not ziel:
            return []
        treffer: list[tuple[int, WorkItem]] = []
        for i in self._items:
            if i.status not in ("open", "active"):
                continue
            # Kanten zuerst: praeziser (Symbol-Ebene) und ausdruecklich vorwaerts.
            rang = None
            if i.kanten is not None:
                for anker in (i.kanten.nutzt + i.kanten.erweitert + i.kanten.baut):
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
        return [i for _r, i in treffer[:limit]]

    def lint(self) -> dict:
        """Read-only hygiene report — mirrors memory.py's ``lint()`` shape so
        both stores are consumed the same way, but every rule here maps to a
        NAMED actor (see the plan's section 2: a finding without an executing
        party is inert, however correct — that is precisely how the
        zolltarif-live-test-politur case sat un-actioned since 2026-07-02).

        - dangling_refs: {key: [missing target keys]} for part_of/amends/
          supersedes/blocked_by pointing at a key that does not exist.
        - closeable_goals: goal keys whose children are ALL done/dropped/
          superseded but the goal itself is still open/active — the exact
          "empfohlen zu schliessen, niemand tat es" failure mode.
        - missing_evidence: keys with status=done and no commits/files in
          evidence — a closed item that asserts rather than shows.
        - unclosed_pairs: keys where `closed` is set but status is not a
          closed one, or vice versa — a half-closed record.
        - plan_path_broken: {key: plan_path} where the referenced plan file
          does not exist. This is the ONLY link between the two stores, and
          nothing checked it — measured 2026-08-04: 8 records pointed at
          `plans/todo/...` files that the plan-keeper had moved to
          `plans/begonnen/`, while this very lint reported `dangling_refs: 0`.
          `dangling_refs` above deliberately only covers WORK KEYS; a filesystem
          path is a different kind of reference and simply had no checker.
        - reuse_ungeprueft: keys with status=active that carry no `kanten`
          block — work that is being done RIGHT NOW without a recorded
          reuse search. Only `active`, not `open`: an open item has not
          reached the moment where the question changes anything, and listing
          all 878 open items would drown the ones that matter.
        - load_errors: files skipped at the last load (invalid schema,
          duplicate key) — surfaced here, not just at startup, because a
          dropped record silently corrupts closeable_goals/rollup above: a
          goal whose only still-open child failed to load looks fully
          closeable. See WorkEngine.errors / load_directory's docstring.
        """
        by_key = self._by_key()
        dangling: dict[str, list[str]] = {}
        for i in self._items:
            missing = [
                ref for ref in (i.part_of, i.amends, i.supersedes)
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

        reuse_ungeprueft = [
            i.key for i in self._items
            if i.status == "active" and i.kanten is None
        ]

        return {
            "total": len(self._items),
            "dangling_refs": dangling,
            "closeable_goals": sorted(closeable),
            "missing_evidence": sorted(missing_evidence),
            "unclosed_pairs": sorted(unclosed_pairs),
            "plan_path_broken": plan_path_broken,
            "reuse_ungeprueft": sorted(reuse_ungeprueft),
            "load_errors": list(self._errors),
        }
