"""Recall learning — a confirmed recall gap becomes a durable ranking signal.

A scout (a small model searching the store with its own vocabulary) finds a
memory the mechanical recall missed for a query. Logging that is measurement,
not learning: the next identical query misses again. ``learn_recall_gap``
closes the loop deterministically and without any model call:

1. Validate the claim — the target resolves the way ``get_memory`` resolves
   it (exact name, else the package that absorbed that member slug), the
   scout named it,
   and the mechanical ranking, recomputed WITHOUT any session exclude set,
   really does not deliver it within ``limit`` (an already-delivered memory is
   a dedupe artefact, not a gap).
2. Validate the bridge terms — one to ``MAX_TERMS`` clean, distinct terms,
   every word of which is literally a word of the real query. That is both
   the effect condition (only query words can change this query's ranking)
   and the trust boundary (nothing else can be written into curated text).
3. Shadow-replay — the proposed tags are applied to a copy of the target in a
   shadow engine and ranked through the SAME ``memory_recall_hits`` path with
   the SAME frozen frecency snapshot and backlink boost. Only when the target
   then lands within ``limit`` is anything written, and only the minimal
   subset of terms that still achieves it.
4. Write — under an exclusive store lock the file is re-read, only the
   ``learned_tags`` node of its frontmatter is edited (where ``recall`` reads
   it: top level first, else ``metadata:``), the result is re-parsed and must
   equal the old frontmatter plus exactly the added tags with an identical
   body, then an atomic ``os.replace``. Anything that does not verify is
   rejected fail-closed, nothing is written.

``learned_tags`` is indexed together with ``tags`` in the same BM25F tag field
(same weight, no length normalisation), so the learned signal flows through
the one ranking — no second ranking signal, no usage-store mutation. It is a
separate list only so its provenance survives: the bundler rebuilds ``tags``
from a package's current members and carries ``learned_tags`` over.
"""

from __future__ import annotations

import json
import os
import unicodedata
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel

from mcp_context_toolkit.core import (
    MAX_RECALL_LIMIT,
    clamp_limit,
    parse_frontmatter,
    query_terms,
)
from mcp_context_toolkit.memory import Memory, MemoryEngine, _parse_memory, memory_recall_hits
from mcp_context_toolkit.usage import LockUnavailable, RecallEventLog, exclusive_file_lock

MAX_TERMS = 5
MAX_TERM_LEN = 60
DEFAULT_LIMIT = 6
LOCK_NAME = "recall_learning.lock"
COMPARISON_LOCK_DIR = "recall_comparisons"
# The frontmatter list recall learning writes: ranked exactly like ``tags``,
# kept apart so a re-bundle can rebuild ``tags`` from its current members
# without losing — or silently keeping — what was learned (see bundler).
LEARNED_KEY = "learned_tags"
# Stripped from the edges of a QUERY word before bridge terms are matched
# against it ("prüft?" still offers "prüft"); a term word itself must be clean.
_EDGE_PUNCT = ".,;:!?\"'()[]{}<>„“”‚‘’«»"

LearningStatus = Literal["accepted", "noop", "rejected"]


class RecallLearningResult(BaseModel):
    status: LearningStatus
    reason: str
    target: str                      # the record recall ranks (a package for a member slug)
    requested_target: str = ""       # the name the caller passed
    proposed_tags: list[str] = []
    added_tags: list[str] = []
    invalid_terms: list[str] = []
    baseline_names: list[str] = []
    replay_names: list[str] = []
    before_rank: int | None = None   # 1-based, in the uncapped ranking; None = not ranked at all
    after_rank: int | None = None


class ScoutHit(BaseModel):
    """One memory a scout found, with query words that explain the bridge."""

    target_name: str
    bridge_terms: list[str]


class ScoutComparisonResult(BaseModel):
    """One idempotent, resumable scout comparison for a session turn."""

    comparison_id: str
    query: str
    turn: int
    limit: int
    mechanical_names: list[str]
    scout_names: list[str]
    results: list[RecallLearningResult]
    resumed: bool = False


