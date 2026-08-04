from pathlib import Path

import pytest

from mcp_context_toolkit.core import KnowledgeLoadError
from mcp_context_toolkit.work import WorkEngine


def _write(d: Path, name: str, fm: str, body: str = "") -> None:
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(f"---\n{fm}\n---\n\n{body}\n", encoding="utf-8")


class TestLoading:
    def test_loads_a_flat_goal(self, tmp_path: Path):
        _write(tmp_path, "a.md",
               "key: a\ntitle: A\nkind: goal\nstatus: open\ntheme: engine\n"
               "opened: 2026-07-01")
        e = WorkEngine.from_directory(tmp_path)
        assert [i.key for i in e.items] == ["a"]
        assert e.get("a").title == "A"
        assert e.get("missing") is None

    def test_unknown_theme_is_rejected_not_silently_degraded(self, tmp_path: Path):
        # The lesson from Memory's `type: package` (73/119 records fell to
        # "misc" un-flagged): an unknown theme must fail LOUDLY, not degrade.
        _write(tmp_path, "a.md",
               "key: a\ntitle: A\nkind: goal\nstatus: open\ntheme: not_a_real_theme\n"
               "opened: 2026-07-01")
        with pytest.raises(KnowledgeLoadError):
            WorkEngine.from_directory(tmp_path, strict=True)

    def test_unknown_theme_is_reported_not_silently_dropped_when_lenient(self, tmp_path: Path):
        # strict=False must not make the bad record vanish without a trace —
        # it has to surface in the returned error list.
        _write(tmp_path, "a.md",
               "key: a\ntitle: A\nkind: goal\nstatus: open\ntheme: not_a_real_theme\n"
               "opened: 2026-07-01")
        engine = WorkEngine()
        result = engine.load_directory(tmp_path, strict=False)
        assert result["added"] == 0
        assert len(result["errors"]) == 1
        assert "a.md" in result["errors"][0]

    def test_errors_survive_on_the_engine_via_from_directory(self, tmp_path: Path):
        # F2 (2026-07-28 review): from_directory() discards load_directory's
        # return dict, and mcp_server.py's production loader only ever keeps
        # the ENGINE (never the dict) — so a dropped record must be visible
        # through engine.errors too, or it vanishes with zero trace on the
        # only path a real server actually uses.
        _write(tmp_path, "a.md",
               "key: a\ntitle: A\nkind: goal\nstatus: open\ntheme: not_a_real_theme\n"
               "opened: 2026-07-01")
        e = WorkEngine.from_directory(tmp_path, strict=False)
        assert len(e.errors) == 1
        assert "a.md" in e.errors[0]

    def test_load_errors_surface_in_lint(self, tmp_path: Path):
        # A dropped record isn't cosmetic: it can make an unrelated goal look
        # falsely closeable (its only still-open child never loaded). lint()
        # must report the drop right next to closeable_goals, not bury it.
        _write(tmp_path, "a.md",
               "key: a\ntitle: A\nkind: goal\nstatus: open\ntheme: not_a_real_theme\n"
               "opened: 2026-07-01")
        e = WorkEngine.from_directory(tmp_path, strict=False)
        assert len(e.lint()["load_errors"]) == 1

    def test_duplicate_key_is_skipped_not_silently_overwritten(self, tmp_path: Path):
        _write(tmp_path, "a.md",
               "key: dup\ntitle: First\nkind: goal\nstatus: open\ntheme: engine\n"
               "opened: 2026-07-01")
        _write(tmp_path, "b.md",
               "key: dup\ntitle: Second\nkind: goal\nstatus: open\ntheme: engine\n"
               "opened: 2026-07-01")
        e = WorkEngine.from_directory(tmp_path)
        assert len(e.items) == 1
        assert e.get("dup").title == "First"  # first-loaded wins, deterministic (sorted iter_files)

    def test_nested_metadata_style_is_read(self, tmp_path: Path):
        # Same two-frontmatter-styles convention as memory.py's _meta_get.
        _write(tmp_path, "a.md",
               "key: a\ntitle: A\nmetadata:\n  kind: step\n  theme: chat\n"
               "  part_of: parent_x\nstatus: open\nopened: 2026-07-01")
        e = WorkEngine.from_directory(tmp_path)
        item = e.get("a")
        assert item.kind == "step"
        assert item.theme == "chat"
        assert item.part_of == "parent_x"

    def test_nested_metadata_key_is_read_not_taken_from_filename(self, tmp_path: Path):
        # F3 (2026-07-28 review): key was the only field still reading
        # meta.get() directly instead of _meta_get() — a key nested under
        # metadata: silently fell back to the filename stem instead, loading
        # the record under the WRONG key with no error at all.
        _write(tmp_path, "some_filename.md",
               "title: A\nmetadata:\n  key: real_key\n  theme: engine\n"
               "status: open\nopened: 2026-07-01")
        e = WorkEngine.from_directory(tmp_path)
        assert e.get("real_key") is not None
        assert e.get("some_filename") is None


