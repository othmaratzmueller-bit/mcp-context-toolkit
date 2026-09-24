import io
import json
import multiprocessing
from pathlib import Path

import pytest

from mcp_context_toolkit.core import parse_frontmatter
from mcp_context_toolkit.memory import MemoryEngine, memory_recall_hits
from mcp_context_toolkit.recall_learning import (
    ScoutHit,
    UnsafeFrontmatter,
    add_tags_to_frontmatter,
    complete_scout_comparison,
    learn_recall_gap,
    preview_learned_tag_ablation,
)
from mcp_context_toolkit.usage import RecallEventLog

TARGET = "honesty_rule"
QUERY = "agent fabricated result"


def _write(d: Path, name: str, text: str) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{name}.md"
    path.write_text(text, encoding="utf-8")
    return path


def _store(tmp_path: Path) -> Path:
    """A store where TARGET shares no token with QUERY, and four distractors
    own 'agent' and 'result' — the mechanical lane cannot reach TARGET."""
    mem = tmp_path / ".claude" / "memory"
    _write(mem, TARGET, (
        f"---\nname: {TARGET}\ndescription: report outcomes truthfully\n"
        "type: feedback\nmodified: 2026-09-21T22:26:28.182Z\n---\n\n"
        "Never claim a verification you did not perform.\n"
    ))
    for i in range(4):
        _write(mem, f"agent_note_{i}", (
            f"---\nname: agent_note_{i}\ndescription: agent result handling {i}\n---\n\n"
            f"The agent returns a result. agent result agent {i}\n"
        ))
    return mem


def _learn(mem: Path, **overrides):
    kwargs = {
        "query": QUERY, "target_name": TARGET, "bridge_terms": ["fabricated"],
        "scout_names": [TARGET], "limit": 2, "boosts": {},
    }
    kwargs.update(overrides)
    return learn_recall_gap(MemoryEngine.from_directory(mem), **kwargs)


def _recalled(mem: Path, query: str = QUERY, limit: int = 2) -> list[str]:
    engine = MemoryEngine.from_directory(mem)
    return [m.name for m in memory_recall_hits(engine, query, limit=limit, boosts={})]


class TestLearnAccepted:
    def test_gap_is_learned_and_recall_then_delivers_target(self, tmp_path: Path):
        mem = _store(tmp_path)
        assert TARGET not in _recalled(mem)

        result = _learn(mem)

        assert result.status == "accepted"
        assert result.reason == "replay_verified"
        assert result.added_tags == ["fabricated"]
        assert result.before_rank is None
        assert result.after_rank is not None and result.after_rank <= 2
        assert TARGET not in result.baseline_names
        assert TARGET in result.replay_names
        meta, _ = parse_frontmatter((mem / f"{TARGET}.md").read_text(encoding="utf-8"))
        assert meta["learned_tags"] == ["fabricated"]
        assert "tags" not in meta           # curated tags stay untouched
        assert TARGET in _recalled(mem)  # the productive path, reloaded from disk

    def test_only_the_effective_subset_is_written(self, tmp_path: Path):
        mem = _store(tmp_path)
        result = _learn(mem, bridge_terms=["agent", "fabricated"])
        assert result.status == "accepted"
        # 'agent' is carried by every distractor: it alone cannot lift the
        # target, and 'fabricated' alone can — so 'agent' is never persisted.
        assert result.proposed_tags == ["agent", "fabricated"]
        assert result.added_tags == ["fabricated"]

    def test_member_slug_resolves_to_its_package(self, tmp_path: Path):
        # Scouts report the atomic slug a package absorbed; recall ranks and
        # delivers the package, so that is where the tag has to land.
        mem = _store(tmp_path)
        _write(mem, "honesty_pkg", (
            "---\nname: honesty_pkg\nmetadata:\n  type: feedback\n  members:\n"
            "    - claim-only-what-you-ran\n---\n\n## claim_only_what_you_ran\n"
            "Report outcomes truthfully.\n"
        ))
        result = _learn(mem, target_name="claim-only-what-you-ran",
                        scout_names=["claim-only-what-you-ran"])
        assert (result.status, result.target) == ("accepted", "honesty_pkg")
        assert result.requested_target == "claim-only-what-you-ran"
        meta, _ = parse_frontmatter((mem / "honesty_pkg.md").read_text(encoding="utf-8"))
        assert meta["metadata"]["learned_tags"] == ["fabricated"]
        assert "honesty_pkg" in _recalled(mem)
        # Idempotent through the slug too: the package is now delivered.
        again = _learn(mem, target_name="claim-only-what-you-ran",
                       scout_names=["claim-only-what-you-ran"])
        assert (again.status, again.reason) == ("noop", "target_already_recalled")

    def test_body_and_other_metadata_are_byte_identical(self, tmp_path: Path):
        mem = _store(tmp_path)
        path = mem / f"{TARGET}.md"
        before = path.read_text(encoding="utf-8")
        _learn(mem)
        after = path.read_text(encoding="utf-8")
        assert "modified: 2026-09-21T22:26:28.182Z\n" in after
        removed = [line for line in before.splitlines() if line not in after.splitlines()]
        assert removed == []
        assert after.endswith("Never claim a verification you did not perform.\n")


