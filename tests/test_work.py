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


class TestWorkItemFrontmatterStrenge:
    """Etappe B (2026-08-19) — dieselbe Bauart wie der `kanten`-Zweig, eine
    Ebene hoeher: der Loader liest WorkItem-Felder NAMENTLICH, `extra="forbid"`
    am Model waere hier wirkungslos. Vorab gemessen (0 Treffer ueber ~1900
    Records): `source_path` steht bei 89 Alt-Records im Frontmatter (redundant,
    wird immer aus dem echten Pfad ueberschrieben) — die Pruefung darf das
    NICHT als unbekannt werten, sonst bricht der Bestand beim naechsten Start."""

    def test_unbekanntes_top_level_feld_ist_ein_fehler(self, tmp_path: Path):
        _write(tmp_path, "a.md",
               "key: a\ntitle: A\nkind: step\nstatus: open\ntheme: engine\n"
               "opened: 2026-01-01\nstatus_hinweis: das Feld gab es nie")
        engine = WorkEngine()
        engine.load_directory(tmp_path, strict=False)
        assert engine.get("a") is None
        assert any("status_hinweis" in e for e in engine.errors), engine.errors

    def test_source_path_im_frontmatter_ist_kein_fehler(self, tmp_path: Path):
        """Regressionsschutz fuer den Vorab-Befund: 89 Records tragen ein
        redundantes `source_path:`, das der Loader ohnehin immer mit dem
        echten Pfad ueberschreibt — kein Informationsverlust, kein Grund zu
        werfen."""
        _write(tmp_path, "a.md",
               "key: a\ntitle: A\nkind: step\nstatus: open\ntheme: engine\n"
               "opened: 2026-01-01\nsource_path: irgendein/alter/pfad.md")
        e = WorkEngine.from_directory(tmp_path)
        assert e.get("a") is not None
        assert e.get("a").source_path == str(tmp_path / "a.md")

    def test_braucht_owner_default_ist_false(self, tmp_path: Path):
        _write(tmp_path, "a.md",
               "key: a\ntitle: A\nkind: step\nstatus: open\ntheme: engine\n"
               "opened: 2026-01-01")
        assert WorkEngine.from_directory(tmp_path).get("a").braucht_owner is False

    def test_braucht_owner_wird_gelesen(self, tmp_path: Path):
        _write(tmp_path, "a.md",
               "key: a\ntitle: A\nkind: step\nstatus: open\ntheme: engine\n"
               "opened: 2026-01-01\nbraucht_owner: true")
        assert WorkEngine.from_directory(tmp_path).get("a").braucht_owner is True

    def test_entblockt_durch_wird_gelesen(self, tmp_path: Path):
        _write(tmp_path, "a.md",
               "key: a\ntitle: A\nkind: step\nstatus: open\ntheme: engine\n"
               "opened: 2026-01-01\nentblockt_durch: eine_andere_entscheidung")
        item = WorkEngine.from_directory(tmp_path).get("a")
        assert item.entblockt_durch == "eine_andere_entscheidung"

    def test_entblockt_durch_default_ist_none(self, tmp_path: Path):
        _write(tmp_path, "a.md",
               "key: a\ntitle: A\nkind: step\nstatus: open\ntheme: engine\n"
               "opened: 2026-01-01")
        assert WorkEngine.from_directory(tmp_path).get("a").entblockt_durch is None

    def test_aus_migration_default_ist_false(self, tmp_path: Path):
        _write(tmp_path, "a.md",
               "key: a\ntitle: A\nkind: step\nstatus: open\ntheme: engine\n"
               "opened: 2026-01-01")
        assert WorkEngine.from_directory(tmp_path).get("a").aus_migration is False

    def test_aus_migration_wird_gelesen(self, tmp_path: Path):
        # Work-Record work_migrator_opened_stempel_traegt_das_gate: macht
        # das plan-keeper-Reuse-Gate robust gegen die bisher unausgesprochene
        # `opened`-Konvention, statt sich allein auf sie zu verlassen.
        _write(tmp_path, "a.md",
               "key: a\ntitle: A\nkind: step\nstatus: open\ntheme: engine\n"
               "opened: 2026-01-01\naus_migration: true")
        assert WorkEngine.from_directory(tmp_path).get("a").aus_migration is True

    @pytest.mark.parametrize("kind", ["decision", "changelog"])
    def test_neue_kinds_werden_geladen(self, tmp_path: Path, kind: str):
        _write(tmp_path, "a.md",
               f"key: a\ntitle: A\nkind: {kind}\nstatus: done\ntheme: engine\n"
               "opened: 2026-01-01\nclosed: 2026-01-01\n"
               "evidence: {files: [x.md]}")
        assert WorkEngine.from_directory(tmp_path).get("a").kind == kind

    def test_body_im_frontmatter_ist_ein_fehler(self, tmp_path: Path):
        """W8-Reviewer-Fund WARNING-3: `body` gehoert zu `WorkItem.model_fields`,
        darf aber NICHT in der Allow-List stehen — anders als `source_path`
        waere ein Frontmatter-`body:` kein folgenloses Echo, sondern ERSETZTE
        den echten Markdown-Inhalt lautlos (die 108-Dateien-Klasse aus
        v1.4.314: `body: |` als YAML-Key statt als Text nach `---`)."""
        _write(tmp_path, "a.md",
               "key: a\ntitle: A\nkind: step\nstatus: open\ntheme: engine\n"
               "opened: 2026-01-01\nbody: 'im Frontmatter, nicht als Markdown'")
        engine = WorkEngine()
        engine.load_directory(tmp_path, strict=False)
        assert engine.get("a") is None
        assert any("body" in e for e in engine.errors), engine.errors