class LearnedTagAblation(BaseModel):
    """Read-only evidence for retaining or later manually removing one tag."""

    target: str
    tag: str
    status: Literal["necessary", "candidate", "not_decidable"]
    evidence_queries: list[str] = []
    with_ranks: list[int | None] = []
    without_ranks: list[int | None] = []


class LearnedTagAblationPreview(BaseModel):
    """Read-only maintenance preview; it never edits a memory file."""

    tags: list[LearnedTagAblation]
    jointly_redundant: dict[str, list[str]] = {}


class UnsafeFrontmatter(ValueError):
    """The frontmatter cannot be edited with a verifiable, minimal change."""


def learn_recall_gap(
    engine: MemoryEngine, *, query: str, target_name: str, bridge_terms: list[str],
    scout_names: list[str], mechanical_names: list[str] | None = None,
    limit: int = DEFAULT_LIMIT, boosts: dict[str, float] | None = None,
    event_log: RecallEventLog | None = None, turn: int | None = None,
) -> RecallLearningResult:
    """Validate a scout's recall gap, replay the bridge terms, and persist
    them into the target's ``learned_tags`` only when the replay proves the target
    then ranks within ``limit``. See the module docstring for the contract.

    ``boosts`` is the caller's frecency snapshot (``UsageStore.boosts()``);
    it is copied once so baseline and every replay rank against identical
    heat. ``event_log`` receives one ``recall_learning`` event per call,
    whatever the outcome."""
    snapshot = dict(boosts or {})
    result = _learn(engine, query, target_name, bridge_terms, scout_names,
                    clamp_limit(limit), snapshot)
    if event_log is not None:
        event_log.log_recall_learning(
            turn if turn is not None else event_log.current_turn(), query,
            {**result.model_dump(), "scout_names": list(scout_names),
             "mechanical_names": list(mechanical_names or [])},
        )
    return result