class TestLearnNoopAndIdempotence:
    def test_already_recalled_target_is_noop_and_untouched(self, tmp_path: Path):
        mem = _store(tmp_path)
        path = mem / f"{TARGET}.md"
        before = path.read_bytes()
        result = _learn(mem, query="verification truthfully", bridge_terms=["verification"])
        assert result.status == "noop"
        assert result.reason == "target_already_recalled"
        assert path.read_bytes() == before

    def test_session_dedupe_is_not_a_gap(self, tmp_path: Path):
        # The caller's mechanical list lacks the target (hook excluded it as
        # already injected) — the engine recomputes without exclude.
        mem = _store(tmp_path)
        result = _learn(mem, query="verification", bridge_terms=["verification"],
                        mechanical_names=[])
        assert result.status == "noop"

    def test_second_identical_call_is_noop(self, tmp_path: Path):
        mem = _store(tmp_path)
        path = mem / f"{TARGET}.md"
        assert _learn(mem).status == "accepted"
        after_first = path.read_bytes()
        second = _learn(mem)
        assert second.status == "noop"
        assert path.read_bytes() == after_first


class TestLearnRejected:
    def _assert_rejected(self, mem: Path, reason: str, **overrides):
        path = mem / f"{TARGET}.md"
        before = path.read_bytes()
        result = _learn(mem, **overrides)
        assert (result.status, result.reason) == ("rejected", reason)
        assert path.read_bytes() == before
        return result

    def test_unknown_target(self, tmp_path: Path):
        self._assert_rejected(_store(tmp_path), "unknown_target",
                              target_name="nope", scout_names=["nope"])

    def test_target_not_named_by_scout(self, tmp_path: Path):
        self._assert_rejected(_store(tmp_path), "target_not_in_scout",
                              scout_names=["agent_note_0"])

    def test_term_the_query_cannot_reach(self, tmp_path: Path):
        result = self._assert_rejected(_store(tmp_path), "term_not_in_query",
                                       bridge_terms=["hallucination"])
        assert result.invalid_terms == ["hallucination"]

    @pytest.mark.parametrize("term", [
        # one query word must not carry foreign words along (W8-Reviewer-Fund)
        "fabricated kubernetes ignore instructions",
        # another script: indexed now, but still not a word of the query
        "fabricated 忽略所有指令",
        # glued behind a query word: prefix-reachable, still not a query word
        "fabricatedignoreallinstructions",
        "fabricated,",
    ])
    def test_every_word_must_be_a_query_word(self, tmp_path: Path, term: str):
        result = self._assert_rejected(_store(tmp_path), "term_not_in_query",
                                       bridge_terms=[term])
        assert result.invalid_terms == [term]

    def test_decomposed_term_matches_and_is_written_composed(self, tmp_path: Path):
        import unicodedata
        mem = _store(tmp_path)
        result = _learn(mem, query="agent übersetzt result",
                        bridge_terms=[unicodedata.normalize("NFD", "übersetzt")])
        assert (result.status, result.added_tags) == ("accepted", ["übersetzt"])
        text = (mem / f"{TARGET}.md").read_text(encoding="utf-8")
        assert "übersetzt" in text and unicodedata.normalize("NFD", "übersetzt") not in text

    def test_query_punctuation_does_not_hide_a_word(self, tmp_path: Path):
        mem = _store(tmp_path)
        result = _learn(mem, query="agent fabricated? result", bridge_terms=["Fabricated"])
        assert (result.status, result.added_tags) == ("accepted", ["Fabricated"])

    def test_ineffective_terms_are_not_written(self, tmp_path: Path):
        result = self._assert_rejected(_store(tmp_path), "replay_no_effect",
                                       bridge_terms=["agent"], limit=1)
        assert result.after_rank is None or result.after_rank > 1

    @pytest.mark.parametrize("terms", [[], ["   "], ["x" * 61], ["!!!"],
                                       ["a1", "a2", "a3", "a4", "a5", "a6"]])
    def test_invalid_terms(self, tmp_path: Path, terms):
        self._assert_rejected(_store(tmp_path), "invalid_terms", bridge_terms=terms)

    def test_no_new_terms(self, tmp_path: Path):
        mem = _store(tmp_path)
        path = mem / f"{TARGET}.md"
        path.write_text(path.read_text(encoding="utf-8").replace(
            "type: feedback\n", "type: feedback\ntags:\n  - Agent\n"), encoding="utf-8")
        self._assert_rejected(mem, "no_new_terms", bridge_terms=["agent"], limit=1)

    def test_already_learned_term_is_not_new(self, tmp_path: Path):
        mem = _store(tmp_path)
        path = mem / f"{TARGET}.md"
        path.write_text(path.read_text(encoding="utf-8").replace(
            "type: feedback\n", "type: feedback\nlearned_tags:\n  - Agent\n"), encoding="utf-8")
        self._assert_rejected(mem, "no_new_terms", bridge_terms=["agent"], limit=1)

    def test_unsafe_frontmatter_is_fail_closed(self, tmp_path: Path):
        mem = _store(tmp_path)
        path = mem / f"{TARGET}.md"
        path.write_text(path.read_text(encoding="utf-8").replace(
            "type: feedback\n", "type: feedback\nlearned_tags: not-a-list\n"), encoding="utf-8")
        self._assert_rejected(mem, "unsafe_frontmatter")