class TestArchivedFiltering:
    def _five_statuses(self, tmp_path: Path):
        for i, status in enumerate(["open", "active", "done", "dropped", "superseded"]):
            closed = "\nclosed: 2026-07-10" if status in ("done", "dropped", "superseded") else ""
            evidence = "\nevidence: {commits: [c1]}" if status == "done" else ""
            _write(tmp_path, f"i{i}.md",
                   f"key: i{i}\ntitle: I{i}\nkind: step\nstatus: {status}\n"
                   f"theme: engine\nopened: 2026-07-01{closed}{evidence}")
        return WorkEngine.from_directory(tmp_path)

    def test_default_view_excludes_closed_statuses(self, tmp_path: Path):
        # Owner 2026-08-18: "wenn ich frage was im work steht, wir NUR die
        # anzeigen sollten, die offen sind ... der Rest ist als Archiv
        # durchaus brauchbar" — closed items stay on disk, they just don't
        # clutter the default "what's open" view.
        e = self._five_statuses(tmp_path)
        assert sorted(i.key for i in e.list()) == ["i0", "i1"]

    def test_explicit_status_still_finds_a_closed_item(self, tmp_path: Path):
        # The archive is not hidden, just not the DEFAULT — an explicit
        # status= filter must still reach done/dropped/superseded.
        e = self._five_statuses(tmp_path)
        assert [i.key for i in e.list(status="done")] == ["i2"]
        assert [i.key for i in e.list(status="dropped")] == ["i3"]
        assert [i.key for i in e.list(status="superseded")] == ["i4"]

    def test_include_archived_returns_everything(self, tmp_path: Path):
        e = self._five_statuses(tmp_path)
        assert sorted(i.key for i in e.list(include_archived=True)) == \
            ["i0", "i1", "i2", "i3", "i4"]

    def test_theme_filter_still_combines_with_the_new_default(self, tmp_path: Path):
        # Guards against the default-exclusion silently swallowing an
        # unrelated filter instead of composing with it.
        _write(tmp_path, "other.md",
               "key: other\ntitle: O\nkind: step\nstatus: open\n"
               "theme: chat\nopened: 2026-07-01")
        e = self._five_statuses(tmp_path)
        assert [i.key for i in e.list(theme="engine")] == ["i0", "i1"]


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

    @pytest.mark.parametrize("kind", ["decision", "changelog"])
    def test_missing_evidence_excludes_decision_and_changelog(self, tmp_path: Path, kind: str):
        """W8-Reviewer-Fund WARNING-4 (Etappe D): eine migrierte historische
        Karte hat kein Commit, auf das evidence zeigen koennte — der Backfill
        (2377 Changelog-Karten) begrub sonst die 29 echten Faelle unter fast
        30-fachem Rauschen. Dieselbe Ausnahme wie bereits bei
        reuse_ungeprueft (Etappe B)."""
        _write(tmp_path, "a.md",
               f"key: a\ntitle: A\nkind: {kind}\nstatus: done\ntheme: engine\n"
               "opened: 2026-07-01\nclosed: 2026-07-05")
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

    def test_body_link_to_missing_key_is_flagged(self, tmp_path: Path):
        # Owner-decision 2026-08-16 (work_lint_prueft_prosa_links_nicht):
        # a [[...]] in a work item's body must resolve to another work key —
        # never a memory. This is the case that surfaced the whole record:
        # a Prosa-[[...]] pointing at a memory read exactly like a dangling
        # cross-record reference, but neither dangling_refs (structured
        # fields only) nor memory.py's broken_links (memory tree only)
        # ever checked it.
        _write(tmp_path, "a.md",
               "key: a\ntitle: A\nkind: step\nstatus: open\ntheme: engine\n"
               "opened: 2026-07-01",
               body="Siehe [[ghost_memory_or_typo]] fuer Kontext.")
        e = WorkEngine.from_directory(tmp_path)
        assert e.lint()["body_link_broken"] == {"a": ["ghost_memory_or_typo"]}

    def test_body_link_to_existing_work_key_is_not_flagged(self, tmp_path: Path):
        _write(tmp_path, "parent.md",
               "key: parent\ntitle: P\nkind: goal\nstatus: open\ntheme: engine\n"
               "opened: 2026-07-01")
        _write(tmp_path, "child.md",
               "key: child\ntitle: C\nkind: step\nstatus: open\ntheme: engine\n"
               "opened: 2026-07-01",
               body="Verwandt: [[parent]].")
        e = WorkEngine.from_directory(tmp_path)
        assert e.lint()["body_link_broken"] == {}

    def test_body_without_links_is_not_flagged(self, tmp_path: Path):
        _write(tmp_path, "a.md",
               "key: a\ntitle: A\nkind: step\nstatus: open\ntheme: engine\n"
               "opened: 2026-07-01",
               body="Ganz normaler Text ohne jede Doppelklammer.")
        e = WorkEngine.from_directory(tmp_path)
        assert e.lint()["body_link_broken"] == {}

    def test_body_link_self_reference_is_not_flagged(self, tmp_path: Path):
        _write(tmp_path, "a.md",
               "key: a\ntitle: A\nkind: step\nstatus: open\ntheme: engine\n"
               "opened: 2026-07-01",
               body="Dieser Eintrag verweist auf sich selbst: [[a]].")
        e = WorkEngine.from_directory(tmp_path)
        assert e.lint()["body_link_broken"] == {}

    def test_body_link_padding_md_suffix_and_kebab_case_all_resolve(self, tmp_path: Path):
        # Reviewer-fund (2026-08-16): the first cut compared raw match text
        # against by_key and flagged all three of these as broken even
        # though the target key genuinely exists — the same normalization
        # memory.py applies (strip padding, drop `.md`, fold kebab->snake)
        # must apply here too, via the now-shared core.norm_link.
        _write(tmp_path, "target_key.md",
               "key: target_key\ntitle: T\nkind: step\nstatus: open\n"
               "theme: engine\nopened: 2026-07-01")
        _write(tmp_path, "child.md",
               "key: child\ntitle: C\nkind: step\nstatus: open\ntheme: engine\n"
               "opened: 2026-07-01",
               body="[[ target_key ]] und [[target_key.md]] und [[target-key]].")
        e = WorkEngine.from_directory(tmp_path)
        assert e.lint()["body_link_broken"] == {}

    @pytest.mark.parametrize("kind", ["decision", "changelog"])
    def test_body_link_broken_excludes_decision_and_changelog(self, tmp_path: Path, kind: str):
        # Etappe E: a migrated changelog card's VERBATIM body can contain an
        # incidental "[[TALOS_THOUGHT]]"-style placeholder mention that reads
        # like a cross-record link but is prose about code, not a real one.
        _write(tmp_path, "a.md",
               f"key: a\ntitle: A\nkind: {kind}\nstatus: done\ntheme: engine\n"
               "opened: 2026-01-01\nclosed: 2026-01-01",
               body="Placeholder [[TALOS_THOUGHT]] wird eingesetzt.")
        e = WorkEngine.from_directory(tmp_path)
        assert e.lint()["body_link_broken"] == {}

    def test_kanten_gebrochen_is_none_without_symbol_check(self, tmp_path: Path):
        # Not run vs. run-and-clean must stay distinguishable (Work-Record
        # work_kanten_ohne_ankerpruefung) — the default, cheap lint() call
        # never triggers the injected check.
        _write(tmp_path, "a.md",
               "key: a\ntitle: A\nkind: step\nstatus: open\ntheme: engine\n"
               "opened: 2026-07-01\nkanten:\n  gesucht:\n    - x\n"
               "  nutzt:\n    - services/foo.py::bar")
        e = WorkEngine.from_directory(tmp_path)
        assert e.lint()["kanten_gebrochen"] is None

    def test_kanten_gebrochen_reports_only_unhealthy_states(self, tmp_path: Path):
        _write(tmp_path, "a.md",
               "key: a\ntitle: A\nkind: step\nstatus: open\ntheme: engine\n"
               "opened: 2026-07-01\nkanten:\n  gesucht:\n    - x\n"
               "  nutzt:\n    - ok.py::vorhanden\n  erweitert:\n    - weg.py::verschwunden\n"
               "  baut:\n    - neu.py::kommt_noch\n  modul:\n    - ein_modul")

        def check(typ: str, ziel: str) -> tuple:
            werte = {
                ("nutzt", "ok.py::vorhanden"): ("ok", None),
                ("erweitert", "weg.py::verschwunden"): ("gebrochen", "weg"),
                ("baut", "neu.py::kommt_noch"): ("geplant", None),
                ("modul", "ein_modul"): ("ok", None),
            }
            return werte[(typ, ziel)]

        e = WorkEngine.from_directory(tmp_path)
        kg = e.lint(check)["kanten_gebrochen"]
        # ok + geplant sind gesund und fehlen; nur gebrochen erscheint.
        assert kg == {"a": [{"typ": "erweitert", "ziel": "weg.py::verschwunden",
                              "zustand": "gebrochen", "hinweis": "weg"}]}

    def test_kanten_gebrochen_never_calls_check_for_gesucht(self, tmp_path: Path):
        # `gesucht` entries are search terms, not anchors, and cannot "break".
        _write(tmp_path, "a.md",
               "key: a\ntitle: A\nkind: step\nstatus: open\ntheme: engine\n"
               "opened: 2026-07-01\nkanten:\n  gesucht:\n    - 'anonymize PII'")
        gesehen = []

        def check(typ: str, ziel: str) -> tuple:
            gesehen.append((typ, ziel))
            return "ok", None

        e = WorkEngine.from_directory(tmp_path)
        assert e.lint(check)["kanten_gebrochen"] == {}
        assert gesehen == []

    def test_kanten_gebrochen_empty_dict_when_run_and_clean(self, tmp_path: Path):
        _write(tmp_path, "a.md",
               "key: a\ntitle: A\nkind: step\nstatus: open\ntheme: engine\n"
               "opened: 2026-07-01\nkanten:\n  gesucht:\n    - x\n"
               "  nutzt:\n    - services/foo.py::bar")
        e = WorkEngine.from_directory(tmp_path)
        assert e.lint(lambda _typ, _ziel: ("ok", None))["kanten_gebrochen"] == {}


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

    def test_gefunden_und_entscheidung_werden_geladen(self, tmp_path: Path):
        """Der Dreisatz aus CLAUDE.md muss vollstaendig ankommen.

        Vor v1.4.474 kannte `WorkEdges` nur fuenf Felder; `gefunden`/
        `entscheidung` wurden vom Loader kommentarlos uebergangen. Sechs
        Records im Bestand hatten sie befuellt — der Nachweis stand in der
        Datei und fehlte im Index.
        """
        _write(tmp_path, "a.md",
               "key: a\ntitle: A\nkind: step\nstatus: active\ntheme: engine\n"
               "opened: 2026-01-01\nkanten:\n"
               "  gesucht:\n    - 'gibt es schon einen Splitter?'\n"
               "  gefunden:\n    - 'services/x.py::split — Mixin, verworfen'\n"
               "  entscheidung: 'Neubau, weil hier keine Klasse vorliegt'")
        item = WorkEngine.from_directory(tmp_path).get("a")
        assert item is not None and item.kanten is not None
        assert item.kanten.gefunden == ["services/x.py::split — Mixin, verworfen"]
        assert item.kanten.entscheidung == "Neubau, weil hier keine Klasse vorliegt"

    def test_unbekannter_kanten_schluessel_ist_ein_fehler(self, tmp_path: Path):
        """Ein vertippter Schluessel darf nicht spurlos verschwinden.

        Der Loader liest die Felder NAMENTLICH auf — ohne diese Pruefung
        erreicht Unbekanntes das Model nie, und `extra="forbid"` dort waere
        wirkungslos. Genau so gingen `gefunden`/`entscheidung` verloren.
        """
        _write(tmp_path, "a.md",
               "key: a\ntitle: A\nkind: step\nstatus: active\ntheme: engine\n"
               "opened: 2026-01-01\nkanten:\n"
               "  gesucht:\n    - x\n  gefunded:\n    - y")
        engine = WorkEngine()
        engine.load_directory(tmp_path, strict=False)
        assert engine.get("a") is None
        assert any("gefunded" in e for e in engine.errors), engine.errors

    def test_entscheidung_allein_oeffnet_das_gate_NICHT(self, tmp_path: Path):
        """Ein Satz ist das billigste Feld hier — er darf nicht als Suche zaehlen.

        Sonst waere `kanten: {entscheidung: 'passt schon'}` der neue
        Schleichweg, den `kanten: {}` bereits einmal war.

        EHRLICH DAZU: dieser Test ist KEIN Regressionstest fuer v1.4.474 — er
        war auch vorher gruen, allerdings aus dem falschen Grund (das Feld
        existierte nicht, der Block blieb leer). Er schuetzt nach vorne: gegen
        eine kuenftige Aenderung, die `entscheidung` in `gesucht_worden()`
        aufnimmt, weil es "ja auch zum Dreisatz gehoert".
        """
        _write(tmp_path, "a.md",
               "key: a\ntitle: A\nkind: step\nstatus: active\ntheme: engine\n"
               "opened: 2026-01-01\nkanten:\n  entscheidung: 'passt schon'")
        assert WorkEngine.from_directory(tmp_path).get("a").kanten is None

    def test_entscheidung_als_liste_ist_ein_fehler(self, tmp_path: Path):
        """Eine Liste darf nicht still zur Python-repr werden.

        `str(["Neubau", "X erweitern"])` ergibt `"['Neubau', 'X erweitern']"` —
        eine Zeichenkette, die als Nachweis ausgeliefert wird und wie ein Satz
        aussieht, ohne einer zu sein. Genau die stille Toleranz, gegen die
        `_als_liste` ein Feld weiter gebaut wurde. (Reviewer-Fund 2026-08-11.)
        """
        _write(tmp_path, "a.md",
               "key: a\ntitle: A\nkind: step\nstatus: active\ntheme: engine\n"
               "opened: 2026-01-01\nkanten:\n  gesucht:\n    - x\n"
               "  entscheidung:\n    - Neubau\n    - X erweitern")
        engine = WorkEngine()
        engine.load_directory(tmp_path, strict=False)
        assert engine.get("a") is None
        assert any("kanten.entscheidung" in e for e in engine.errors), engine.errors

    def test_entscheidung_als_text_bleibt_unberuehrt(self, tmp_path: Path):
        """Gegenprobe: der Guard darf den Normalfall nicht mitreissen."""
        _write(tmp_path, "a.md",
               "key: a\ntitle: A\nkind: step\nstatus: active\ntheme: engine\n"
               "opened: 2026-01-01\nkanten:\n  gesucht:\n    - x\n"
               "  entscheidung: '  Neubau, weil keine Klasse vorliegt  '")
        item = WorkEngine.from_directory(tmp_path).get("a")
        assert item is not None and item.kanten is not None
        assert item.kanten.entscheidung == "Neubau, weil keine Klasse vorliegt"

    def test_gefunden_allein_zaehlt_als_gesucht(self, tmp_path: Path):
        """Eine Trefferliste IST das Ergebnis einer Suche — anders als ein Satz."""
        _write(tmp_path, "a.md",
               "key: a\ntitle: A\nkind: step\nstatus: active\ntheme: engine\n"
               "opened: 2026-01-01\nkanten:\n  gefunden:\n    - 'services/x.py::y'")
        assert WorkEngine.from_directory(tmp_path).get("a").kanten is not None

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

    @pytest.mark.parametrize("kind", ["decision", "changelog"])
    def test_reuse_ungeprueft_nimmt_decision_und_changelog_aus(self, tmp_path: Path, kind: str):
        """W8-Reviewer-Fund WARNING-4: `kanten` ist ein VORWAERTS-Feld — eine
        migrierte historische Tatsache hat vor ihrem eigenen Entstehen nichts
        gesucht. Ohne diese Ausnahme widerspraeche der Engine-Lint der in
        talos-plan-keeper.md dokumentierten Gate-Ausnahme."""
        _write(tmp_path, "a.md",
               f"key: a\ntitle: A\nkind: {kind}\nstatus: active\ntheme: engine\n"
               "opened: 2026-01-01")
        assert WorkEngine.from_directory(tmp_path).lint()["reuse_ungeprueft"] == []

    def test_entblockt_durch_auf_unbekannten_key_wird_gemeldet(self, tmp_path: Path):
        """W8-Reviewer-Fund WARNING-2: ohne diese Pruefung hebt jeder truthy
        String — auch ein Tippfehler — einen Owner-Vorbehalt lautlos auf."""
        _write(tmp_path, "a.md",
               "key: a\ntitle: A\nkind: step\nstatus: open\ntheme: engine\n"
               "opened: 2026-01-01\nentblockt_durch: gibt_es_nicht")
        assert WorkEngine.from_directory(tmp_path).lint()["dangling_refs"] == \
            {"a": ["gibt_es_nicht"]}

    def test_entblockt_durch_auf_existierenden_key_ist_ok(self, tmp_path: Path):
        _write(tmp_path, "entscheidung.md",
               "key: entscheidung\ntitle: E\nkind: step\nstatus: done\n"
               "theme: engine\nopened: 2026-01-01\nclosed: 2026-01-02\n"
               "evidence: {files: [x.md]}")
        _write(tmp_path, "a.md",
               "key: a\ntitle: A\nkind: step\nstatus: open\ntheme: engine\n"
               "opened: 2026-01-01\nplan_path: irgendwas.md\n"
               "entblockt_durch: entscheidung")
        assert WorkEngine.from_directory(tmp_path).lint()["dangling_refs"] == {}