def complete_scout_comparison(
    engine: MemoryEngine, *, query: str, turn: int, limit: int,
    mechanical_names: list[str], scout_hits: list[ScoutHit | dict[str, Any]],
    duration_ms: int, event_log: RecallEventLog | None = None,
    boosts: dict[str, float] | None = None,
) -> ScoutComparisonResult:
    """Complete all learning from one scout comparison exactly once.

    The comparison id is ``(session_id, turn)``. A durable ``claimed`` event
    is appended before each possible memory write. On retry completed targets
    are reused; a claim left behind by a crash is reconciled from its recorded
    pre-state and replay evidence, otherwise the learner is safely rerun.
    ``limit`` is the original mechanical recall limit and is never inferred
    from the scout result.
    """
    hits, limit = _validate_comparison_input(
        query, turn, limit, mechanical_names, scout_hits, duration_ms,
    )
    log = event_log or _event_log_for(engine)
    if log is None:
        raise ValueError("comparison requires a memory-backed event log")
    comparison_id = f"{log.session_id}:{turn}"
    lock_path = log.session_dir / COMPARISON_LOCK_DIR / f"{_safe_lock_name(comparison_id)}.lock"
    snapshot = dict(boosts or {})
    scout_names = [hit.target_name for hit in hits]
    with exclusive_file_lock(lock_path, required=True):
        history = _comparison_history(log, comparison_id)
        resumed = bool(history.get("started"))
        if history.get("started") and not _comparison_payload_matches(
            history["start"], query, limit, mechanical_names, hits, duration_ms,
        ):
            raise ValueError("comparison id already belongs to different payload")
        if history.get("completed"):
            return _comparison_from_history(history, comparison_id, query, turn, limit,
                                            mechanical_names, scout_names, resumed=True)
        if not history.get("started"):
            _checkpoint(log,
                "scout_comparison_started", comparison_id, turn, query,
                limit=limit, mechanical_names=list(mechanical_names),
                scout_hits=[hit.model_dump() for hit in hits], duration_ms=duration_ms,
            )
            history = _comparison_history(log, comparison_id)

        results: list[RecallLearningResult] = []
        for hit in hits:
            previous = history["results"].get(hit.target_name)
            if previous is not None:
                results.append(RecallLearningResult.model_validate(previous))
                continue
            claim = history["claims"].get(hit.target_name)
            if claim is not None:
                recovered = _recover_claim(engine, claim, query, limit, snapshot)
                if recovered is not None:
                    _checkpoint(log,
                        "scout_comparison_result", comparison_id, turn, query,
                        target_name=hit.target_name, result=recovered.model_dump(), recovered=True,
                    )
                    results.append(recovered)
                    history["results"][hit.target_name] = recovered.model_dump()
                    continue
            else:
                target = engine.get(hit.target_name)
                pre_tags = list(target.learned_tags) if target is not None else []
                claim = {"target_name": hit.target_name, "bridge_terms": hit.bridge_terms,
                         "pre_learned_tags": pre_tags, "limit": limit,
                         "mechanical_names": list(mechanical_names),
                         "scout_names": scout_names}
                _checkpoint(log,
                    "scout_comparison_claimed", comparison_id, turn, query, **claim,
                )
            # Keep the individual compatibility event out of the closed-loop
            # transaction: this result checkpoint is its sole durable outcome.
            result = learn_recall_gap(
                engine, query=query, target_name=hit.target_name,
                bridge_terms=hit.bridge_terms, scout_names=scout_names,
                mechanical_names=mechanical_names, limit=limit, boosts=snapshot,
            )
            _checkpoint(log,
                "scout_comparison_result", comparison_id, turn, query,
                target_name=hit.target_name, result=result.model_dump(), recovered=False,
            )
            results.append(result)
            history["results"][hit.target_name] = result.model_dump()
        completed = ScoutComparisonResult(
            comparison_id=comparison_id, query=query, turn=turn, limit=limit,
            mechanical_names=list(mechanical_names), scout_names=scout_names,
            results=results, resumed=resumed,
        )
        _checkpoint(log, "scout_comparison_completed", comparison_id, turn, query,
                    result=completed.model_dump())
    return completed


def preview_learned_tag_ablation(
    engine: MemoryEngine, *, event_log: RecallEventLog | None = None,
    boosts: dict[str, float] | None = None,
) -> LearnedTagAblationPreview:
    """Preview whether evidence-backed ``learned_tags`` still matter.

    Only accepted learning events with a recorded original limit supply
    evidence. Missing historical fields are deliberately not guessed. Each
    query is replayed with the current tag set and then with a tag removed;
    no file is changed, and a future writer must use the ordinary lock and
    re-parse path separately.
    """
    log = event_log or _event_log_for(engine)
    proofs = _accepted_tag_proofs(log) if log is not None else {}
    snapshot = dict(boosts or {})
    records: list[LearnedTagAblation] = []
    candidate_by_target: dict[str, list[str]] = {}
    for memory in engine._memories:
        for tag in memory.learned_tags:
            evidence = proofs.get((memory.name, tag), [])
            if not evidence:
                records.append(LearnedTagAblation(target=memory.name, tag=tag,
                                                  status="not_decidable"))
                continue
            with_ranks = [_rank(_ranking(engine, query, snapshot), memory.name)
                          for query, _ in evidence]
            without = engine.with_memory(memory.model_copy(update={
                "learned_tags": [known for known in memory.learned_tags if known != tag],
            }))
            without_ranks = [_rank(_ranking(without, query, snapshot), memory.name)
                             for query, _ in evidence]
            if any(rank is None or rank > limit
                   for rank, (_, limit) in zip(with_ranks, evidence, strict=True)):
                status: Literal["necessary", "candidate", "not_decidable"] = "not_decidable"
            elif any(rank is None or rank > limit
                     for rank, (_, limit) in zip(without_ranks, evidence, strict=True)):
                status = "necessary"
            else:
                status = "candidate"
                candidate_by_target.setdefault(memory.name, []).append(tag)
            records.append(LearnedTagAblation(target=memory.name, tag=tag, status=status,
                                              evidence_queries=[query for query, _ in evidence],
                                              with_ranks=with_ranks, without_ranks=without_ranks))

    jointly_redundant: dict[str, list[str]] = {}
    for target, tags in candidate_by_target.items():
        if len(tags) < 2:
            continue
        memory = engine.get(target)
        if memory is None:
            continue
        evidence = [proof for tag in tags for proof in proofs.get((target, tag), [])]
        removed = engine.with_memory(memory.model_copy(update={
            "learned_tags": [known for known in memory.learned_tags if known not in tags],
        }))
        if all((rank := _rank(_ranking(removed, query, snapshot), target)) is not None and rank <= limit
               for query, limit in evidence):
            jointly_redundant[target] = tags
    return LearnedTagAblationPreview(tags=records, jointly_redundant=jointly_redundant)


