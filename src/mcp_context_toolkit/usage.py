"""Usage tracking for knowledge stores — the hot/cold (frecency) signal.

Frequency-dominant by design: a memory's heat is driven by how OFTEN it is
used, not by wall-clock recency. A weekend (or three-week) pause must NOT cool
a heavily-used memory — the guiding principle "decay by conversation distance,
not wall time" applies to the store too. Timestamps are kept for display and
as a tiebreak only; they never decay the score.

Per-machine and best-effort. This captures only EXPLICIT recall()/get_memory()
hits routed through the MCP. A host's own auto-recall (if any) bypasses this
layer, so counts are a LOWER BOUND, never a
full tally. The score must — and does — degrade gracefully under that
undercounting: log-damped and frequency-dominant, so a handful of missed hits
cannot flip a ranking.
"""

from __future__ import annotations

import json
import math
import os
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

try:
    import fcntl  # POSIX advisory file locking (Linux/Mac); absent on Windows
except ImportError:  # pragma: no cover - non-POSIX platform
    fcntl = None  # type: ignore[assignment]

# Heat weights: opening the full body ("I actually used this") is a stronger
# signal than merely surfacing in a recall result list.
_W_OPEN = 3
_W_RECALL = 1


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class LockUnavailable(RuntimeError):
    """A ``required`` exclusive lock could not be taken."""


@contextmanager
def exclusive_file_lock(lock_path: Path, *, required: bool = False):
    """Hold an exclusive cross-process ``flock`` on ``lock_path`` for the block.

    A dedicated lock file is used (never replaced), so a data file's atomic
    ``os.replace()`` inside the block does not drop the lock.

    ``required=False`` is best-effort: without fcntl (non-POSIX) or when the
    lock file cannot be opened, the block runs unlocked — right for lossy,
    log-damped usage counters. ``required=True`` raises ``LockUnavailable``
    instead when the lock file cannot be opened or locked — for writes into
    curated content, where silently racing is not acceptable. Without fcntl,
    ``required=True`` fails closed; best-effort callers remain unlocked.
    """
    if fcntl is None:
        if required:
            raise LockUnavailable(f"fcntl unavailable for {lock_path}")
        yield
        return
    handle = None
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        # noqa SIM115: the handle must OUTLIVE this block — it carries the
        # flock across the `yield` below and is released in the finally.
        # A `with` would close it immediately and drop the lock.
        handle = open(lock_path, "w")  # noqa: SIM115
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    except OSError as exc:
        if handle is not None:
            handle.close()
        handle = None
        if required:
            raise LockUnavailable(str(lock_path)) from exc
    try:
        yield
    finally:
        if handle is not None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()


def _heat(opens: int, recalls: int) -> float:
    """Frequency-only, log-damped boost factor. No wall-clock decay."""
    return math.log1p(opens * _W_OPEN + recalls * _W_RECALL)


