"""Shared primitives for knowledge stores (rules + memory + work items).

Rules, memory and work items are the same shape on disk — a directory of
files with YAML frontmatter that grows and changes over time. This module
holds the genuinely shared, content-type-agnostic helpers so a new store type
does not re-invent them (memory and work items both reuse the BM25F ranking
below, rather than each carrying their own copy).

Also home to store-directory conventions and discovery helpers shared between
the MCP server and the CLI — kept here so they stay in sync by construction
rather than by comment ("keep in sync with …").
"""

from __future__ import annotations

import bisect
import math
import os
import re
from collections.abc import Iterator
from pathlib import Path

import yaml

# Store-directory conventions tried during walk-up auto-discovery, in order.
# ``.context`` is the generic default (matches the tool/env naming); ``.claude``
# is kept as a fallback for existing Claude Code repos. Overridable via
# CONTEXT_STORE_CONVENTIONS (comma-separated, e.g. ".acme,.context,.claude")
# so an embedding product can brand its store dir without forking the engine.
_DEFAULT_STORE_CONVENTIONS = (".context", ".claude")


class KnowledgeLoadError(Exception):
    """A knowledge file (rule or memory) failed to parse or validate."""

    def __init__(self, file: Path, errors: str):
        super().__init__(f"{file}: {errors}")
        self.file = file
        self.errors = errors


def iter_files(root: Path, suffix: str) -> Iterator[Path]:
    """Yield files ending in ``suffix`` under ``root``, sorted, skipping any
    path whose parts start with ``_`` (meta dirs like ``_meta``/``_flushed``)."""
    for path in sorted(root.rglob(f"*{suffix}")):
        if any(part.startswith("_") for part in path.relative_to(root).parts):
            continue
        yield path


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split markdown into ``(frontmatter mapping, body)``.

    Frontmatter is a leading block delimited by lines containing only ``---``.
    Returns ``({}, text)`` when no well-formed frontmatter is present. Never
    raises on bad YAML — returns ``({}, text)`` so a single malformed file is
    degraded, not fatal (memory is best-effort, unlike the strict rule schema).
    """
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return {}, text
    try:
        meta = yaml.safe_load("\n".join(lines[1:end])) or {}
    except yaml.YAMLError:
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    body = "\n".join(lines[end + 1:]).lstrip("\n")
    return meta, body


# ``[[name]]`` cross-references in a record's prose body — used by both
# memory.py (links resolve to another memory) and work.py (links resolve to
# another work key). Pulled here after they diverged: work.py originally
# carried its own copy of just the regex without `norm_link`, so a padded
# `[[ key ]]` or a `.md`-suffixed target resolved in memory bodies but not in
# work bodies for the exact same input — two regexes that read identically
# but behaved differently is precisely the drift `core.py` exists to prevent
# (see the module docstring). Owner-decision 2026-08-16 (work-record
# `work_lint_prueft_prosa_links_nicht`): a work-item body `[[...]]` may only
# resolve to another WORK key, never a memory — so the two consumers share
# the parsing/normalization, not the resolution target.
LINK_RE = re.compile(r"\[\[([^\]]+)\]\]")


def norm_link(target: str) -> str:
    """Normalize a ``[[link]]`` target for resolution: drop a trailing
    ``.md`` and fold kebab-case to snake_case, so links in mixed styles (old
    prod kebab vs. dev snake, or a target typed with a `.md` suffix) all
    resolve to the same canonical key."""
    target = target.strip()
    if target.endswith(".md"):
        target = target[:-3]
    return target.replace("-", "_")


# ---------------------------------------------------------------------------
# Store-directory conventions + discovery — shared between MCP server and CLI
# ---------------------------------------------------------------------------


def store_conventions() -> tuple[str, ...]:
    """Return the ordered store-directory conventions to try during walk-up
    auto-discovery. Defaults to ``(".context", ".claude")``; overridable via
    the ``CONTEXT_STORE_CONVENTIONS`` env var (comma-separated)."""
    raw = os.environ.get("CONTEXT_STORE_CONVENTIONS", "").strip()
    if not raw:
        return _DEFAULT_STORE_CONVENTIONS
    parts = tuple(p.strip() for p in raw.split(",") if p.strip())
    return parts or _DEFAULT_STORE_CONVENTIONS


def discover_shared_rules_dir() -> Path | None:
    """Locate the optional SHARED (org) rules tier. Env-only
    (``CONTEXT_SHARED_RULES_DIR``), NO walk-up — the shared tier is an explicit
    deployment choice (org grundregeln mounted read-only), never auto-found in a
    parent dir the way the project tier is. Returns ``None`` when unset."""
    env = os.environ.get("CONTEXT_SHARED_RULES_DIR")
    if not env:
        return None
    p = Path(env).expanduser().resolve()
    return p if p.is_dir() else None


def discover_work_dir() -> Path | None:
    """Locate the PROJECT work-item store. Env ``CONTEXT_WORK_DIR`` wins, else
    walk up for ``<convention>/work``. Returns ``None`` when absent — the work
    tools and the per-file delivery are then simply not offered.

    Lives HERE and not in a caller because both the MCP server and the CLI need
    it, and they needed it in slightly different words: the server had one copy,
    the CLI grew a second on 2026-08-05 that differed only in a `.resolve()`.
    Found by the W8 reviewer — in the very sprint that removed the duplicate
    detector, and missed by the write-time scanner because it listens to
    Edit/Write while the file was written through a shell heredoc. The third
    member of a family (`_discover_rules_dir`, `_discover_memory_dir` are still
    doubled) is where a pattern stops being a coincidence.

    `.resolve()` is deliberate: callers derive sibling paths from this
    (``lint()`` walks two levels up to find the repo root), and a relative store
    path made that computation report every `plan_path` as broken.
    """
    env = os.environ.get("CONTEXT_WORK_DIR")
    if env:
        p = Path(env).expanduser().resolve()
        return p if p.is_dir() else None
    cwd = Path.cwd()
    for candidate in [cwd, *cwd.parents]:
        for conv in store_conventions():
            work = candidate / conv / "work"
            if work.is_dir():
                return work.resolve()
    return None


def warn_if_example_rules(rules_dir: Path) -> None:
    """The shipped starter pack (``examples/rules``) is INERT — copy-to-activate,
    never a production default. Auto-discovery (``<dir>/.context/rules`` or
    ``<dir>/.claude/rules``) can never reach it, but a misconfigured
    ``CONTEXT_RULES_DIR`` could point at it directly. Print a loud NOTE so the
    examples don't silently 'poison' a real rule set."""
    if "examples" in rules_dir.parts:
        import sys

        print(
            f"[context-toolkit] NOTE: serving EXAMPLE/starter rules from "
            f"{rules_dir} — inert starter pack, copy into your own rules "
            f"dir for real use.",
            file=sys.stderr,
        )