class TestConcurrency:
    def test_unwritable_lock_rejects_without_memory_write_and_logs_reason(self, tmp_path: Path):
        mem = _store(tmp_path)
        # The state directory is a regular file, so the required lock cannot
        # create its lock path. Keep the event log elsewhere to prove the
        # rejected outcome is still observable.
        state = tmp_path / ".claude" / ".session_state"
        state.write_text("not a directory", encoding="utf-8")
        log = RecallEventLog(tmp_path / "events", "s1")
        before = (mem / f"{TARGET}.md").read_bytes()

        result = learn_recall_gap(
            MemoryEngine.from_directory(mem), query=QUERY, target_name=TARGET,
            bridge_terms=["fabricated"], scout_names=[TARGET], limit=2, boosts={},
            event_log=log, turn=4,
        )

        assert (result.status, result.reason) == ("rejected", "lock_unavailable")
        assert (mem / f"{TARGET}.md").read_bytes() == before
        event = json.loads(log.log_file.read_text(encoding="utf-8").strip())
        assert (event["event"], event["reason"]) == ("recall_learning", "lock_unavailable")

    def test_foreign_tag_written_after_load_survives(self, tmp_path: Path):
        mem = _store(tmp_path)
        engine = MemoryEngine.from_directory(mem)  # snapshot BEFORE the foreign write
        path = mem / f"{TARGET}.md"
        path.write_text(path.read_text(encoding="utf-8").replace(
            "type: feedback\n", "type: feedback\nlearned_tags:\n  - foreign\n"), encoding="utf-8")
        result = learn_recall_gap(engine, query=QUERY, target_name=TARGET,
                                  bridge_terms=["fabricated"], scout_names=[TARGET],
                                  limit=2, boosts={})
        assert result.status == "accepted"
        meta, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        assert meta["learned_tags"] == ["foreign", "fabricated"]

    def test_unlocked_writer_during_replay_is_not_overwritten(self, tmp_path: Path, monkeypatch):
        # A host edit does not take the learner's lock; it can land while the
        # replays run. The learner must notice and back off (W8-Reviewer-Fund).
        from mcp_context_toolkit import recall_learning

        mem = _store(tmp_path)
        path = mem / f"{TARGET}.md"
        real_replay = recall_learning._replay

        def replay_with_foreign_edit(*args, **kwargs):
            path.write_text(path.read_text(encoding="utf-8") + "host edit\n", encoding="utf-8")
            return real_replay(*args, **kwargs)

        monkeypatch.setattr(recall_learning, "_replay", replay_with_foreign_edit)
        result = _learn(mem)
        assert (result.status, result.reason) == ("rejected", "target_changed")
        assert path.read_text(encoding="utf-8").endswith("host edit\n")
        assert "fabricated" not in path.read_text(encoding="utf-8")

    def test_parallel_learners_lose_no_update(self, tmp_path: Path):
        mem = _store(tmp_path)
        jobs = [(str(mem), "agent fabricated result", "fabricated"),
                (str(mem), "agent invented result", "invented")]
        with multiprocessing.get_context("spawn").Pool(2) as pool:
            statuses = pool.starmap(_learn_in_process, jobs)
        assert statuses == ["accepted", "accepted"]
        meta, _ = parse_frontmatter((mem / f"{TARGET}.md").read_text(encoding="utf-8"))
        assert sorted(meta["learned_tags"]) == ["fabricated", "invented"]