class TestZustellungFuerDatei:
    """`fuer_datei` — offene Punkte zur angefassten Datei (2026-08-05).

    Aus "hier sind deine Regeln" wird "hier sind deine Regeln und die N offenen
    Punkte, die jemand fuer diese Datei notiert hat".
    """

    def test_kanten_treffer(self, tmp_path: Path):
        _write(tmp_path, "a.md",
               "key: a\ntitle: A\nkind: step\nstatus: open\ntheme: engine\n"
               "opened: 2026-01-01\nkanten:\n  gesucht:\n    - x\n"
               "  nutzt:\n    - backend/app/services/x.py::foo")
        t = WorkEngine.from_directory(tmp_path).fuer_datei("backend/app/services/x.py")
        assert [i.key for i in t] == ["a"]

    def test_evidence_files_treffer(self, tmp_path: Path):
        _write(tmp_path, "a.md",
               "key: a\ntitle: A\nkind: step\nstatus: open\ntheme: engine\n"
               "opened: 2026-01-01\nevidence:\n  commits: []\n"
               "  files:\n    - backend/app/services/x.py")
        t = WorkEngine.from_directory(tmp_path).fuer_datei("backend/app/services/x.py")
        assert [i.key for i in t] == ["a"]

    def test_geschlossene_records_bleiben_draussen(self, tmp_path: Path):
        """Ein erledigter Punkt ist keine Arbeit mehr."""
        _write(tmp_path, "a.md",
               "key: a\ntitle: A\nkind: step\nstatus: done\nclosed: 2026-01-02\n"
               "theme: engine\nopened: 2026-01-01\nevidence:\n  commits: [abc]\n"
               "  files:\n    - backend/app/services/x.py")
        assert WorkEngine.from_directory(tmp_path).fuer_datei(
            "backend/app/services/x.py") == []

    def test_kanten_vor_evidence(self, tmp_path: Path):
        """Kanten sind praeziser (Symbol-Ebene) und ausdruecklich vorwaerts."""
        _write(tmp_path, "ev.md",
               "key: ev\ntitle: Ev\nkind: step\nstatus: open\ntheme: engine\n"
               "opened: 2026-01-01\nevidence:\n  commits: []\n"
               "  files:\n    - backend/app/services/x.py")
        _write(tmp_path, "ka.md",
               "key: ka\ntitle: Ka\nkind: step\nstatus: open\ntheme: engine\n"
               "opened: 2026-01-01\nkanten:\n  gesucht:\n    - x\n"
               "  nutzt:\n    - backend/app/services/x.py::foo")
        t = WorkEngine.from_directory(tmp_path).fuer_datei("backend/app/services/x.py")
        assert [i.key for i in t] == ["ka", "ev"]

    def test_deckel_haelt(self, tmp_path: Path):
        """Eine Wand wird ueberblaettert, eine Handvoll gelesen."""
        for n in range(12):
            _write(tmp_path, f"r{n}.md",
                   f"key: r{n}\ntitle: R{n}\nkind: step\nstatus: open\n"
                   "theme: engine\nopened: 2026-01-01\nevidence:\n  commits: []\n"
                   "  files:\n    - backend/app/services/x.py")
        t = WorkEngine.from_directory(tmp_path).fuer_datei(
            "backend/app/services/x.py", limit=8)
        assert len(t) == 8

    def test_fremde_datei_liefert_nichts(self, tmp_path: Path):
        _write(tmp_path, "a.md",
               "key: a\ntitle: A\nkind: step\nstatus: open\ntheme: engine\n"
               "opened: 2026-01-01\nevidence:\n  commits: []\n"
               "  files:\n    - backend/app/services/x.py")
        assert WorkEngine.from_directory(tmp_path).fuer_datei("ganz/woanders.py") == []

    def test_fingerprint_aendert_sich_mit_der_arbeit(self, tmp_path: Path):
        """OHNE das waere die Zustellung ein stiller Veraltungs-Filter.

        Der Hook dedupliziert ueber den Fingerprint. Zaehlten die Work-Items
        nicht mit, wuerde ein NEUER offener Punkt nie zugestellt, solange sich
        die Regeln nicht aendern — die Dedup unterdrueckte genau die Auskunft,
        die neu ist.
        """
        from mcp_context_toolkit.engine import fingerprint_rules
        _write(tmp_path, "a.md",
               "key: a\ntitle: A\nkind: step\nstatus: open\ntheme: engine\n"
               "opened: 2026-01-01\nevidence:\n  commits: []\n"
               "  files:\n    - backend/app/services/x.py")
        e = WorkEngine.from_directory(tmp_path)
        arbeit = e.fuer_datei("backend/app/services/x.py")
        ohne = fingerprint_rules([], None, None)
        mit = fingerprint_rules([], None, arbeit)
        assert ohne != mit, "Work-Items schlagen nicht auf den Fingerprint durch"
        # Statuswechsel muss ebenfalls durchschlagen
        arbeit[0].status = "active"
        assert fingerprint_rules([], None, arbeit) != mit


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

    def test_exclude_kinds_drops_candidates_before_the_limit_cut(self, tmp_path: Path):
        """W8-Reviewer-Fund ERROR-2 (Etappe D): nach dem Changelog-Backfill
        (2377 `kind: changelog`-Karten) verdraengten Changelog-Treffer echte
        goal/step-Records vollstaendig aus den Top-8 (gemessen: 8/8 bei einer
        Query). Ein Post-Hoc-Filter auf die bereits limitierten Top-N haette
        nicht geholfen (waere teils leer geblieben) — `exclude_kinds` muss
        VOR dem `limit`-Schnitt greifen, deshalb hier mit `limit=1` gegen
        einen Bestand, in dem die Changelog-Karte den staerkeren Treffer hat."""
        _write(tmp_path, "changelog_hit.md",
               "key: changelog_hit\ntitle: Zolltarif Suche\nkind: changelog\n"
               "status: done\ntheme: engine\nopened: 2026-01-01\nclosed: 2026-01-01")
        _write(tmp_path, "goal_hit.md",
               "key: goal_hit\ntitle: Sonstiges\nkind: goal\nstatus: open\n"
               "theme: engine\nopened: 2026-01-01",
               body="Erwaehnt Zolltarif nur einmal beilaeufig im Fliesstext.")
        e = WorkEngine.from_directory(tmp_path)
        # Ohne Filter gewinnt der Titel-Treffer (Changelog) den einzigen Platz.
        assert [i.key for i in e.recall("zolltarif suche", limit=1)] == ["changelog_hit"]
        # Mit Filter faellt die Changelog-Karte VOR dem limit=1-Schnitt raus,
        # der goal-Record bleibt uebrig statt einer leeren Liste.
        gefiltert = e.recall("zolltarif suche", limit=1,
                             exclude_kinds=frozenset({"decision", "changelog"}))
        assert [i.key for i in gefiltert] == ["goal_hit"]