# ---------------------------------------------------------------------------
# BM25F recall — shared between any content type that ranks records by
# keyword relevance (memory today, work items now). Extracted here rather
# than left in memory.py once a second store needed the identical ranking:
# duplicating already-reviewed ranking math is how the double-counting bug
# found in this exact algorithm (2026-07-26 review) would have shipped
# TWICE, silently, in two files that drift independently.
# ---------------------------------------------------------------------------

_WORD_RE = re.compile(r"[a-z0-9_]+")

# BM25 parameters (Robertson/Sparck-Jones defaults).
#   k1 — term-frequency saturation: the 10th occurrence of a word adds far less
#        than the 2nd, so a record that merely repeats a term cannot outrank
#        one that is actually about it.
#   b  — length normalisation strength (1.0 = full, 0 = off). Needed because a
#        grown store is wildly uneven, and without normalisation a long
#        collector record matches almost any query by sheer surface area.
BM25_K1 = 1.5
BM25_B = 0.75

# Shortest query term still resolved by PREFIX (forward-stemming "deploy" ->
# "deployment"). Below it a term must match a vocabulary entry exactly.
# Prefix matching is blind to word boundaries, and on short terms that is
# mostly noise: "wie" reaches "wieder"/"wiederherstellen" (38 entries in a real
# German store), "der" reaches "deren"/"derivable". The useful short matches —
# "pii" -> pii_anonymization, "set" -> set_budget — are snake_case parts, and
# those are indexed as their own tokens anyway (see split_token), so an exact
# match still finds them. Long terms keep prefix behaviour, where stemming
# actually pays.
PREFIX_MIN_LEN = 4


def split_token(token: str) -> list[str]:
    """Split a token into its snake_case parts (or return it unchanged).

    Indexing and querying both go through this, which is what keeps the two
    sides symmetric. Indexing the FULL token alongside its parts would look
    harmless but double-counts: a prefix query for "deploy" matches both the
    part ``deploy`` and the whole ``deploy_workflow``, and pooling their
    postings credits one occurrence twice — and only for tokens whose LEADING
    part matches, so "deploy_workflow" would outscore "release_deploy" on a
    query neither of them mentions more often (this exact bug shipped once,
    was caught by review, and is now pinned by a test — see test_memory.py's
    ``test_snake_case_parts_are_not_double_counted``). Parts only, on both
    sides.

    The trade-off, deliberately accepted: identifier-exact lookup gets weaker.
    A query for ``deploy_workflow`` now becomes {deploy, workflow} and also
    reaches records that only mention ``workflow``. The compound is no longer
    an anchor of its own — bag-of-words behaviour, in exchange for a term
    frequency that means what it says.
    """
    if "_" not in token:
        return [token]
    parts = [p for p in token.split("_") if p]
    return parts or [token]


