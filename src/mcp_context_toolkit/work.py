"""Work items as validated Markdown records with a typed relationship graph.

Recall and lint read frontmatter files. Editing through ``apply_update`` or
``save_update`` validates the complete record before an atomic replacement and
refuses files with YAML comments, which a plain YAML round-trip would discard.
Work-item links resolve within the work store; hierarchy uses ``part_of``
rather than fixed levels.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import yaml
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

# Public (no leading underscore — cross-module import) because it must stay
# the SINGLE source of truth for "what work_recall excludes by default":
# `server/tools_work.py`'s `work_recall` MCP tool AND any second consumer
# (the Context Studio chat tool) call
# `WorkEngine.recall(..., exclude_kinds=RECALL_DEFAULT_EXCLUDED_KINDS)` —
# a changelog line is never "an existing item to reuse": changelog entries
# can crowd out the goal/step/note records this search exists to find. `decision` stays
# IN the pool on purpose — it answers exactly the W20/S8 "was this already
# considered?" question. A second copy of this literal in a second file
# would silently diverge the day one of them changes.
RECALL_DEFAULT_EXCLUDED_KINDS: frozenset[str] = frozenset({"changelog"})

# `_LINK_RE`/`_norm_link` (imported from core.py above) parse a [[name]] in a
# WORK item's prose exactly like memory.py's own links — same syntax, same
# normalization (padding/`.md`-suffix/kebab-case). What differs is the
# RESOLUTION target: a work-item [[name]] must resolve to a work KEY, never
# to a memory name (a prose [[...]] link to a memory would otherwise
# look like a dangling work reference because the stores are checked
# separately). A memory reference in work-item prose is written in plain text.


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
    # statt als Markdown-Text nach dem schliessenden `---`, regression review).
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
            category=_meta_get(meta, "category"),
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


class FrontmatterWriteError(Exception):
    """A work-item write cannot proceed safely: either the update fails
    WorkItem validation, or the source file's frontmatter carries a YAML
    comment that a plain `yaml.safe_dump` round-trip would silently drop
    (see `WorkEngine.apply_update`'s docstring)."""


# Canonical on-disk frontmatter field order for a WRITE — mirrors
# WorkItem.model_fields, English names only. A hand-authored file may use
# the German legacy aliases (`kanten`/`braucht_owner`/...) that `_meta_get`
# still reads (see `_parse_work_item`); a write through this path
# normalizes those away on save. That is a visible, not a silent, tradeoff
# — the diff preview (`apply_update`'s `old_text`/`new_text`) shows exactly
# this normalization before anyone confirms a save.
_FRONTMATTER_FIELD_ORDER = (
    "key", "title", "kind", "category", "status", "theme", "opened", "closed",
    "part_of", "amends", "supersedes", "blocked_by",
    "evidence", "reuse", "plan_path",
    "requires_owner", "unblocked_by", "migrated",
)


def _work_item_frontmatter_dict(item: WorkItem) -> dict:
    """Ordered dict of `item`'s frontmatter fields (everything except
    `body`/`source_path`, which are not frontmatter — `body` is the text
    after the closing `---`, `source_path` is derived from the real file
    path, never read from or written to disk)."""
    d: dict = {}
    for field in _FRONTMATTER_FIELD_ORDER:
        value = getattr(item, field)
        if field == "evidence":
            value = {"commits": list(value.commits), "files": list(value.files)}
        elif field == "reuse" and value is not None:
            value = {
                "searched_for": list(value.searched_for), "found": list(value.found),
                "conclusion": value.conclusion, "uses": list(value.uses),
                "extends": list(value.extends), "creates": list(value.creates),
                "modules": list(value.modules),
            }
        d[field] = value
    return d


def render_work_item_text(item: WorkItem) -> str:
    """Full markdown text (frontmatter + body) for `item` — what
    `WorkEngine.save_update` puts on disk. Pure function, no I/O: callers
    use this to build a diff preview before ever touching the filesystem."""
    fm = yaml.safe_dump(
        _work_item_frontmatter_dict(item), sort_keys=False, allow_unicode=True,
        default_flow_style=False,
    )
    return f"---\n{fm}---\n\n{item.body.rstrip()}\n"


def _raw_frontmatter_block(text: str) -> str | None:
    """The raw text BETWEEN the `---` delimiters, before any YAML parsing —
    same delimiter rule as `core.parse_frontmatter` (a closing line must
    `strip()` to exactly `---`), kept as a tiny local duplicate rather than
    changing that function's return shape for every other caller (memory.py
    included) just to hand back a second value only this write path needs."""
    if not text.startswith("---"):
        return None
    lines = text.splitlines()
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return None
    return "\n".join(lines[1:end])


def _has_frontmatter_comment(raw_frontmatter_text: str) -> bool:
    """True if the raw frontmatter contains a `#` YAML comment marker
    outside a quoted string. Deliberately conservative: a false positive
    only means the write is refused (safe, and the record stays editable
    by hand); a false negative would silently drop a real comment on save.
    Four refinements over a first, simpler draft, each caught by an
    independent ground-truth run against the real store (2026-08-22,
    `ruamel.yaml`'s own comment-attachment tracking, checked recursively
    over the whole loaded structure — not just the top-level mapping):

    1. A quote character only OPENS tracking at the start of a value
       (right after `key:`, after `- `, or as the first non-space
       character of a line) — a mid-word apostrophe like "Owner's
       decision" must NOT be mistaken for the start of a quoted scalar,
       or a real `#` comment later on that same line would wrongly read
       as "inside a quote" and never fire (a regression review —
       0 occurrences in the real store today, but latent).
    2. Quote state persists ACROSS lines, not reset per line — resetting
       per line flagged real files as a false positive: continuation
       lines of a genuinely multi-line quoted title, where the quote
       opened on an earlier line and a bare `#` inside that still-open
       string is not a comment at all.
    3. A `#` only starts a comment if it is the first character of the
       line or immediately preceded by whitespace, per the YAML spec — a
       `#` embedded in a word (`Backend#1`, `Lock-#1-Liefergrenze`) is a
       literal character, not a comment marker (regression review
       "Hinweis" — 3 real titles were needlessly refused without this).
    4. `''` inside a single-quoted scalar is YAML's escape for one literal
       `'`, NOT the end of the string — a doubled quote must be consumed
       as a pair and NEVER close `in_quote`. Without this, the first `'`
       of the pair closed tracking, the second could not reopen it
       (`at_value_start` stays `False` right after a close), and the
       scanner ran the REST of that multi-line scalar believing it was
       outside any quote — a later `#` in ordinary prose text (e.g.
       "Transition #4") then misread as a comment start. A regression case with a multi-line `searched_for` entry containing
       `''Status-Read''` exposed this false positive.

    Measured on the real store (2026-08-22, before refinement 4) after the
    first three: 15 of 5159 files carry a real comment that a plain
    `yaml.safe_load`/`dump` round-trip would drop — 0 false positives, 0
    false negatives against the ground-truth run. Roughly half are an
    `evidence.commits` entry explaining an intentionally-empty hash, the
    rest a commented-out S8 candidate list."""
    in_quote = None
    at_value_start = True
    prev_was_space = True  # start-of-text counts as "preceded by whitespace"
    n = len(raw_frontmatter_text)
    i = 0
    while i < n:
        ch = raw_frontmatter_text[i]
        if ch == "\n":
            if in_quote is None:
                at_value_start = True
            prev_was_space = True
            i += 1
            continue
        if in_quote:
            if ch == in_quote:
                # `''` inside a single-quoted scalar is one literal quote,
                # not the closing delimiter — consume the pair, stay open.
                if in_quote == "'" and i + 1 < n and raw_frontmatter_text[i + 1] == "'":
                    i += 2
                    prev_was_space = False
                    continue
                in_quote = None
            prev_was_space = False
            i += 1
            continue
        if ch in ("'", '"') and at_value_start:
            in_quote = ch
            at_value_start = False
            prev_was_space = False
            i += 1
            continue
        # A `#` only STARTS a comment per the YAML spec if it is the first
        # character of the line or immediately preceded by whitespace — a
        # `#` embedded in a word (`Backend#1`) is a literal character.
        # Without this check, titles containing an embedded # were needlessly
        # refused even though YAML treats that character as literal text when
        # it is not preceded by whitespace.
        if ch == "#" and prev_was_space:
            return True
        if ch in (" ", "\t"):
            prev_was_space = True
            i += 1
            continue
        prev_was_space = False
        at_value_start = ch in (":", "-")
        i += 1
    return False


class WorkEngine:
    """Loads work-item frontmatter files from one root and answers
    get/list/children/rollup/lint/apply_update/save_update. Single-tier
    (unlike Memory's project/user/core split) — work items are
    project-scoped by nature; a
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

    def apply_update(self, key: str, updates: dict) -> tuple[str, str, WorkItem]:
        """Validate an update to work item `key` WITHOUT writing anything.
        Returns `(old_text, new_text, the validated WorkItem)` — a caller
        builds a diff preview from the first two, or hands the third to
        `save_update` once a human confirms. Raises `KeyError` if `key`
        doesn't exist, `ValueError` if `updates` tries to change `key` or
        `source_path` (identity is immutable — a rename is a filesystem
        operation, out of this write path's scope; `source_path` is WORSE
        than immutable, it is the thing `save_update` writes TO — accepting
        it from `updates` would be a path-traversal write primitive, since
        `WorkItem`, unlike the rules/decisions models, is plain `BaseModel`
        with `extra="ignore"` rather than `_StrengesModell`'s `extra="forbid"`
        and would otherwise silently accept it), `ValueError` if `updates`
        names a field that is not a real `WorkItem` field (same "unknown
        key must be LOUD, not silently ignored" discipline `_parse_work_item`
        already applies to the read path, for the same reason — Pydantic's
        default `extra="ignore"` would otherwise swallow a typo'd field name
        without a trace), and `FrontmatterWriteError` if the merged fields
        fail WorkItem validation OR the source file's frontmatter carries a
        YAML comment this round-trip would silently drop."""
        if "key" in updates or "source_path" in updates:
            raise ValueError(
                "`key`/`source_path` cannot be set via updates — "
                "key via filesystem rename, source_path is derived, never user input"
            )
        unbekannt = sorted(set(updates) - set(WorkItem.model_fields))
        if unbekannt:
            raise ValueError(f"unknown field(s) in updates: {', '.join(unbekannt)}")
        current = self.get(key)
        if current is None:
            raise KeyError(key)
        path = Path(current.source_path)
        old_text = path.read_text(encoding="utf-8")
        raw_fm = _raw_frontmatter_block(old_text)
        if raw_fm is not None and _has_frontmatter_comment(raw_fm):
            raise FrontmatterWriteError(
                f"{key}: frontmatter carries a YAML comment this write path "
                f"cannot preserve — edit the file directly instead"
            )
        merged = current.model_dump()
        merged.update(updates)
        try:
            new_item = WorkItem(**merged)
        except ValidationError as e:
            raise FrontmatterWriteError(f"{key}: {e}") from e
        new_text = render_work_item_text(new_item)
        return old_text, new_text, new_item

    def save_update(self, key: str, updates: dict) -> WorkItem:
        """`apply_update` + an ATOMIC write (temp file + rename — a crash
        mid-write leaves either the old file or the new one, never a
        half-written one) + keeps this engine's in-memory state consistent
        with disk so a caller does not need to reload the whole directory
        after a single-item save."""
        _old_text, new_text, new_item = self.apply_update(key, updates)
        path = Path(new_item.source_path)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(new_text, encoding="utf-8")
        tmp.replace(path)
        idx = next(i for i, it in enumerate(self._items) if it.key == key)
        self._items[idx] = new_item
        self._by_key_cache = None
        self._children_cache = None
        self._bm25_cache = None
        return new_item

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
        the returned top-N. A regression review found that
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
        category: str | None = None,
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
        if category is not None:
            result = [i for i in result if i.category == category]
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
        used to be re-typed as prose into a legacy task JSON file next to
        an existing plan file; here it is counted.

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
          kind decision/changelog (a regression review): a
          migrated historical fact has no commit to point at by construction
          (the "evidence" concept describes work just claimed done, not a
          changelog line already true when it was written) — without the
          exclusion the 2377-card backfill buried the 78 real cases the
          counter exists to surface under 29x noise, the same reasoning
          already applied to `reuse_ungeprueft` in the work-status integration.
        - unclosed_pairs: keys where `closed` is set but status is not a
          closed one, or vice versa — a half-closed record.
        - plan_path_broken: {key: plan_path} where the referenced plan file
          does not exist. This is the ONLY link between the two stores, and
          nothing checked it — measured 2026-08-04: 8 records pointed at
          `plans/todo/...` files that a migration helper had moved to
          `plans/begonnen/`, while this very lint reported `dangling_refs: 0`.
          `dangling_refs` above deliberately only covers WORK KEYS; a filesystem
          path is a different kind of reference and simply had no checker.
        - reuse_not_checked: keys with status=active that carry no `reuse`
          block — work that is being done RIGHT NOW without a recorded
          reuse search. Excludes kind decision/changelog (the work-status integration):
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
          `work-link-check`): both were `[[name]]` links
          that actually pointed at a MEMORY, not a work item. Owner decision
          (2026-08-16): a work-item body `[[...]]` may only resolve to
          another work key — a memory reference belongs in the prose as
          plain text, never as `[[...]]`, so this check has no cross-store
          case to special-case and simply treats every non-resolving link as
          broken. Excludes kind decision/changelog (Etappe E): their body is
          VERBATIM migrated text, never written under this convention — a
          quoted `[[DRAFT_NOTE]]` placeholder or `` `[[link]]` `` mention
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
          This engine stays project-agnostic: the actual anchor lookup is
          INJECTED, never imported here. `symbol_check(typ, ziel) ->
          (zustand, hinweis)` lets the caller provide its own symbol
          inventory and control its cost; this lint pass does no I/O for
          it. `gesucht` entries are search terms, not anchors, so they
          are never checked for breakage.
        """
        by_key = self._by_key()
        dangling: dict[str, list[str]] = {}
        for i in self._items:
            # `entblockt_durch` gehoert hier dazu: es ist der Key eines Records,
            # der einen `plan_vorbehalt` aufhebt —
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

        # `decision`/`changelog` ausgenommen (the work-status integration, regression review
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
        # geschrieben. Ein "[[DRAFT_NOTE]]"-Platzhalter oder ein
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
                        if zustand not in ("ok", "geplant", "ungeprueft"):
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


def work_status_payload(lint: dict) -> dict:
    """Turn a `WorkEngine.lint()` result into the `work_status` counts +
    plain-language recommendation. Pulled out of `server/tools_work.py`'s
    `work_status()` closure so another consumer can reuse the
    exact same recommendation logic instead of re-deriving it — the S8
    lesson from the initial integration (a reimplementation of `_work_payload` drifted on 8
    of 13 edge cases) generalizes past just the payload-building functions.

    `load_errors` gets top billing in the recommendation, not just a tally:
    a dropped record can make an UNRELATED goal look falsely closeable (its
    only still-open child never made it into the tree), so "records were
    dropped" is at least as urgent as "goals are closeable" — arguably
    more, since it can be the CAUSE of a wrong `closeable_goals` reading."""
    closeable = len(lint["closeable_goals"])
    dangling = len(lint["dangling_refs"])
    no_evidence = len(lint["missing_evidence"])
    half_closed = len(lint["unclosed_pairs"])
    dropped = len(lint["load_errors"])
    # Die zwei juengeren Pruefungen MUSSEN mitzaehlen. Sonst stand
    # `reuse_not_checked: 130` neben der Empfehlung "kleine Reste" —
    # genau die Zahl, fuer die der Store umgebaut wurde, fehlte in
    # seinem eigenen Statusbericht (Reviewer-Fund 2026-08-04).
    plan_tot = len(lint.get("plan_path_broken", {}))
    ungeprueft = len(lint.get("reuse_not_checked", []))
    total_issues = (closeable + dangling + no_evidence + half_closed
                    + dropped + plan_tot + ungeprueft)
    if dropped:
        recommendation = "🚨 Records wurden beim Laden verworfen — Zahlen oben ggf. unvollstaendig"
    elif closeable or dangling or plan_tot:
        recommendation = "🧹 Aufraeumen faellig"
    elif ungeprueft:
        recommendation = (f"🔎 {ungeprueft} Records arbeiten ohne aufgezeichnete "
                          f"Reuse-Suche (`reuse:` fehlt)")
    elif total_issues:
        recommendation = "💡 kleine Reste"
    else:
        recommendation = "✅ kein dringender Bedarf"
    return {
        "total_items": lint["total"],
        "closeable_goals": closeable,
        "dangling_refs": dangling,
        "missing_evidence": no_evidence,
        "unclosed_pairs": half_closed,
        "plan_path_broken": plan_tot,
        "reuse_not_checked": ungeprueft,
        "load_errors": dropped,
        "recommendation": recommendation,
    }