class UsageStore:
    """Reads/writes a ``_usage.json`` sidecar next to the memories.

    Shape::

        {name: {"opens": int, "recalls": int,
                "last_open": iso|null, "last_recall": iso|null}}

    The ``_``-prefix keeps the file out of ``iter_files`` (never parsed as a
    memory) and it is ``.json`` not ``.md`` — doubly safe. Gitignored: usage is
    a per-machine signal, so dev and prod each keep their own hot set.

    Concurrent writes from parallel MCP processes are serialized with a
    ``_usage.json.lock`` companion file (see ``_file_lock``) — gitignore it too.
    """

    FILENAME = "_usage.json"

    def __init__(self, path: Path):
        self.path = Path(path)
        self._data: dict[str, dict] = self._load()

    @classmethod
    def for_memory_dir(cls, memory_dir: str | Path) -> UsageStore:
        return cls(Path(memory_dir) / cls.FILENAME)

    def _load(self) -> dict[str, dict]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}  # missing or corrupt -> start clean, never crash the MCP
        return raw if isinstance(raw, dict) else {}

    def _entry(self, name: str) -> dict:
        return self._data.setdefault(
            name, {"opens": 0, "recalls": 0, "last_open": None, "last_recall": None}
        )

    def _file_lock(self):
        """Hold an exclusive cross-process lock for one read-modify-write cycle.

        Parallel agent sessions each run their own MCP process, all pointing at
        the same ``_usage.json`` — without serialization two near-simultaneous
        hits would clobber each other (last writer wins, hits lost).

        Best-effort (``required=False``): if the lock cannot be taken, the
        cycle runs unlocked rather than crashing — usage is lossy and
        log-damped by design, so degrading to the old race is acceptable; a
        crash is not.
        """
        return exclusive_file_lock(self.path.parent / (self.path.name + ".lock"))

    def record_open(self, name: str) -> None:
        """Strong hit: the full body was fetched (get_memory)."""
        with self._file_lock():
            self._data = self._load()  # re-read under lock before mutating
            entry = self._entry(name)
            entry["opens"] = int(entry.get("opens", 0)) + 1
            entry["last_open"] = _now_iso()
            self._save()

    def record_recall(self, names: list[str]) -> None:
        """Weak hit: surfaced in a recall result (the 'searched for' signal)."""
        if not names:
            return
        with self._file_lock():
            self._data = self._load()  # re-read under lock before mutating
            ts = _now_iso()
            for name in names:
                entry = self._entry(name)
                entry["recalls"] = int(entry.get("recalls", 0)) + 1
                entry["last_recall"] = ts
            self._save()

    def boosts(self) -> dict[str, float]:
        """name -> frecency boost factor, for blending into recall scoring.

        Reads fresh from disk so a parallel session's recorded hits show up at
        the next recall — paired with the MCP mtime-reload (which refreshes
        memory CONTENT), this keeps the hot/cold signal current too."""
        data = self._load()
        return {
            name: _heat(int(e.get("opens", 0)), int(e.get("recalls", 0)))
            for name, e in data.items()
        }

    def report(self) -> list[dict]:
        """Hot -> cold rows for the memory_usage tool and a consolidation reorder.

        Reads fresh from disk (like ``boosts``) so the report reflects writes
        from parallel sessions, not just this process's own hits."""
        rows = [
            {
                "name": name,
                "opens": int(e.get("opens", 0)),
                "recalls": int(e.get("recalls", 0)),
                "last_open": e.get("last_open"),
                "last_recall": e.get("last_recall"),
                "heat": round(_heat(int(e.get("opens", 0)), int(e.get("recalls", 0))), 3),
            }
            for name, e in self._load().items()
        ]
        rows.sort(key=lambda r: (-r["heat"], r["name"]))
        return rows

    def _save(self) -> None:
        tmp = self.path.parent / (self.path.name + ".tmp")
        try:
            tmp.write_text(
                json.dumps(self._data, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            os.replace(tmp, self.path)  # atomic on POSIX
        except OSError:
            pass  # usage is best-effort; never break recall on a write failure


class TelemetryStore:
    """Logs tool latencies, matched record counts, and estimated token savings to JSONL."""

    LOG_FILENAME = "telemetry.jsonl"

    def __init__(self, log_dir: Path):
        self.log_dir = Path(log_dir)
        self.log_file = self.log_dir / self.LOG_FILENAME

    @classmethod
    def default_store(cls, root: Path | None = None) -> TelemetryStore:
        base = root or Path.cwd()
        log_dir = base / ".context" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        return cls(log_dir)

    def record_event(
        self,
        tool: str,
        latency_ms: float,
        matched_count: int,
        delivered_tokens_est: int,
        monolith_tokens_est: int = 2320000,
    ) -> None:
        savings_pct = (
            round((1.0 - (delivered_tokens_est / monolith_tokens_est)) * 100.0, 2)
            if monolith_tokens_est > 0
            else 100.0
        )
        entry = {
            "timestamp": _now_iso(),
            "tool": tool,
            "latency_ms": round(latency_ms, 2),
            "matched_count": matched_count,
            "delivered_tokens": delivered_tokens_est,
            "token_savings_pct": savings_pct,
        }
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass  # Telemetry is strictly fail-soft


def resolve_session_id() -> str:
    """Cross-harness session identity, same fallback chain the shell hooks
    already use (``check-finalization.sh``/``register-codegraph-manifest.sh``):
    Claude Code sets ``CLAUDE_CODE_SESSION_ID`` in the process environment,
    inherited by every child process this engine runs as — the MCP server
    AND the CLI binary the hooks spawn — so no plumbing through CLI args is
    needed to reach it. Codex sets ``CODEX_SESSION_ID``. Falls back to
    ``"unknown"`` rather than raising: an event log entry with an unknown
    session id is still useful data, a crashed recall call is not."""
    return (os.environ.get("CLAUDE_CODE_SESSION_ID")
            or os.environ.get("CODEX_SESSION_ID")
            or "unknown")


class RecallEventLog:
    """Per-session, append-only JSONL log of memory-recall events — Etappe 0
    ("Messen") of the "Sumpf+Baum" memory concept
    (``memory_sumpf_und_baum_lebensechtes_recall.md``): pure observation
    BEFORE any ranking change, so a later evaluation pass has real data to
    replay instead of nothing to compare against. Nothing reads this log
    for a ranking decision — it exists only to be read back offline.

    Logs two kinds of event: ``offered`` (a recall call surfaced these
    names, at these scores, with this many candidates on each side of the
    relevance floor) and ``opened`` (``get_memory(name)`` was actually
    called — the "hit" signal is "opened" following "offered" in the same
    session). ``user_tier_event`` marks the unconditional session-start
    tier dump separately, since it is not a ranked recall (no query, no
    floor) and must never be mistaken for one.

    ONE FILE PER SESSION (``<session_id>.recall_events.jsonl``), not a
    single shared file like ``telemetry.jsonl`` — several sessions run in
    parallel against the same store (parallel-session banner in
    CLAUDE.md), and a stateful write into one shared file across ALL
    sessions would race far more often than within one.

    A single session's OWN file can still see concurrent writers (the CLI
    hook process and the MCP server process both resolve the same
    ``session_id`` and can append near-simultaneously — see
    ``_count_offered()``'s docstring). No lock guards that, unlike
    ``UsageStore``'s locked read-modify-write — not because there is
    nothing shared (the ``turn`` count IS read-then-written across
    processes), but because the worst case of losing that race is a
    duplicated or skipped turn NUMBER, never a corrupted or dropped EVENT:
    ``O_APPEND`` writes to a regular file serialize at the OS level, so
    each JSON line lands whole no matter how the readers/writers
    interleave (measured: 8 concurrent processes, 0 corrupt lines, 0 lost
    events, only non-monotonic turn numbers). A cosmetically wrong turn
    count is an acceptable trade for the complexity a lock would add here
    — matches ``TelemetryStore``'s fail-soft, unlocked append.

    Lives in ``.session_state/`` (a sibling of the memory dir, e.g.
    ``.claude/memory`` -> ``.claude/.session_state``) — the SAME directory
    the auto-recall hooks already use for the per-session
    ``<id>.memory.json`` reference register, not a new discovery path."""

    DIRNAME = ".session_state"

    def __init__(self, session_dir: str | Path, session_id: str):
        self.session_dir = Path(session_dir)
        self.session_id = session_id
        self.log_file = self.session_dir / f"{session_id}.recall_events.jsonl"

    @classmethod
    def for_memory_dir(cls, memory_dir: str | Path, session_id: str | None = None) -> RecallEventLog:
        session_dir = Path(memory_dir).parent / cls.DIRNAME
        return cls(session_dir, session_id or resolve_session_id())

    def _count_offered(self) -> int:
        """How many ``offered`` events this session has already logged —
        the basis for the best-effort ``turn`` counter. Fail-soft against a
        missing or partially-corrupt file: an unparseable line is skipped,
        not fatal (the CLI hook process and the MCP server process CAN
        interleave appends to the same file; each append is atomic, but a
        reader mid-write could in principle see a torn last line)."""
        count = 0
        try:
            with open(self.log_file, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        if json.loads(line).get("event") == "offered":
                            count += 1
                    except json.JSONDecodeError:
                        continue
        except OSError:
            pass
        return count

    def current_turn(self) -> int:
        """Public read of the most recently assigned ``offered`` turn number
        — for a caller (e.g. the CLI) that goes through a helper like
        ``memory_recall_hits()`` and therefore never sees ``log_offered``'s
        own return value directly, but still wants to hand that turn to a
        later ``log_haiku_compare`` call. Safe to call immediately after such
        a helper returns within the SAME synchronous invocation (no other
        process writes to this file in that window); see ``_count_offered``'s
        own docstring for the cross-process race this is otherwise best-effort
        against."""
        return self._count_offered()

    # Bump on any field ADDED/REMOVED/RENAMED to an event's shape — an
    # evaluation pass (Etappe 2) reads this log across a whole store history,
    # and the schema WILL drift mid-flight (W8-Reviewer-Fund, Delta-Runde 2:
    # a live entry from this very session was written before the `excluded`
    # field existed, so real files on disk already mix shapes). Self-
    # describing beats guessing field presence.
    # v4: durable ``scout_comparison_*`` checkpoints make a multi-target
    # learning run resumable after a process dies between a memory replace and
    # its result event. Older readers must continue to accept v1-v3 lines.
    SCHEMA_VERSION = 4

    def _append(self, event: dict) -> bool:
        entry = {"v": self.SCHEMA_VERSION, "ts": _now_iso(),
                  "session_id": self.session_id, **event}
        try:
            self.session_dir.mkdir(parents=True, exist_ok=True)
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            return True
        except OSError:
            pass  # observation must never break recall — fail-soft, like TelemetryStore
        return False

    def log_offered(
        self, query: str, names: list[str], scores: dict[str, float],
        floor_candidates_above: int, floor_candidates_below: int,
        excluded: int = 0,
        sources: dict[str, list[str]] | None = None,
        context_query: dict[str, list[str]] | None = None,
        context_candidates_total: int = 0,
    ) -> int:
        """A recall call surfaced ``names`` to the session — after the
        relevance floor, the limit slice, AND the caller's own
        already-injected exclude set, i.e. exactly what the hook actually
        put in front of the model. ``scores`` is ``{name: final_score}``
        for those same names, so a later evaluation pass can see how close
        the ranking was without re-running the query against a store that
        may have since changed. A curated context-only candidate has no BM25
        score; ``sources`` therefore says which delivered names came from the
        keyword and/or context lane, while ``context_candidates_total`` keeps
        the denominator visible when the narrow context lane was capped.

        ``floor_candidates_above/below`` is the honest denominator:
        ``len(names)`` alone cannot distinguish "nothing else was
        relevant" from "five more strong candidates existed but were cut
        by ``limit``" — both look identical from the offered list alone.

        ``excluded`` (default 0, the MCP `recall` tool has no exclude
        concept) is the size of the caller's already-injected set BEFORE
        this call — ``floor_candidates_above`` is measured before that
        filter runs, ``names`` after it (W8-Reviewer-Fund: without this,
        an empty ``names`` with a nonzero ``floor_candidates_above`` is
        ambiguous between "the floor let candidates through but every one
        was already injected earlier this session" and anything else).

        Returns the ``turn`` number this event was written under, so a
        caller that later wants to attach a comparison sample
        (``log_haiku_compare``) to the SAME turn does not have to guess it
        from a possibly-stale re-count (a fast-following prompt can advance
        the count before a slower comparison call — e.g. an in-session
        Agent — returns)."""
        turn = self._count_offered() + 1
        self._append({
            "turn": turn, "event": "offered", "query": query,
            "names": names, "scores": scores,
            "sources": sources or {name: ["keyword"] for name in names},
            "context_query": context_query or {},
            "context_candidates_total": context_candidates_total,
            "floor_candidates_above": floor_candidates_above,
            "floor_candidates_below": floor_candidates_below,
            "excluded": excluded,
        })
        return turn

    def log_opened(self, name: str) -> None:
        """``get_memory(name)`` was called. Belongs to the turn of the most
        recent ``offered`` event in this session (not a new turn of its
        own) — turn 0 before any recall has happened yet this session."""
        self._append({"turn": self._count_offered(), "event": "opened", "names": [name]})

    def log_haiku_compare(
        self, query: str, mechanical_names: list[str], haiku_names: list[str],
        duration_ms: int, turn: int,
    ) -> None:
        """Measurement sample (Work-Record
        ``memory_haiku_vs_mechanisch_vergleichslauf``): how a bare-keyword
        Haiku search on the SAME prompt would have picked, next to what the
        mechanical keyword+context lane actually offered that turn
        (``log_offered``). Purely an offline comparison signal — never
        injected into the visible recall context — so a later pass can
        answer "at what point does the mechanical lane alone match Haiku
        closely enough to stop running the comparison." Intended caller: an
        in-session Agent call (Haiku for Claude Code, Codex's own equivalent
        for Codex sessions), NOT a hook-spawned standalone ``claude``
        process — the latter re-fires this project's own UserPromptSubmit
        hook and risks recursion (see
        ``hook_spawnt_claude_erbt_eigene_hooks_rekursionsgefahr``). ``turn``
        is the caller's own captured value from ``log_offered``'s return,
        not re-derived here — a recomputed ``_count_offered()`` could have
        already advanced past this turn by the time a slower call returns."""
        self._append({
            "turn": turn, "event": "haiku_compare", "query": query,
            "mechanical_names": mechanical_names, "haiku_names": haiku_names,
            "duration_ms": duration_ms,
        })

    def log_recall_learning(self, turn: int, query: str, result: dict) -> None:
        """Outcome of one ``learn_recall_gap`` call — accepted, noop or
        rejected alike, so convergence of the scout comparison is measurable
        from the log instead of only its successes. ``result`` is the
        ``RecallLearningResult`` as a dict (status, reason, target, proposed
        and added tags, baseline/replay names, ranks)."""
        self._append({"turn": turn, "event": "recall_learning", "query": query, **result})

    def log_scout_comparison(self, event: str, comparison_id: str, turn: int,
                             query: str, **payload: object) -> bool:
        """Append one durable checkpoint for a closed scout comparison.

        ``event`` is deliberately explicit instead of a nested ``phase`` so
        offline readers can stream older event schemas without a second
        dispatch layer. The writer remains fail-soft for ordinary telemetry;
        the comparison core replays a claimed target safely if this append
        was interrupted after the target's atomic memory replace.
        """
        return self._append({"turn": turn, "event": event,
                             "comparison_id": comparison_id, "query": query,
                             **payload})

    def log_user_tier_event(self, names: list[str]) -> None:
        """The unconditional session-start user-tier dump — not a ranked
        recall (no query, no floor), logged as its own event type so an
        evaluation pass never mistakes an always-loaded memory for a
        ranked hit."""
        self._append({"turn": 0, "event": "user_tier_event", "names": names})