def build_bm25_index(fields_by_key: dict[str, list[tuple[str, float]]]) -> dict:
    """Build an inverted BM25F index from ``{key: [(text, field_weight), ...]}``.

    Shape returned::

        {"postings": {token: {key: weighted_tf}},
         "tokens":   [sorted vocabulary],
         "lengths":  {key: weighted_length},
         "avg_len":  float}

    ``weighted_tf`` already carries the field weight (a hit in a 3x-weighted
    field counts 3x), which is what makes this BM25**F** rather than plain
    BM25 — field emphasis is preserved as term frequency, not a separate score
    component. The vocabulary is kept sorted so a query term can be resolved
    to every token it PREFIXES via binary search (see ``prefix_slice``).
    """
    postings: dict[str, dict[str, float]] = {}
    lengths: dict[str, float] = {}
    for key, fields in fields_by_key.items():
        total = 0.0
        for text, weight in fields:
            for token in _WORD_RE.findall(text.lower()):
                for part in split_token(token):
                    postings.setdefault(part, {})
                    postings[part][key] = postings[part].get(key, 0.0) + weight
                    total += weight
        lengths[key] = total
    n = len(lengths)
    return {
        "postings": postings,
        "tokens": sorted(postings),
        "lengths": lengths,
        "avg_len": (sum(lengths.values()) / n) if n else 0.0,
    }


def prefix_slice(tokens: list[str], term: str, min_len: int = PREFIX_MIN_LEN) -> list[str]:
    """Vocabulary entries matching ``term``, via binary search.

    Terms shorter than ``min_len`` match exactly; longer ones match by prefix
    (forward-stemming). See ``PREFIX_MIN_LEN`` for why the cutoff exists.
    """
    lo = bisect.bisect_left(tokens, term)
    if len(term) < min_len:
        return [term] if lo < len(tokens) and tokens[lo] == term else []
    # Bound the slice by binary search too, so the work is proportional to the
    # number of MATCHES, not to the vocabulary tail behind them. The sentinel
    # is safe because the vocabulary is built from [a-z0-9_] only (max
    # codepoint 122), well below U+FFFD.
    hi = bisect.bisect_right(tokens, term + "�")
    return tokens[lo:hi]


def bm25_scores(
    index: dict, query: str, *, k1: float = BM25_K1, b: float = BM25_B
) -> dict[str, float]:
    """Score every key in ``index`` against ``query``. Returns ``{key: base}``
    for keys with a nonzero score — callers layer their own boosts/floors on
    top (frecency, backlinks, whatever the content type owns) and look up the
    record itself; this function only ranks.

    Query terms go through the same snake_case split as the index (see
    ``split_token``), so a query for "deploy_workflow" resolves against the
    parts that were actually indexed rather than against a compound that is
    not there.
    """
    terms = {part for raw in _WORD_RE.findall(query.lower()) for part in split_token(raw)}
    if not terms:
        return {}

    postings, vocab = index["postings"], index["tokens"]
    lengths, avg_len = index["lengths"], index["avg_len"]
    total_docs = len(lengths)

    # BM25F: sum over query terms of IDF(term) * saturated, length-normalised
    # term frequency. Each term is resolved to every vocabulary entry it
    # prefixes, and their weighted frequencies are pooled — so "deploy" scores
    # "deployment" and "deploy_workflow" as the same concept.
    bases: dict[str, float] = {}
    for term in terms:
        matches = prefix_slice(vocab, term)
        if not matches:
            continue
        # Document frequency of the CONCEPT, not of one spelling: a record
        # that holds several matching tokens must still be counted once.
        hits: dict[str, float] = {}
        for token in matches:
            for key, tf in postings[token].items():
                hits[key] = hits.get(key, 0.0) + tf
        # Robertson/Sparck-Jones IDF. A term carried by nearly every record
        # ("der", "wie") lands near zero, so filler words in a natural-language
        # question stop competing with the words that carry the topic.
        idf = math.log(1 + (total_docs - len(hits) + 0.5) / (len(hits) + 0.5))
        for key, tf in hits.items():
            norm = 1 - b + b * (lengths[key] / avg_len)
            bases[key] = bases.get(key, 0.0) + idf * (tf * (k1 + 1) / (tf + k1 * norm))
    return bases