def _learn_in_process(mem: str, query: str, term: str) -> str:
    return learn_recall_gap(MemoryEngine.from_directory(mem), query=query,
                            target_name=TARGET, bridge_terms=[term],
                            scout_names=[TARGET], limit=2, boosts={}).status


def _complete_in_process(mem: str, state: str) -> str:
    """Spawn-safe worker for the same comparison-id retry race test."""
    result = complete_scout_comparison(
        MemoryEngine.from_directory(mem), query=QUERY, turn=11, limit=2,
        mechanical_names=["agent_note_0"],
        scout_hits=[{"target_name": TARGET, "bridge_terms": ["fabricated"]}],
        duration_ms=0, event_log=RecallEventLog(state, "same-session"), boosts={},
    )
    return result.results[0].status


class TestEvent:
    def test_every_outcome_is_logged_with_evidence(self, tmp_path: Path):
        mem = _store(tmp_path)
        log = RecallEventLog(tmp_path / "state", "s1")
        learn_recall_gap(MemoryEngine.from_directory(mem), query=QUERY, target_name=TARGET,
                         bridge_terms=["fabricated"], scout_names=[TARGET],
                         mechanical_names=["agent_note_0"], limit=2, boosts={},
                         event_log=log, turn=7)
        learn_recall_gap(MemoryEngine.from_directory(mem), query=QUERY, target_name="nope",
                         bridge_terms=["fabricated"], scout_names=["nope"], limit=2,
                         boosts={}, event_log=log, turn=8)
        events = [json.loads(line) for line in log.log_file.read_text(encoding="utf-8").splitlines()]
        assert [e["event"] for e in events] == ["recall_learning", "recall_learning"]
        first, second = events
        assert first["v"] == RecallEventLog.SCHEMA_VERSION == 4
        assert (first["turn"], first["status"], first["target"]) == (7, "accepted", TARGET)
        assert first["query"] == QUERY
        assert first["added_tags"] == ["fabricated"]
        assert first["mechanical_names"] == ["agent_note_0"]
        assert TARGET in first["replay_names"] and TARGET not in first["baseline_names"]
        assert (second["turn"], second["status"], second["reason"]) == (8, "rejected", "unknown_target")