class TestHierarchyAndRollup:
    def _goal_with_children(self, tmp_path: Path, child_statuses: list[str]):
        _write(tmp_path, "goal.md",
               "key: goal\ntitle: G\nkind: goal\nstatus: active\ntheme: engine\n"
               "opened: 2026-07-01")
        for i, status in enumerate(child_statuses):
            closed = "\nclosed: 2026-07-10" if status in ("done", "dropped", "superseded") else ""
            evidence = "\nevidence: {commits: [c1]}" if status == "done" else ""
            _write(tmp_path, f"step{i}.md",
                   f"key: step{i}\ntitle: S{i}\nkind: step\nstatus: {status}\n"
                   f"theme: engine\nopened: 2026-07-01\npart_of: goal{closed}{evidence}")
        return WorkEngine.from_directory(tmp_path)

    def test_median_case_is_a_leaf_with_no_children(self, tmp_path: Path):
        # Measured on the real backlog: median sub-points per item is ZERO.
        # A goal with no children must not appear in any hierarchy-shaped
        # finding — it is simply done or not, on its own status.
        e = self._goal_with_children(tmp_path, [])
        assert e.children("goal") == []
        assert e.rollup("goal") == {"total": 0, "open": 0, "active": 0,
                                     "done": 0, "dropped": 0, "superseded": 0}
        assert e.lint()["closeable_goals"] == []  # nothing to roll up

    def test_rollup_separates_done_from_dropped(self, tmp_path: Path):
        # The owner's explicit ask: "7 von 24 verworfen" and "10 von 24
        # erledigt" must stay two different numbers, not one "resolved" count.
        e = self._goal_with_children(tmp_path, ["done", "done", "dropped", "open"])
        assert e.rollup("goal") == {"total": 4, "open": 1, "active": 0,
                                     "done": 2, "dropped": 1, "superseded": 0}

    def test_rollup_keeps_active_separate_from_open(self, tmp_path: Path):
        # F4 (2026-07-28 review): active used to be silently folded into
        # open — the same collapse the store exists to stop doing one level
        # down (done vs. dropped vs. superseded already stay separate).
        e = self._goal_with_children(tmp_path, ["open", "active", "active"])
        assert e.rollup("goal") == {"total": 3, "open": 1, "active": 2,
                                     "done": 0, "dropped": 0, "superseded": 0}

    def test_closeable_goal_is_the_zolltarif_case(self, tmp_path: Path):
        # The plan's sharpest single finding: a 2026-07-02 audit already
        # recommended closing this exact item and nothing acted on it. This
        # is the mechanical detector for that failure mode.
        e = self._goal_with_children(tmp_path, ["done", "done"])
        assert e.lint()["closeable_goals"] == ["goal"]

    def test_one_open_child_blocks_closing(self, tmp_path: Path):
        e = self._goal_with_children(tmp_path, ["done", "done", "open"])
        assert e.lint()["closeable_goals"] == []