def _accepted_tag_proofs(log: RecallEventLog) -> dict[tuple[str, str], list[tuple[str, int]]]:
    """Extract replayable accepted events from every session of this store."""
    proofs: dict[tuple[str, str], list[tuple[str, int]]] = {}
    try:
        files = sorted(log.session_dir.glob("*.recall_events.jsonl"))
    except OSError:
        return proofs
    for path in files:
        claims: dict[tuple[str, str], int] = {}
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict):
                continue
            if (entry.get("event") == "scout_comparison_claimed"
                    and isinstance(entry.get("comparison_id"), str)):
                target = entry.get("target_name")
                limit = entry.get("limit")
                if isinstance(target, str) and isinstance(limit, int) and not isinstance(limit, bool):
                    claims[(entry["comparison_id"], target)] = limit
            elif entry.get("event") == "scout_comparison_result":
                result = entry.get("result")
                target_name = entry.get("target_name")
                comparison_id = entry.get("comparison_id")
                query = entry.get("query")
                if not (isinstance(result, dict) and result.get("status") == "accepted"
                        and isinstance(target_name, str) and isinstance(comparison_id, str)
                        and isinstance(query, str)):
                    continue
                limit = claims.get((comparison_id, target_name))
                resolved = result.get("target", target_name)
                tags = result.get("added_tags")
                if not (isinstance(limit, int) and isinstance(resolved, str)
                        and isinstance(tags, list)):
                    continue
                for tag in tags:
                    if isinstance(tag, str):
                        key = (resolved, tag)
                        proof = (query, limit)
                        if proof not in proofs.setdefault(key, []):
                            proofs[key].append(proof)
    return proofs


def _validate_comparison_input(query: str, turn: int, limit: int,
                               mechanical_names: list[str],
                               raw_hits: list[ScoutHit | dict[str, Any]],
                               duration_ms: int) -> tuple[list[ScoutHit], int]:
    """Validate all external input before a comparison creates its first event."""
    if (not isinstance(query, str) or not query.strip() or isinstance(turn, bool)
            or not isinstance(turn, int) or turn < 0):
        raise ValueError("query must be non-empty and turn must be non-negative")
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValueError("limit must be an integer")
    if isinstance(duration_ms, bool) or not isinstance(duration_ms, int) or duration_ms < 0:
        raise ValueError("duration_ms must be a non-negative integer")
    if not isinstance(mechanical_names, list) or not all(isinstance(n, str) for n in mechanical_names):
        raise ValueError("mechanical_names must be a list of strings")
    if not isinstance(raw_hits, list):
        raise ValueError("scout_hits must be a list")
    hits = [hit if isinstance(hit, ScoutHit) else ScoutHit.model_validate(hit) for hit in raw_hits]
    names = [hit.target_name for hit in hits]
    if len(names) != len(set(names)):
        raise ValueError("scout hit targets must be unique")
    if any(not hit.target_name.strip() or not isinstance(hit.bridge_terms, list)
           or not all(isinstance(term, str) for term in hit.bridge_terms) for hit in hits):
        raise ValueError("every scout hit needs a target and bridge terms")
    for hit in hits:
        terms, invalid = _clean_terms(hit.bridge_terms)
        if invalid or not terms or len(terms) > MAX_TERMS or _terms_outside_query(terms, query):
            raise ValueError("every scout bridge term must be a valid query word")
    return hits, clamp_limit(limit)