class TestScoutComparison:
    def _complete(self, mem: Path, log: RecallEventLog, **overrides):
        kwargs = {
            "query": QUERY, "turn": 7, "limit": 2,
            "mechanical_names": ["agent_note_0"],
            "scout_hits": [ScoutHit(target_name=TARGET, bridge_terms=["fabricated"])],
            "duration_ms": 12, "event_log": log, "boosts": {},
        }
        kwargs.update(overrides)
        return complete_scout_comparison(MemoryEngine.from_directory(mem), **kwargs)

    def test_one_call_learns_then_duplicate_turn_is_idempotent(self, tmp_path: Path):
        mem = _store(tmp_path)
        log = RecallEventLog(tmp_path / "state", "s1")
        first = self._complete(mem, log)
        events_after_first = log.log_file.read_text(encoding="utf-8").splitlines()
        second = self._complete(mem, log)

        assert (first.resumed, first.results[0].status) == (False, "accepted")
        assert (second.resumed, second.results[0].status) == (True, "accepted")
        assert log.log_file.read_text(encoding="utf-8").splitlines() == events_after_first
        meta, _ = parse_frontmatter((mem / f"{TARGET}.md").read_text(encoding="utf-8"))
        assert meta["learned_tags"] == ["fabricated"]

    def test_mixed_targets_complete_as_one_comparison(self, tmp_path: Path):
        mem = _store(tmp_path)
        log = RecallEventLog(tmp_path / "state", "s1")
        result = self._complete(mem, log, scout_hits=[
            {"target_name": TARGET, "bridge_terms": ["fabricated"]},
            {"target_name": "missing", "bridge_terms": ["fabricated"]},
        ])
        assert [r.status for r in result.results] == ["accepted", "rejected"]
        assert result.results[1].reason == "unknown_target"
        events = [json.loads(line)["event"] for line in log.log_file.read_text(encoding="utf-8").splitlines()]
        assert events == ["scout_comparison_started", "scout_comparison_claimed",
                          "scout_comparison_result", "scout_comparison_claimed",
                          "scout_comparison_result", "scout_comparison_completed"]

    def test_crash_after_replace_recovers_claim_without_second_write(self, tmp_path: Path, monkeypatch):
        mem = _store(tmp_path)
        log = RecallEventLog(tmp_path / "state", "s1")
        original = log.log_scout_comparison
        failed = False

        def crash_once(event, *args, **kwargs):
            nonlocal failed
            if event == "scout_comparison_result" and not failed:
                failed = True
                raise RuntimeError("crash after replace")
            return original(event, *args, **kwargs)

        monkeypatch.setattr(log, "log_scout_comparison", crash_once)
        with pytest.raises(RuntimeError, match="after replace"):
            self._complete(mem, log)
        assert "fabricated" in (mem / f"{TARGET}.md").read_text(encoding="utf-8")
        monkeypatch.setattr(log, "log_scout_comparison", original)

        recovered = self._complete(mem, log)
        assert recovered.results[0].reason == "recovered_after_replace"
        events = [json.loads(line) for line in log.log_file.read_text(encoding="utf-8").splitlines()]
        assert [e.get("recovered") for e in events if e["event"] == "scout_comparison_result"] == [True]

    def test_same_comparison_id_with_different_payload_fails_closed(self, tmp_path: Path):
        mem = _store(tmp_path)
        log = RecallEventLog(tmp_path / "state", "s1")
        self._complete(mem, log)
        before = (mem / f"{TARGET}.md").read_bytes()
        with pytest.raises(ValueError, match="different payload"):
            self._complete(mem, log, mechanical_names=["different"])
        assert (mem / f"{TARGET}.md").read_bytes() == before

    def test_invalid_payload_cannot_claim_a_comparison(self, tmp_path: Path):
        mem = _store(tmp_path)
        log = RecallEventLog(tmp_path / "state", "s1")
        with pytest.raises(ValueError, match="duration_ms"):
            self._complete(mem, log, duration_ms=-1)
        assert not log.log_file.exists()

    def test_concurrent_same_id_retries_write_one_claim_and_one_result(self, tmp_path: Path):
        mem = _store(tmp_path)
        state = tmp_path / "state"
        with multiprocessing.get_context("spawn").Pool(2) as pool:
            statuses = pool.starmap(_complete_in_process, [(str(mem), str(state))] * 2)
        assert statuses == ["accepted", "accepted"]
        events = [json.loads(line) for line in next(state.glob("*.recall_events.jsonl")).read_text().splitlines()]
        assert sum(e["event"] == "scout_comparison_claimed" for e in events) == 1
        assert sum(e["event"] == "scout_comparison_result" for e in events) == 1