class TestLint:
    def test_dangling_refs_across_all_four_relation_fields(self, tmp_path: Path):
        _write(tmp_path, "a.md",
               "key: a\ntitle: A\nkind: step\nstatus: open\ntheme: engine\n"
               "opened: 2026-07-01\npart_of: ghost1\namends: ghost2\n"
               "supersedes: ghost3\nblocked_by: [ghost4]")
        e = WorkEngine.from_directory(tmp_path)
        assert set(e.lint()["dangling_refs"]["a"]) == {"ghost1", "ghost2", "ghost3", "ghost4"}

    def test_valid_refs_are_not_flagged(self, tmp_path: Path):
        _write(tmp_path, "parent.md",
               "key: parent\ntitle: P\nkind: goal\nstatus: open\ntheme: engine\n"
               "opened: 2026-07-01")
        _write(tmp_path, "child.md",
               "key: child\ntitle: C\nkind: step\nstatus: open\ntheme: engine\n"
               "opened: 2026-07-01\npart_of: parent")
        e = WorkEngine.from_directory(tmp_path)
        assert e.lint()["dangling_refs"] == {}

    def test_missing_evidence_on_a_done_item(self, tmp_path: Path):
        _write(tmp_path, "a.md",
               "key: a\ntitle: A\nkind: step\nstatus: done\ntheme: engine\n"
               "opened: 2026-07-01\nclosed: 2026-07-05")
        e = WorkEngine.from_directory(tmp_path)
        assert e.lint()["missing_evidence"] == ["a"]

    def test_evidence_present_is_not_flagged(self, tmp_path: Path):
        _write(tmp_path, "a.md",
               "key: a\ntitle: A\nkind: step\nstatus: done\ntheme: engine\n"
               "opened: 2026-07-01\nclosed: 2026-07-05\nevidence: {files: [x.py]}")
        e = WorkEngine.from_directory(tmp_path)
        assert e.lint()["missing_evidence"] == []

    def test_unclosed_pair_both_directions(self, tmp_path: Path):
        # closed set but status still open, AND status closed but no closed date.
        _write(tmp_path, "a.md",
               "key: a\ntitle: A\nkind: step\nstatus: open\ntheme: engine\n"
               "opened: 2026-07-01\nclosed: 2026-07-05")
        _write(tmp_path, "b.md",
               "key: b\ntitle: B\nkind: step\nstatus: done\ntheme: engine\n"
               "opened: 2026-07-01\nevidence: {files: [x.py]}")
        e = WorkEngine.from_directory(tmp_path)
        assert set(e.lint()["unclosed_pairs"]) == {"a", "b"}