def _event_log_for(engine: MemoryEngine) -> RecallEventLog | None:
    if not engine._roots:
        return None
    return RecallEventLog.for_memory_dir(engine._roots[0][0])


def _safe_lock_name(value: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in value)


def _checkpoint(log: RecallEventLog, event: str, comparison_id: str, turn: int,
                query: str, **payload: object) -> None:
    """Require durable comparison state; telemetry failures are not harmless here."""
    if not log.log_scout_comparison(event, comparison_id, turn, query, **payload):
        raise RuntimeError("comparison checkpoint unavailable")


def _comparison_history(log: RecallEventLog, comparison_id: str) -> dict[str, Any]:
    history: dict[str, Any] = {"started": False, "start": None, "completed": None,
                               "claims": {}, "results": {}}
    try:
        lines = log.log_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return history
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("comparison_id") != comparison_id:
            continue
        kind = event.get("event")
        if kind == "scout_comparison_started":
            history["started"] = True
            history["start"] = event
        elif kind == "scout_comparison_claimed":
            history["claims"][event.get("target_name")] = event
        elif kind == "scout_comparison_result" and isinstance(event.get("result"), dict):
            history["results"][event.get("target_name")] = event["result"]
        elif kind == "scout_comparison_completed" and isinstance(event.get("result"), dict):
            history["completed"] = event["result"]
    return history


def _comparison_payload_matches(start: dict[str, Any], query: str, limit: int,
                                mechanical_names: list[str], hits: list[ScoutHit],
                                duration_ms: int) -> bool:
    return (start.get("query") == query and start.get("limit") == limit
            and start.get("mechanical_names") == mechanical_names
            and start.get("scout_hits") == [hit.model_dump() for hit in hits]
            and start.get("duration_ms") == duration_ms)


def _comparison_from_history(history: dict[str, Any], comparison_id: str, query: str, turn: int,
                             limit: int, mechanical_names: list[str], scout_names: list[str],
                             *, resumed: bool) -> ScoutComparisonResult:
    stored = history.get("completed")
    if isinstance(stored, dict):
        return ScoutComparisonResult.model_validate({**stored, "resumed": resumed})
    return ScoutComparisonResult(comparison_id=comparison_id, query=query, turn=turn, limit=limit,
                                 mechanical_names=mechanical_names, scout_names=scout_names,
                                 results=[RecallLearningResult.model_validate(v)
                                          for v in history["results"].values()], resumed=resumed)


def _recover_claim(engine: MemoryEngine, claim: dict[str, Any], query: str, limit: int,
                   boosts: dict[str, float]) -> RecallLearningResult | None:
    """Reconstruct an accepted result after a crash just after ``os.replace``."""
    target = engine.get(claim.get("target_name", ""))
    if target is None or claim.get("limit") != limit:
        return None
    before = set(claim.get("pre_learned_tags", []))
    added = [tag for tag in target.learned_tags if tag not in before]
    expected = set(claim.get("bridge_terms", []))
    if not added or not set(added).issubset(expected):
        return None
    ranking = _ranking(engine, query, boosts)
    rank = _rank(ranking, target.name)
    if rank is None or rank > limit:
        return None
    return RecallLearningResult(status="accepted", reason="recovered_after_replace",
                                target=target.name, requested_target=claim["target_name"],
                                proposed_tags=list(claim["bridge_terms"]), added_tags=added,
                                replay_names=ranking[:limit], after_rank=rank)