class TestLearnedTagAblation:
    def test_replay_keeps_a_tag_needed_by_its_original_gap(self, tmp_path: Path):
        mem = _store(tmp_path)
        log = RecallEventLog(tmp_path / "state", "s1")
        result = complete_scout_comparison(
            MemoryEngine.from_directory(mem), query=QUERY, turn=7, limit=2,
            mechanical_names=["agent_note_0"],
            scout_hits=[{"target_name": TARGET, "bridge_terms": ["fabricated"]}],
            duration_ms=0, event_log=log, boosts={},
        )
        assert result.results[0].status == "accepted"

        next_session = RecallEventLog(log.session_dir, "s2")
        preview = preview_learned_tag_ablation(MemoryEngine.from_directory(mem),
                                               event_log=next_session)

        assert [(item.tag, item.status) for item in preview.tags] == [("fabricated", "necessary")]
        assert preview.tags[0].with_ranks[0] <= 2
        assert preview.tags[0].without_ranks == [None]

    def test_missing_or_corrupt_history_is_not_decidable(self, tmp_path: Path):
        mem = _store(tmp_path)
        path = mem / f"{TARGET}.md"
        path.write_text(path.read_text(encoding="utf-8").replace(
            "type: feedback\n", "type: feedback\nlearned_tags: [fabricated]\n",
        ), encoding="utf-8")
        log = RecallEventLog(tmp_path / "state", "s1")
        log.session_dir.mkdir(parents=True)
        log.log_file.write_text("{broken json}\n", encoding="utf-8")

        preview = preview_learned_tag_ablation(MemoryEngine.from_directory(mem), event_log=log)

        assert [(item.tag, item.status) for item in preview.tags] == [("fabricated", "not_decidable")]

    def test_redundant_tags_are_candidates_and_jointly_redundant(self, tmp_path: Path):
        mem = _store(tmp_path)
        path = mem / f"{TARGET}.md"
        path.write_text(path.read_text(encoding="utf-8").replace(
            "type: feedback\n", "type: feedback\nlearned_tags: [fabricated, result]\n",
        ).replace("Never claim a verification you did not perform.",
                  "agent fabricated result agent fabricated result."), encoding="utf-8")
        log = RecallEventLog(tmp_path / "state", "s1")
        for comparison_id, tag in (("s1:7", "fabricated"), ("s1:8", "result")):
            assert log.log_scout_comparison(
                "scout_comparison_claimed", comparison_id, 7, QUERY,
                target_name=TARGET, limit=2,
            )
            assert log.log_scout_comparison(
                "scout_comparison_result", comparison_id, 7, QUERY,
                target_name=TARGET,
                result={"status": "accepted", "target": TARGET, "added_tags": [tag]},
            )

        preview = preview_learned_tag_ablation(MemoryEngine.from_directory(mem), event_log=log)

        assert {(item.tag, item.status) for item in preview.tags} == {
            ("fabricated", "candidate"), ("result", "candidate"),
        }
        assert preview.jointly_redundant == {TARGET: ["fabricated", "result"]}


class TestFrontmatterEdit:
    def _added(self, text: str, add: list[str]) -> tuple[dict, str]:
        new_text = add_tags_to_frontmatter(text, add)
        old_lines = text.splitlines()
        new_lines = new_text.splitlines()
        assert all(line in new_lines for line in old_lines if "tags" not in line)
        return parse_frontmatter(new_text)[0], new_text

    def test_flat_block_list_keeps_its_style(self):
        meta, new = self._added("---\nname: a\ntags:\n    - one\n---\nbody\n", ["two"])
        assert meta["tags"] == ["one", "two"]
        assert "    - two\n" in new

    def test_flat_flow_list(self):
        meta, _ = self._added("---\nname: a\ntags: [one, 'x y']\n---\nbody\n", ["two"])
        assert meta["tags"] == ["one", "x y", "two"]

    def test_nested_metadata_tags(self):
        text = ("---\nname: a\nmetadata:\n  node_type: memory\n  tags:\n    - one\n"
                "  type: feedback\n  modified: 2026-09-21T22:26:28.182Z\n---\nbody\n")
        meta, new = self._added(text, ["two"])
        assert meta["metadata"]["tags"] == ["one", "two"]
        assert "  modified: 2026-09-21T22:26:28.182Z\n" in new

    def test_nested_metadata_without_tags_gets_them_there(self):
        meta, _ = self._added("---\nname: a\nmetadata:\n  type: feedback\n---\nbody\n", ["two"])
        assert meta["metadata"]["tags"] == ["two"]
        assert "tags" not in meta

    def test_flat_without_tags_gets_top_level(self):
        meta, _ = self._added("---\nname: a\ntype: feedback\n---\nbody\n", ["two"])
        assert meta["tags"] == ["two"]

    def test_special_characters_are_quoted(self):
        meta, _ = self._added("---\nname: a\n---\nbody\n", ["a: b", "#x", "lügt"])
        assert meta["tags"] == ["a: b", "#x", "lügt"]

    def test_crlf_is_preserved(self):
        new = add_tags_to_frontmatter("---\r\nname: a\r\n---\r\nbody\r\n", ["two"])
        assert "\ntags:" in new and "\r\n  - two\r\n" in new

    @pytest.mark.parametrize("text", [
        "no frontmatter\n",
        "---\nname: a\ntags: text\n---\nbody\n",
        "---\nname: a\nmetadata: {type: feedback}\n---\nbody\n",
    ])
    def test_unsafe_shapes_raise(self, text: str):
        with pytest.raises(UnsafeFrontmatter):
            add_tags_to_frontmatter(text, ["two"])

    @pytest.mark.parametrize("text", [
        # an anchor on the node: rewriting it would orphan the alias
        "---\nname: a\ntags: &t\n  - one\nother: *t\n---\nbody\n",
        # YAML keeps the LAST duplicate key; a line edit hits the first
        "---\nname: a\ntags:\n  - one\ntags:\n  - zwei\n---\nbody\n",
    ])
    def test_only_the_reparse_check_catches_these(self, text: str):
        # These pass every structural precondition; the edit itself goes
        # wrong, and only the re-parse comparison stops it being written.
        with pytest.raises(UnsafeFrontmatter):
            add_tags_to_frontmatter(text, ["two"])