class TestKantenUndPlanPath:
    """Die zwei Pruefungen, die 2026-08-04 dazukamen — beide aus einem echten Fund.

    `plan_path` ist die EINZIGE Verbindung zwischen Plan-Dateien und Work-Store,
    und nichts prueft sie: gemessen zeigten 8 Records auf `plans/todo/...`-Dateien,
    die der plan-keeper nach `plans/begonnen/` verschoben hatte — waehrend
    derselbe Lint-Lauf `dangling_refs: 0` meldete. `dangling_refs` deckt bewusst
    nur WORK-KEYS ab; ein Dateipfad hatte schlicht keinen Pruefer.

    `kanten` trennt "gesucht und nichts gefunden" von "nie gesucht". Ohne diese
    Trennung waere ein Pflichtfeld wertlos — man fuellte es mit erfundenen
    Kanten, weil das Feld billig zu faelschen ist und die Suche nicht.
    """

    def test_plan_path_auf_existierende_datei_ist_ok(self, tmp_path: Path):
        plan = tmp_path / ".claude" / "plans" / "x.md"
        plan.parent.mkdir(parents=True)
        plan.write_text("# Plan\n", encoding="utf-8")
        work = tmp_path / ".claude" / "work"
        _write(work, "a.md",
               "key: a\ntitle: A\nkind: goal\nstatus: open\ntheme: engine\n"
               "opened: 2026-01-01\nplan_path: .claude/plans/x.md")
        assert WorkEngine.from_directory(work).lint()["plan_path_broken"] == {}

    def test_plan_path_ins_leere_wird_gemeldet(self, tmp_path: Path):
        work = tmp_path / ".claude" / "work"
        _write(work, "a.md",
               "key: a\ntitle: A\nkind: goal\nstatus: open\ntheme: engine\n"
               "opened: 2026-01-01\nplan_path: .claude/plans/weg.md")
        kaputt = WorkEngine.from_directory(work).lint()["plan_path_broken"]
        assert kaputt == {"a": ".claude/plans/weg.md"}

    def test_ohne_plan_path_kein_falscher_alarm(self, tmp_path: Path):
        work = tmp_path / ".claude" / "work"
        _write(work, "a.md",
               "key: a\ntitle: A\nkind: goal\nstatus: open\ntheme: engine\n"
               "opened: 2026-01-01")
        assert WorkEngine.from_directory(work).lint()["plan_path_broken"] == {}

    def test_kanten_werden_gelesen(self, tmp_path: Path):
        _write(tmp_path, "a.md",
               "key: a\ntitle: A\nkind: step\nstatus: active\ntheme: engine\n"
               "opened: 2026-01-01\nkanten:\n  gesucht:\n    - 'anonymize PII'\n"
               "  nutzt:\n    - services/privacy_filter.py::anonymize")
        item = WorkEngine.from_directory(tmp_path).get("a")
        assert item is not None and item.kanten is not None
        assert item.kanten.nutzt == ["services/privacy_filter.py::anonymize"]
        assert item.kanten.gesucht == ["anonymize PII"]

    def test_gesucht_ohne_treffer_zaehlt_als_geprueft(self, tmp_path: Path):
        """Der Kern: leere Listen sind ein ERGEBNIS, kein fehlender Eintrag."""
        _write(tmp_path, "a.md",
               "key: a\ntitle: A\nkind: step\nstatus: active\ntheme: engine\n"
               "opened: 2026-01-01\nkanten:\n  gesucht:\n    - 'nichts dergleichen'")
        e = WorkEngine.from_directory(tmp_path)
        assert e.get("a").kanten is not None
        assert e.lint()["reuse_ungeprueft"] == []

    def test_leerer_kanten_block_gilt_NICHT_als_geprueft(self, tmp_path: Path):
        """Sonst genuegte `kanten: {}` um das Gate zu umgehen."""
        _write(tmp_path, "a.md",
               "key: a\ntitle: A\nkind: step\nstatus: active\ntheme: engine\n"
               "opened: 2026-01-01\nkanten:\n  gesucht: []\n  nutzt: []")
        e = WorkEngine.from_directory(tmp_path)
        assert e.get("a").kanten is None
        assert e.lint()["reuse_ungeprueft"] == ["a"]

    def test_string_statt_liste_ist_ein_FEHLER_kein_gate_oeffner(self, tmp_path: Path):
        """Der teuerste Tippfehler: `gesucht: text` statt `gesucht:\n  - text`.

        `list("anonymize PII")` ergibt 13 Einzelbuchstaben — das Gate waere
        erfuellt, ohne dass je gesucht wurde. Fail-OPEN auf genau dem Feld, das
        nicht billig faelschbar sein soll (Reviewer-Fund 2026-08-04).
        """
        _write(tmp_path, "a.md",
               "key: a\ntitle: A\nkind: step\nstatus: active\ntheme: engine\n"
               "opened: 2026-01-01\nkanten:\n  gesucht: anonymize PII")
        engine = WorkEngine()
        engine.load_directory(tmp_path, strict=False)
        # Der Record faellt raus UND der Grund steht in load_errors — nicht still.
        assert engine.get("a") is None
        assert any("kanten.gesucht" in e for e in engine.errors), engine.errors

    def test_string_in_anderen_kanten_feldern_ebenso(self, tmp_path: Path):
        _write(tmp_path, "a.md",
               "key: a\ntitle: A\nkind: step\nstatus: active\ntheme: engine\n"
               "opened: 2026-01-01\nkanten:\n  nutzt: services/x.py::y")
        engine = WorkEngine()
        engine.load_directory(tmp_path, strict=False)
        assert engine.get("a") is None
        assert any("kanten.nutzt" in e for e in engine.errors), engine.errors

    def test_korrekte_liste_bleibt_unberuehrt(self, tmp_path: Path):
        """Gegenprobe: der Guard darf den Normalfall nicht mitreissen."""
        _write(tmp_path, "a.md",
               "key: a\ntitle: A\nkind: step\nstatus: active\ntheme: engine\n"
               "opened: 2026-01-01\nkanten:\n  gesucht:\n    - anonymize PII")
        item = WorkEngine.from_directory(tmp_path).get("a")
        assert item is not None and item.kanten is not None
        assert item.kanten.gesucht == ["anonymize PII"]

    def test_plan_path_auch_bei_relativem_store_pfad(self, tmp_path: Path,
                                                     monkeypatch):
        """`parent.parent` ohne `resolve()` meldete bei relativem Aufrufpfad
        JEDEN plan_path als kaputt (gemessen: 1563 Falschtreffer)."""
        plan = tmp_path / ".claude" / "plans" / "x.md"
        plan.parent.mkdir(parents=True)
        plan.write_text("# Plan\n", encoding="utf-8")
        work = tmp_path / ".claude" / "work"
        _write(work, "a.md",
               "key: a\ntitle: A\nkind: goal\nstatus: open\ntheme: engine\n"
               "opened: 2026-01-01\nplan_path: .claude/plans/x.md")
        monkeypatch.chdir(tmp_path / ".claude")
        assert WorkEngine.from_directory("work").lint()["plan_path_broken"] == {}

    def test_nur_active_wird_gemeldet_nicht_open(self, tmp_path: Path):
        """`open` hat den Moment noch nicht erreicht, an dem die Frage etwas
        aendert — alle 878 offenen zu listen ersaeufte die, auf die es ankommt."""
        _write(tmp_path, "offen.md",
               "key: offen\ntitle: O\nkind: step\nstatus: open\ntheme: engine\n"
               "opened: 2026-01-01")
        _write(tmp_path, "aktiv.md",
               "key: aktiv\ntitle: A\nkind: step\nstatus: active\ntheme: engine\n"
               "opened: 2026-01-01")
        assert WorkEngine.from_directory(tmp_path).lint()["reuse_ungeprueft"] == ["aktiv"]