def _learn(
    engine: MemoryEngine, query: str, target_name: str, bridge_terms: list[str],
    scout_names: list[str], limit: int, boosts: dict[str, float],
) -> RecallLearningResult:
    terms, invalid = _clean_terms(bridge_terms)
    # Resolved exactly like get_memory(): a bundled member slug (kebab or
    # snake) resolves to the package that absorbed it — the unit recall ranks
    # and delivers. Scouts report member slugs; every real gap measured before
    # this was built named one.
    requested = target_name
    target = engine.get(requested)
    resolved = target.name if target is not None else requested

    def done(status: LearningStatus, reason: str, **extra) -> RecallLearningResult:
        return RecallLearningResult(status=status, reason=reason, target=resolved,
                                    requested_target=requested, proposed_tags=terms,
                                    **extra)

    if invalid or not terms or len(terms) > MAX_TERMS:
        return done("rejected", "invalid_terms", invalid_terms=invalid)
    if target is None:
        return done("rejected", "unknown_target")
    if requested not in scout_names:
        return done("rejected", "target_not_in_scout")
    outside = _terms_outside_query(terms, query)
    if outside:
        return done("rejected", "term_not_in_query", invalid_terms=outside)

    ranking = _ranking(engine, query, boosts)
    baseline, before_rank = ranking[:limit], _rank(ranking, resolved)
    if resolved in baseline:
        return done("noop", "target_already_recalled", baseline_names=baseline,
                    before_rank=before_rank)
    measured = {"baseline_names": baseline, "before_rank": before_rank}

    lock_path = _lock_path(engine, target)
    if lock_path is None:
        return done("rejected", "not_writable", **measured)
    try:
        with exclusive_file_lock(lock_path, required=True):
            return _replay_and_write(engine, target, terms, query, limit, boosts,
                                     done, measured)
    except LockUnavailable:
        return done("rejected", "lock_unavailable", **measured)