class TestCliAdapter:
    """The MCP adapter is covered in test_mcp_server.py next to its helpers."""

    def _run_cli(self, monkeypatch, mem: Path, *extra: str) -> int:
        from mcp_context_toolkit.cli import main

        monkeypatch.delenv("CONTEXT_USER_MEMORY_DIR", raising=False)
        monkeypatch.setattr("sys.argv", [
            "context-toolkit-query", "--memory-dir", str(mem),
            "--learn-recall-gap", TARGET, "--learn-query", QUERY,
            "--learn-scout-name", TARGET, "--learn-scout-name", "agent_note_1",
            "--learn-mechanical-name", "agent_note_0", "--limit", "2", *extra,
        ])
        with pytest.raises(SystemExit) as exc:
            main()
        return int(exc.value.code or 0)

    def test_cli_accepts_logs_and_leaves_usage_alone(self, tmp_path: Path, monkeypatch, capsys):
        mem = _store(tmp_path)
        code = self._run_cli(monkeypatch, mem, "--learn-term", "fabricated",
                             "--learn-turn", "3")
        out = json.loads(capsys.readouterr().out)
        assert code == 0
        assert (out["status"], out["added_tags"]) == ("accepted", ["fabricated"])
        assert not (mem / "_usage.json").exists()
        log = next((tmp_path / ".claude" / ".session_state").glob("*.recall_events.jsonl"))
        event = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
        assert (event["event"], event["turn"]) == ("recall_learning", 3)
        assert event["scout_names"] == [TARGET, "agent_note_1"]

    def test_cli_rejection_exits_2(self, tmp_path: Path, monkeypatch, capsys):
        mem = _store(tmp_path)
        code = self._run_cli(monkeypatch, mem, "--learn-term", "hallucination")
        out = json.loads(capsys.readouterr().out)
        assert code == 2
        assert out["reason"] == "term_not_in_query"

    def test_cli_completes_a_structured_comparison_from_stdin(self, tmp_path: Path, monkeypatch, capsys):
        from mcp_context_toolkit.cli import main

        mem = _store(tmp_path)
        monkeypatch.delenv("CONTEXT_USER_MEMORY_DIR", raising=False)
        monkeypatch.setattr("sys.argv", [
            "context-toolkit-query", "--memory-dir", str(mem),
            "--complete-scout-comparison",
        ])
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({
            "query": QUERY, "turn": 3, "limit": 2,
            "mechanical_names": ["agent_note_0"],
            "scout_hits": [{"target_name": TARGET, "bridge_terms": ["fabricated"]}],
        })))
        with pytest.raises(SystemExit) as exc:
            main()
        out = json.loads(capsys.readouterr().out)
        assert exc.value.code == 0
        assert out["results"][0]["status"] == "accepted"