class TestSupersedesChain:
    def test_superseded_idea_stays_readable_alongside_its_replacement(self, tmp_path: Path):
        # The owner's core ask: a dropped idea is never overwritten — its
        # status flips and the new one points back, so the old state is
        # still legible.
        _write(tmp_path, "old.md",
               "key: old\ntitle: Erster Ansatz\nkind: goal\nstatus: superseded\n"
               "theme: engine\nopened: 2026-06-01\nclosed: 2026-07-01")
        _write(tmp_path, "new.md",
               "key: new\ntitle: Zweiter Ansatz\nkind: goal\nstatus: active\n"
               "theme: engine\nopened: 2026-07-01\nsupersedes: old")
        e = WorkEngine.from_directory(tmp_path)
        assert e.get("old").status == "superseded"
        assert e.get("new").supersedes == "old"
        assert e.lint()["dangling_refs"] == {}


class TestRecall:
    def test_title_hit_outranks_body_only_hit(self, tmp_path: Path):
        # A query matching the TITLE should beat one that only mentions the
        # term once in a long body — that's what _W_TITLE > _W_BODY encodes.
        _write(tmp_path, "a.md",
               "key: a\ntitle: Work-Store Engine\nkind: goal\nstatus: open\n"
               "theme: engine\nopened: 2026-07-01",
               body="Ein Datenmodell fuer Plaene, ganz allgemein gehalten.")
        _write(tmp_path, "b.md",
               "key: b\ntitle: Irgendwas anderes\nkind: goal\nstatus: open\n"
               "theme: engine\nopened: 2026-07-01",
               body="Hier kommt das Wort Engine nur einmal beilaeufig vor.")
        e = WorkEngine.from_directory(tmp_path)
        results = e.recall("engine")
        assert [i.key for i in results] == ["a", "b"]

    def test_no_match_returns_empty(self, tmp_path: Path):
        _write(tmp_path, "a.md",
               "key: a\ntitle: Work-Store Engine\nkind: goal\nstatus: open\n"
               "theme: engine\nopened: 2026-07-01")
        e = WorkEngine.from_directory(tmp_path)
        assert e.recall("voellig unpassender begriff xyz") == []

    def test_recall_is_symmetric_with_snake_case_query(self, tmp_path: Path):
        # Regression pin for the exact bug the shared core.split_token fixes:
        # a query for the compound must not double-count against a title
        # that names the parts separately.
        _write(tmp_path, "a.md",
               "key: a\ntitle: deploy_workflow ueberarbeiten\nkind: goal\n"
               "status: open\ntheme: engine\nopened: 2026-07-01")
        e = WorkEngine.from_directory(tmp_path)
        results = e.recall("deploy_workflow")
        assert [i.key for i in results] == ["a"]