def _replay_and_write(engine, target: Memory, terms, query, limit, boosts, done, measured):
    """Runs under the store lock: re-read, replay, minimise, write."""
    path = Path(str(target.source_path))
    try:
        text = path.read_text(encoding="utf-8")
        fresh = _parse_memory(path, target.tier)
    except (OSError, ValueError):
        return done("rejected", "not_writable", **measured)
    if fresh.name != target.name:
        return done("rejected", "target_changed", **measured)
    known = {tag.casefold() for tag in [*fresh.tags, *fresh.learned_tags]}
    new_terms = [term for term in terms if term.casefold() not in known]
    if not new_terms:
        return done("rejected", "no_new_terms", **measured)

    replay_names, after_rank = _replay(engine, fresh, new_terms, query, boosts)
    if after_rank is None or after_rank > limit:
        return done("rejected", "replay_no_effect", replay_names=replay_names[:limit],
                    after_rank=after_rank, **measured)
    chosen = list(new_terms)
    for term in new_terms:  # drop every term the replay does not need
        trial = [t for t in chosen if t != term]
        if trial:
            rank = _replay(engine, fresh, trial, query, boosts)[1]
            if rank is not None and rank <= limit:
                chosen = trial
    replay_names, after_rank = _replay(engine, fresh, chosen, query, boosts)

    try:
        new_text = add_tags_to_frontmatter(text, chosen, key=LEARNED_KEY)
    except UnsafeFrontmatter:
        return done("rejected", "unsafe_frontmatter", **measured)
    tmp = path.with_name(f".{path.name}.recall-learning.tmp")
    try:
        # The lock only serializes learners. A host edit, a dream pass or a
        # reindex during the replays above does not take it — re-check right
        # before the replace so such a write is never silently overwritten.
        if path.read_text(encoding="utf-8") != text:
            return done("rejected", "target_changed", **measured)
        tmp.write_text(new_text, encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        return done("rejected", "not_writable", **measured)
    return done("accepted", "replay_verified", added_tags=chosen,
                replay_names=replay_names[:limit], after_rank=after_rank, **measured)


def _clean_terms(raw_terms: list[str]) -> tuple[list[str], list[str]]:
    """Whitespace-normalised, case-insensitively deduplicated terms, plus the
    raw values that are unusable (empty, too long, or without any token)."""
    clean: list[str] = []
    invalid: list[str] = []
    seen: set[str] = set()
    for raw in raw_terms:
        # NFC, so a decomposed "lügt" is written (and compared) as the same
        # word the ranker indexes.
        term = unicodedata.normalize("NFC", " ".join(str(raw).split()))
        if not term or len(term) > MAX_TERM_LEN or not query_terms(term):
            invalid.append(str(raw))
        elif term.casefold() not in seen:
            seen.add(term.casefold())
            clean.append(term)
    return clean, invalid


def _terms_outside_query(terms: list[str], query: str) -> list[str]:
    """Terms with any word that is not literally (case-insensitively, NFC) a
    word of the query. Checked on whitespace words, NOT on ranker tokens: the
    tokenizer drops punctuation and every non-word character, and a prefix
    reaches longer tokens, so a token check would let unindexed text
    (punctuation, symbols, a word glued behind a query word) ride along into
    curated content. A caller can only teach a memory words of the query it
    passes (and which is logged with the call)."""
    query = unicodedata.normalize("NFC", query)
    words = {w.strip(_EDGE_PUNCT).casefold() for w in query.split()} - {""}
    return [term for term in terms
            if any(w.casefold() not in words for w in term.split())]


def _ranking(engine: MemoryEngine, query: str, boosts: dict[str, float]) -> list[str]:
    """The production keyword ranking (backlink boost included), uncapped up
    to ``MAX_RECALL_LIMIT``, with no usage recording and no event."""
    return [m.name for m in memory_recall_hits(engine, query, limit=MAX_RECALL_LIMIT,
                                               boosts=boosts)]


def _rank(ranking: list[str], name: str) -> int | None:
    return ranking.index(name) + 1 if name in ranking else None


def _replay(engine: MemoryEngine, fresh: Memory, terms: list[str], query: str,
            boosts: dict[str, float]) -> tuple[list[str], int | None]:
    shadow = engine.with_memory(
        fresh.model_copy(update={"learned_tags": [*fresh.learned_tags, *terms]}))
    ranking = _ranking(shadow, query, boosts)
    return ranking, _rank(ranking, fresh.name)


def _lock_path(engine: MemoryEngine, target: Memory) -> Path | None:
    """One lock per store, in the ``.session_state`` sibling the recall event
    log already uses (``.claude/memory`` -> ``.claude/.session_state``)."""
    root = engine._owning_root(target)
    if root is None or not target.source_path or not Path(target.source_path).is_file():
        return None
    return root.parent / RecallEventLog.DIRNAME / LOCK_NAME


# --- frontmatter edit -------------------------------------------------------

def add_tags_to_frontmatter(text: str, add: list[str], key: str = "tags") -> str:
    """Return ``text`` with ``add`` appended to the list ``key`` that
    ``recall`` reads (top-level ``key``, else ``metadata.<key>``; a missing
    list is created under ``metadata:`` when that block exists, else at top
    level). Only lines of that one node change; every other byte is kept.
    Recall learning writes ``LEARNED_KEY``.

    Raises ``UnsafeFrontmatter`` unless the edited text re-parses to exactly
    the old frontmatter plus the added tags, with an identical body."""
    meta, body = parse_frontmatter(text)
    if not meta:
        raise UnsafeFrontmatter("no parseable frontmatter")
    lines = text.splitlines(keepends=True)
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        raise UnsafeFrontmatter("unterminated frontmatter")
    newline = "\r\n" if lines[0].endswith("\r\n") else "\n"

    nested = meta.get(key) is None and isinstance(meta.get("metadata"), dict)
    container = meta["metadata"] if nested else meta
    existing = container.get(key)
    if existing is not None and not isinstance(existing, list):
        raise UnsafeFrontmatter(f"{key} is not a list")
    merged = [*(existing or []), *add]

    if nested:
        span = _top_level_block(lines, 1, end, "metadata")
        if span is None:
            raise UnsafeFrontmatter("metadata is not a block mapping")
        start, stop = span
        indent = _child_indent(lines, start + 1, stop) or "  "
        new_lines = _edit_list_node(lines, start + 1, stop, indent, key, existing, add, newline)
    else:
        new_lines = _edit_list_node(lines, 1, end, "", key, existing, add, newline)

    new_text = "".join(new_lines)
    expected = json.loads(json.dumps(meta, default=str))
    (expected["metadata"] if nested else expected)[key] = merged
    new_meta, new_body = parse_frontmatter(new_text)
    if json.loads(json.dumps(new_meta, default=str)) != expected or new_body != body:
        raise UnsafeFrontmatter("edit did not verify")
    return new_text


def _indent_of(line: str) -> str:
    return line[: len(line) - len(line.lstrip(" "))]


def _top_level_block(lines, lo, hi, key) -> tuple[int, int] | None:
    """``(key_line, stop)`` for a column-0 ``key:`` without an inline value
    in ``lines[lo:hi]``; ``stop`` is one past its last child line. None when
    the key is absent or written inline (e.g. a flow mapping)."""
    for i in range(lo, hi):
        line = lines[i].rstrip("\r\n")
        if not line.startswith(f"{key}:"):
            continue
        if line[len(key) + 1:].split("#", 1)[0].strip():
            return None
        stop = i + 1
        while stop < hi and _is_child(lines[stop], ""):
            stop += 1
        return i, stop
    return None


def _is_child(line: str, indent: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    own = _indent_of(line.rstrip("\r\n"))
    return len(own) > len(indent) or (own == indent and stripped.startswith("- "))


def _child_indent(lines, lo, hi) -> str | None:
    for i in range(lo, hi):
        if lines[i].strip():
            return _indent_of(lines[i].rstrip("\r\n"))
    return None


def _scalar(value: str) -> str:
    """Plain YAML scalar when it round-trips unchanged, else a JSON string
    (every JSON string is a valid YAML double-quoted scalar)."""
    try:
        if yaml.safe_load(f"- {value}") == [value]:
            return value
    except yaml.YAMLError:
        pass
    return json.dumps(value, ensure_ascii=False)


def _edit_list_node(lines, lo, hi, indent, key, existing, add, newline):
    """Append ``add`` to the list node ``key`` at ``indent`` in ``lines[lo:hi]``.
    A block list with items gets new items in its own style; any other shape
    (absent, null, flow list) is replaced by a fresh block list."""
    key_line = next(
        (i for i in range(lo, hi)
         if _indent_of(lines[i].rstrip("\r\n")) == indent
         and lines[i][len(indent):].startswith(f"{key}:")),
        None,
    )
    if key_line is None:
        block = [f"{indent}{key}:{newline}"] + [f"{indent}  - {_scalar(t)}{newline}" for t in add]
        insert_at = hi
        while insert_at > lo and not lines[insert_at - 1].strip():
            insert_at -= 1
        return lines[:insert_at] + block + lines[insert_at:]
    stop = key_line + 1
    while stop < hi and _is_child(lines[stop], indent):
        stop += 1
    inline = lines[key_line].rstrip("\r\n")[len(indent) + len(key) + 1:].split("#", 1)[0].strip()
    items = [i for i in range(key_line + 1, stop) if lines[i].strip().startswith("- ")]
    if not inline and items and existing:
        prefix = lines[items[0]][: len(lines[items[0]]) - len(lines[items[0]].lstrip(" "))]
        added = [f"{prefix}- {_scalar(t)}{newline}" for t in add]
        return lines[:stop] + added + lines[stop:]
    block = [f"{indent}{key}:{newline}"] + [
        f"{indent}  - {_scalar(t)}{newline}" for t in [*(existing or []), *add]
    ]
    return lines[:key_line] + block + lines[stop:]
