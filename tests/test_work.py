from pathlib import Path

import pytest
import yaml

from mcp_context_toolkit.core import KnowledgeLoadError
from mcp_context_toolkit.work import (
    FrontmatterWriteError,
    WorkEngine,
    _has_frontmatter_comment,
    render_work_item_text,
    work_status_payload,
)


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
    """work-status integration (2026-08-19) — dieselbe Bauart wie der `kanten`-Zweig, eine
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
        # Das Migrationskennzeichen macht das Reuse-Gate robust gegen die
        # bisher unausgesprochene
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
        """a regression review: `body` gehoert zu `WorkItem.model_fields`,
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


class TestCategory:
    """`category` (bug/feature/debt/research) is orthogonal to `kind`
    (granularity) and `theme` (domain) — the axis a mixed backlog had no
    field for at all until now."""

    def test_absent_category_loads_as_none(self, tmp_path: Path):
        _write(tmp_path, "a.md",
               "key: a\ntitle: A\nkind: goal\nstatus: open\ntheme: engine\n"
               "opened: 2026-07-01")
        e = WorkEngine.from_directory(tmp_path)
        assert e.get("a").category is None

    def test_valid_category_loads(self, tmp_path: Path):
        _write(tmp_path, "a.md",
               "key: a\ntitle: A\nkind: step\nstatus: open\ntheme: engine\n"
               "category: bug\nopened: 2026-07-01")
        e = WorkEngine.from_directory(tmp_path)
        assert e.get("a").category == "bug"

    def test_unknown_category_is_rejected_not_silently_dropped(self, tmp_path: Path):
        # Same discipline as the theme-slug check above: a typo'd category
        # must fail loudly, not vanish or silently become None.
        _write(tmp_path, "a.md",
               "key: a\ntitle: A\nkind: goal\nstatus: open\ntheme: engine\n"
               "category: enhancement\nopened: 2026-07-01")
        with pytest.raises(KnowledgeLoadError):
            WorkEngine.from_directory(tmp_path, strict=True)

    def test_category_filter(self, tmp_path: Path):
        for key, category in [("a", "bug"), ("b", "feature"), ("c", "bug")]:
            _write(tmp_path, f"{key}.md",
                   f"key: {key}\ntitle: {key}\nkind: goal\nstatus: open\n"
                   f"theme: engine\ncategory: {category}\nopened: 2026-07-01")
        _write(tmp_path, "d.md",
               "key: d\ntitle: D\nkind: goal\nstatus: open\ntheme: engine\n"
               "opened: 2026-07-01")  # kein category — darf beim Filter nicht auftauchen
        e = WorkEngine.from_directory(tmp_path)
        assert sorted(i.key for i in e.list(category="bug")) == ["a", "c"]

    def test_category_filter_combines_with_theme(self, tmp_path: Path):
        _write(tmp_path, "a.md",
               "key: a\ntitle: A\nkind: goal\nstatus: open\ntheme: engine\n"
               "category: bug\nopened: 2026-07-01")
        _write(tmp_path, "b.md",
               "key: b\ntitle: B\nkind: goal\nstatus: open\ntheme: chat\n"
               "category: bug\nopened: 2026-07-01")
        e = WorkEngine.from_directory(tmp_path)
        assert [i.key for i in e.list(category="bug", theme="engine")] == ["a"]

    def test_category_survives_save_update_round_trip(self, tmp_path: Path):
        _write(tmp_path, "a.md",
               "key: a\ntitle: A\nkind: step\nstatus: open\ntheme: engine\n"
               "opened: 2026-01-01")
        e = WorkEngine.from_directory(tmp_path)
        assert e.get("a").category is None
        e.save_update("a", {"category": "debt"})
        reloaded = WorkEngine.from_directory(tmp_path)
        assert reloaded.get("a").category == "debt"


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

    def test_closeable_goal_catches_the_forgotten_goal_case(self, tmp_path: Path):
        # The plan's sharpest single finding: an audit already recommended
        # closing this exact item and nothing acted on it. This is the
        # mechanical detector for that failure mode.
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
        """a regression review (editing integration): eine migrierte historische
        Karte hat kein Commit, auf das evidence zeigen koennte — der Backfill
        (2377 Changelog-Karten) begrub sonst die 29 echten Faelle unter fast
        30-fachem Rauschen. Dieselbe Ausnahme wie bereits bei
        reuse_ungeprueft (work-status integration)."""
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
        # Owner-decision 2026-08-16 (work-link-check):
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
        # incidental "[[DRAFT_NOTE]]"-style placeholder mention that reads
        # like a cross-record link but is prose about code, not a real one.
        _write(tmp_path, "a.md",
               f"key: a\ntitle: A\nkind: {kind}\nstatus: done\ntheme: engine\n"
               "opened: 2026-01-01\nclosed: 2026-01-01",
               body="Placeholder [[DRAFT_NOTE]] wird eingesetzt.")
        e = WorkEngine.from_directory(tmp_path)
        assert e.lint()["body_link_broken"] == {}

    def test_kanten_gebrochen_is_none_without_symbol_check(self, tmp_path: Path):
        # Not run vs. run-and-clean must stay distinguishable: the default,
        # cheap lint() call
        # never triggers the injected check.
        _write(tmp_path, "a.md",
               "key: a\ntitle: A\nkind: step\nstatus: open\ntheme: engine\n"
               "opened: 2026-07-01\nkanten:\n  gesucht:\n    - x\n"
               "  nutzt:\n    - services/foo.py::bar")
        e = WorkEngine.from_directory(tmp_path)
        assert e.lint()["broken_reuse_refs"] is None

    def test_kanten_gebrochen_reports_only_unhealthy_states(self, tmp_path: Path):
        _write(tmp_path, "a.md",
               "key: a\ntitle: A\nkind: step\nstatus: open\ntheme: engine\n"
               "opened: 2026-07-01\nkanten:\n  gesucht:\n    - x\n"
               "  nutzt:\n    - ok.py::vorhanden\n  erweitert:\n    - weg.py::verschwunden\n"
               "  baut:\n    - neu.py::kommt_noch\n  modul:\n    - ein_modul")

        def check(typ: str, ziel: str) -> tuple:
            werte = {
                ("uses", "ok.py::vorhanden"): ("ok", None),
                ("extends", "weg.py::verschwunden"): ("gebrochen", "weg"),
                ("creates", "neu.py::kommt_noch"): ("geplant", None),
                ("modules", "ein_modul"): ("ok", None),
            }
            return werte[(typ, ziel)]

        e = WorkEngine.from_directory(tmp_path)
        kg = e.lint(check)["broken_reuse_refs"]
        # ok + geplant sind gesund und fehlen; nur gebrochen erscheint.
        assert kg == {"a": [{"kind": "extends", "target": "weg.py::verschwunden",
                              "state": "gebrochen", "hint": "weg"}]}

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
        assert e.lint(check)["broken_reuse_refs"] == {}
        assert gesehen == []

    def test_kanten_gebrochen_empty_dict_when_run_and_clean(self, tmp_path: Path):
        _write(tmp_path, "a.md",
               "key: a\ntitle: A\nkind: step\nstatus: open\ntheme: engine\n"
               "opened: 2026-07-01\nkanten:\n  gesucht:\n    - x\n"
               "  nutzt:\n    - services/foo.py::bar")
        e = WorkEngine.from_directory(tmp_path)
        assert e.lint(lambda _typ, _ziel: ("ok", None))["broken_reuse_refs"] == {}


class TestWorkStatusPayload:
    """`work_status_payload` was pulled out of `server/tools_work.py`'s
    `work_status()` closure (Context Studio work-status integration) so the
    backend KPI endpoint can share the exact recommendation logic instead
    of re-deriving it. Hand-built `lint()`-shaped dicts, not a real
    WorkEngine — this tests the pure counting/recommendation function in
    isolation, `TestLint` above already covers `lint()` itself."""

    def _lint(self, **overrides):
        base = {
            "total": 3, "closeable_goals": [], "dangling_refs": [],
            "missing_evidence": [], "unclosed_pairs": [],
            "plan_path_broken": {}, "reuse_not_checked": [],
            "load_errors": [],
        }
        base.update(overrides)
        return base

    def test_load_errors_take_top_billing(self):
        # dropped is true AND closeable is true — dropped must still win,
        # per the docstring's "top billing" claim, not just be counted.
        lint = self._lint(load_errors=["bad.md"], closeable_goals=["g"])
        payload = work_status_payload(lint)
        assert payload["recommendation"].startswith("🚨")

    def test_closeable_or_dangling_or_plan_broken_means_aufraeumen(self):
        assert work_status_payload(self._lint(closeable_goals=["g"]))["recommendation"].startswith("🧹")
        assert work_status_payload(self._lint(dangling_refs=[("a", "b")]))["recommendation"].startswith("🧹")
        assert work_status_payload(self._lint(plan_path_broken={"a": "x"}))["recommendation"].startswith("🧹")

    def test_reuse_not_checked_alone_is_its_own_recommendation(self):
        payload = work_status_payload(self._lint(reuse_not_checked=["a", "b"]))
        assert payload["recommendation"].startswith("🔎")
        assert "2" in payload["recommendation"]

    def test_missing_evidence_alone_is_kleine_reste_not_aufraeumen(self):
        # missing_evidence/unclosed_pairs are counted in total_issues but
        # NOT in the "Aufraeumen faellig" branch's own condition — this is
        # the one case that actually distinguishes the two branches.
        payload = work_status_payload(self._lint(missing_evidence=["a"]))
        assert payload["recommendation"] == "💡 kleine Reste"

    def test_all_clean_is_kein_dringender_bedarf(self):
        assert work_status_payload(self._lint())["recommendation"] == "✅ kein dringender Bedarf"

    def test_counts_pass_through_unmodified(self):
        lint = self._lint(
            closeable_goals=["a", "b"], dangling_refs=[("x", "y")],
            missing_evidence=["c"], unclosed_pairs=["d"],
            plan_path_broken={"e": "f"}, reuse_not_checked=["g"],
            load_errors=["h"],
        )
        payload = work_status_payload(lint)
        assert payload["total_items"] == 3
        assert payload["closeable_goals"] == 2
        assert payload["dangling_refs"] == 1
        assert payload["missing_evidence"] == 1
        assert payload["unclosed_pairs"] == 1
        assert payload["plan_path_broken"] == 1
        assert payload["reuse_not_checked"] == 1
        assert payload["load_errors"] == 1


class TestKantenUndPlanPath:
    """Die zwei Pruefungen, die 2026-08-04 dazukamen — beide aus einem echten Fund.

    `plan_path` ist die EINZIGE Verbindung zwischen Plan-Dateien und Work-Store,
    und nichts prueft sie: gemessen zeigten 8 Records auf `plans/todo/...`-Dateien,
    die eine Migrationshilfe nach `plans/begonnen/` verschoben hatte — waehrend
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

    def test_english_fields_reading(self, tmp_path: Path):
        _write(tmp_path, "a.md",
               "key: a\ntitle: A\nkind: step\nstatus: active\ntheme: engine\n"
               "opened: 2026-01-01\nrequires_owner: true\nunblocked_by: b\nmigrated: true\n"
               "reuse:\n  searched_for:\n    - 'retry with backoff'\n"
               "  uses:\n    - services/http_client.py::retry")
        item = WorkEngine.from_directory(tmp_path).get("a")
        assert item is not None
        assert item.requires_owner is True
        assert item.unblocked_by == "b"
        assert item.migrated is True
        assert item.reuse is not None
        assert item.reuse.searched_for == ["retry with backoff"]
        assert item.reuse.uses == ["services/http_client.py::retry"]
        # Alias properties work
        assert item.braucht_owner is True
        assert item.kanten.nutzt == ["services/http_client.py::retry"]

    def test_gesucht_ohne_treffer_zaehlt_als_geprueft(self, tmp_path: Path):
        """Der Kern: leere Listen sind ein ERGEBNIS, kein fehlender Eintrag."""
        _write(tmp_path, "a.md",
               "key: a\ntitle: A\nkind: step\nstatus: active\ntheme: engine\n"
               "opened: 2026-01-01\nkanten:\n  gesucht:\n    - 'nichts dergleichen'")
        e = WorkEngine.from_directory(tmp_path)
        assert e.get("a").kanten is not None
        assert e.lint()["reuse_not_checked"] == []

    def test_leerer_kanten_block_gilt_NICHT_als_geprueft(self, tmp_path: Path):
        """Sonst genuegte `kanten: {}` um das Gate zu umgehen."""
        _write(tmp_path, "a.md",
               "key: a\ntitle: A\nkind: step\nstatus: active\ntheme: engine\n"
               "opened: 2026-01-01\nkanten:\n  gesucht: []\n  nutzt: []")
        e = WorkEngine.from_directory(tmp_path)
        assert e.get("a").kanten is None
        assert e.lint()["reuse_not_checked"] == ["a"]

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
        assert WorkEngine.from_directory(tmp_path).lint()["reuse_not_checked"] == ["aktiv"]

    @pytest.mark.parametrize("kind", ["decision", "changelog"])
    def test_reuse_ungeprueft_nimmt_decision_und_changelog_aus(self, tmp_path: Path, kind: str):
        """a regression review: `kanten` ist ein VORWAERTS-Feld — eine
        migrierte historische Tatsache hat vor ihrem eigenen Entstehen nichts
        gesucht. Ohne diese Ausnahme widerspraeche der Engine-Lint der in
        der Migrations-Doku dokumentierten Gate-Ausnahme."""
        _write(tmp_path, "a.md",
               f"key: a\ntitle: A\nkind: {kind}\nstatus: active\ntheme: engine\n"
               "opened: 2026-01-01")
        assert WorkEngine.from_directory(tmp_path).lint()["reuse_not_checked"] == []

    def test_entblockt_durch_auf_unbekannten_key_wird_gemeldet(self, tmp_path: Path):
        """a regression review: ohne diese Pruefung hebt jeder truthy
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
               "  nutzt:\n    - src/services/x.py::foo")
        t = WorkEngine.from_directory(tmp_path).fuer_datei("src/services/x.py")
        assert [i.key for i in t] == ["a"]

    def test_evidence_files_treffer(self, tmp_path: Path):
        _write(tmp_path, "a.md",
               "key: a\ntitle: A\nkind: step\nstatus: open\ntheme: engine\n"
               "opened: 2026-01-01\nevidence:\n  commits: []\n"
               "  files:\n    - src/services/x.py")
        t = WorkEngine.from_directory(tmp_path).fuer_datei("src/services/x.py")
        assert [i.key for i in t] == ["a"]

    def test_geschlossene_records_bleiben_draussen(self, tmp_path: Path):
        """Ein erledigter Punkt ist keine Arbeit mehr."""
        _write(tmp_path, "a.md",
               "key: a\ntitle: A\nkind: step\nstatus: done\nclosed: 2026-01-02\n"
               "theme: engine\nopened: 2026-01-01\nevidence:\n  commits: [abc]\n"
               "  files:\n    - src/services/x.py")
        assert WorkEngine.from_directory(tmp_path).fuer_datei(
            "src/services/x.py") == []

    def test_kanten_vor_evidence(self, tmp_path: Path):
        """Kanten sind praeziser (Symbol-Ebene) und ausdruecklich vorwaerts."""
        _write(tmp_path, "ev.md",
               "key: ev\ntitle: Ev\nkind: step\nstatus: open\ntheme: engine\n"
               "opened: 2026-01-01\nevidence:\n  commits: []\n"
               "  files:\n    - src/services/x.py")
        _write(tmp_path, "ka.md",
               "key: ka\ntitle: Ka\nkind: step\nstatus: open\ntheme: engine\n"
               "opened: 2026-01-01\nkanten:\n  gesucht:\n    - x\n"
               "  nutzt:\n    - src/services/x.py::foo")
        t = WorkEngine.from_directory(tmp_path).fuer_datei("src/services/x.py")
        assert [i.key for i in t] == ["ka", "ev"]

    def test_negative_limit_clamps_instead_of_reversing(self, tmp_path: Path):
        # Same class of gap as recall()'s: `treffer[:limit]` treated a
        # negative limit as a valid, reversed Python slice.
        for i in range(5):
            _write(tmp_path, f"a{i}.md",
                   f"key: a{i}\ntitle: A{i}\nkind: step\nstatus: open\n"
                   f"theme: engine\nopened: 2026-01-01\nevidence:\n  commits: []\n"
                   f"  files:\n    - src/services/x.py")
        t = WorkEngine.from_directory(tmp_path).for_file(
            "src/services/x.py", limit=-1)
        assert len(t) == 1

    def test_deckel_haelt(self, tmp_path: Path):
        """Eine Wand wird ueberblaettert, eine Handvoll gelesen."""
        for n in range(12):
            _write(tmp_path, f"r{n}.md",
                   f"key: r{n}\ntitle: R{n}\nkind: step\nstatus: open\n"
                   "theme: engine\nopened: 2026-01-01\nevidence:\n  commits: []\n"
                   "  files:\n    - src/services/x.py")
        t = WorkEngine.from_directory(tmp_path).fuer_datei(
            "src/services/x.py", limit=8)
        assert len(t) == 8

    def test_fremde_datei_liefert_nichts(self, tmp_path: Path):
        _write(tmp_path, "a.md",
               "key: a\ntitle: A\nkind: step\nstatus: open\ntheme: engine\n"
               "opened: 2026-01-01\nevidence:\n  commits: []\n"
               "  files:\n    - src/services/x.py")
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
               "  files:\n    - src/services/x.py")
        e = WorkEngine.from_directory(tmp_path)
        arbeit = e.fuer_datei("src/services/x.py")
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

    def test_recall_clamps_negative_limit(self, tmp_path: Path):
        # A bare `scored[:limit]` treats a negative limit as a valid,
        # reversed Python slice — same gap docs.py's recall() had before
        # it was clamped (see core.clamp_limit). Unclamped, `scored[:-1]`
        # on 10 candidates drops only the last one (9 survive) regardless
        # of individual scores — a loose `1 <= n <= 10` bound would pass
        # either way, so this pins the exact count.
        for i in range(10):
            _write(tmp_path, f"i{i}.md",
                   f"key: i{i}\ntitle: Gemeinsamer Suchbegriff {i}\nkind: goal\n"
                   f"status: open\ntheme: engine\nopened: 2026-07-01")
        e = WorkEngine.from_directory(tmp_path)
        hits = e.recall("Gemeinsamer Suchbegriff", limit=-1)
        assert len(hits) == 1

    def test_recall_clamps_excessive_limit(self, tmp_path: Path):
        # A single candidate proves "does not raise" but not "caps at 50" —
        # a corpus over the ceiling is needed to actually pin the clamp.
        for i in range(60):
            _write(tmp_path, f"i{i}.md",
                   f"key: i{i}\ntitle: Suchbegriff {i}\nkind: goal\n"
                   f"status: open\ntheme: engine\nopened: 2026-07-01")
        e = WorkEngine.from_directory(tmp_path)
        assert len(e.recall("Suchbegriff", limit=10_000)) == 50

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
        """a regression review (editing integration): nach dem Changelog-Backfill
        (2377 `kind: changelog`-Karten) verdraengten Changelog-Treffer echte
        goal/step-Records vollstaendig aus den Top-8 (gemessen: 8/8 bei einer
        Query). Ein Post-Hoc-Filter auf die bereits limitierten Top-N haette
        nicht geholfen (waere teils leer geblieben) — `exclude_kinds` muss
        VOR dem `limit`-Schnitt greifen, deshalb hier mit `limit=1` gegen
        einen Bestand, in dem die Changelog-Karte den staerkeren Treffer hat."""
        _write(tmp_path, "changelog_hit.md",
               "key: changelog_hit\ntitle: Kumquat Suche\nkind: changelog\n"
               "status: done\ntheme: engine\nopened: 2026-01-01\nclosed: 2026-01-01")
        _write(tmp_path, "goal_hit.md",
               "key: goal_hit\ntitle: Sonstiges\nkind: goal\nstatus: open\n"
               "theme: engine\nopened: 2026-01-01",
               body="Erwaehnt Kumquat nur einmal beilaeufig im Fliesstext.")
        e = WorkEngine.from_directory(tmp_path)
        # Ohne Filter gewinnt der Titel-Treffer (Changelog) den einzigen Platz.
        assert [i.key for i in e.recall("kumquat suche", limit=1)] == ["changelog_hit"]
        # Mit Filter faellt die Changelog-Karte VOR dem limit=1-Schnitt raus,
        # der goal-Record bleibt uebrig statt einer leeren Liste.
        gefiltert = e.recall("kumquat suche", limit=1,
                             exclude_kinds=frozenset({"decision", "changelog"}))
        assert [i.key for i in gefiltert] == ["goal_hit"]


class TestWriteWorkItem:
    """Context Studio editing integration — lifts the read-only boundary
    stated in this module's own docstring since v1.4.312, for exactly one
    new consumer (the Context Studio edit form), Owner-decision 2026-08-22.
    Every test here checks the boundary that survives the lift: validated,
    atomic, refuses rather than silently drops a comment."""

    def test_apply_update_does_not_touch_the_file(self, tmp_path: Path):
        _write(tmp_path, "a.md",
               "key: a\ntitle: A\nkind: step\nstatus: open\ntheme: engine\n"
               "opened: 2026-01-01")
        e = WorkEngine.from_directory(tmp_path)
        path = tmp_path / "a.md"
        before = path.read_bytes()
        old_text, new_text, new_item = e.apply_update("a", {"status": "active"})
        assert path.read_bytes() == before  # kein Byte auf Platte veraendert
        assert old_text == before.decode("utf-8")
        assert new_item.status == "active"
        assert "status: active" in new_text
        assert e.get("a").status == "open"  # in-memory ebenfalls unveraendert

    def test_save_update_writes_and_updates_in_memory_state(self, tmp_path: Path):
        _write(tmp_path, "a.md",
               "key: a\ntitle: A\nkind: step\nstatus: open\ntheme: engine\n"
               "opened: 2026-01-01")
        e = WorkEngine.from_directory(tmp_path)
        saved = e.save_update("a", {"status": "done", "closed": "2026-08-22"})
        assert saved.status == "done"
        assert e.get("a").status == "done"  # kein Reload noetig
        # Re-Load von Platte bestaetigt: geschrieben wurde etwas, das der
        # Lesepfad selbst wieder korrekt interpretiert — nicht nur "irgendwas".
        reloaded = WorkEngine.from_directory(tmp_path)
        assert reloaded.get("a").status == "done"
        assert str(reloaded.get("a").closed) == "2026-08-22"

    def test_save_update_leaves_no_tmp_file_behind(self, tmp_path: Path):
        _write(tmp_path, "a.md",
               "key: a\ntitle: A\nkind: step\nstatus: open\ntheme: engine\n"
               "opened: 2026-01-01")
        e = WorkEngine.from_directory(tmp_path)
        e.save_update("a", {"status": "active"})
        assert sorted(p.name for p in tmp_path.iterdir()) == ["a.md"]

    def test_a_crash_between_write_and_rename_never_touches_the_real_file(self, tmp_path: Path, monkeypatch):
        # a regression review: an earlier "atomic write" mutation
        # test only checked whether content ARRIVED in the file, not
        # whether the rename step is what makes that safe — replacing the
        # temp-file+rename with a bare `path.write_text(new_text)` broke
        # ZERO of the then-existing tests, because both approaches produce
        # an identical end state when nothing goes wrong. This test
        # targets the property atomic writes actually buy: if the process
        # dies BETWEEN writing the temp file and renaming it into place,
        # the real file must stay exactly as it was — never partially
        # written, never silently replaced by an incomplete one.
        _write(tmp_path, "a.md",
               "key: a\ntitle: A\nkind: step\nstatus: open\ntheme: engine\n"
               "opened: 2026-01-01")
        original = (tmp_path / "a.md").read_bytes()
        e = WorkEngine.from_directory(tmp_path)

        real_replace = Path.replace

        def crashing_replace(self_path, target):
            if str(self_path).endswith("a.md.tmp"):
                raise OSError("simulated crash between write and rename")
            return real_replace(self_path, target)

        monkeypatch.setattr(Path, "replace", crashing_replace)

        with pytest.raises(OSError):
            e.save_update("a", {"status": "active"})

        assert (tmp_path / "a.md").read_bytes() == original

    def test_key_is_immutable(self, tmp_path: Path):
        _write(tmp_path, "a.md",
               "key: a\ntitle: A\nkind: step\nstatus: open\ntheme: engine\n"
               "opened: 2026-01-01")
        e = WorkEngine.from_directory(tmp_path)
        with pytest.raises(ValueError):
            e.apply_update("a", {"key": "b"})

    def test_unknown_key_raises_keyerror(self, tmp_path: Path):
        e = WorkEngine.from_directory(tmp_path)
        with pytest.raises(KeyError):
            e.apply_update("nope", {"status": "active"})

    def test_source_path_cannot_be_overridden_via_updates(self, tmp_path: Path):
        # Sicherheitsrelevant: WorkItem ist plain BaseModel (extra="ignore"),
        # nicht _StrengesModell (extra="forbid") — ohne diese explizite
        # Sperre wuerde ein source_path in updates save_update dazu bringen,
        # an einen beliebigen Pfad zu schreiben (Path-Traversal-Primitive).
        _write(tmp_path, "a.md",
               "key: a\ntitle: A\nkind: step\nstatus: open\ntheme: engine\n"
               "opened: 2026-01-01")
        outside = tmp_path.parent / "ausserhalb.md"
        e = WorkEngine.from_directory(tmp_path)
        with pytest.raises(ValueError):
            e.apply_update("a", {"source_path": str(outside)})
        assert not outside.exists()

    def test_unknown_field_in_updates_raises_not_silently_ignored(self, tmp_path: Path):
        # Pydantics extra="ignore" wuerde einen Tippfehler sonst spurlos
        # verschlucken — dieselbe Disziplin wie _parse_work_item's
        # unbekannt_top-Pruefung auf dem Lesepfad.
        _write(tmp_path, "a.md",
               "key: a\ntitle: A\nkind: step\nstatus: open\ntheme: engine\n"
               "opened: 2026-01-01")
        e = WorkEngine.from_directory(tmp_path)
        with pytest.raises(ValueError):
            e.apply_update("a", {"staus": "active"})  # Tippfehler absichtlich

    def test_invalid_field_value_raises_frontmatter_write_error(self, tmp_path: Path):
        _write(tmp_path, "a.md",
               "key: a\ntitle: A\nkind: step\nstatus: open\ntheme: engine\n"
               "opened: 2026-01-01")
        e = WorkEngine.from_directory(tmp_path)
        with pytest.raises(FrontmatterWriteError):
            e.apply_update("a", {"status": "not_a_real_status"})
        # Datei bleibt unveraendert, kein halb-geschriebener Zustand.
        assert "status: open" in (tmp_path / "a.md").read_text(encoding="utf-8")

    def test_frontmatter_comment_refuses_the_write(self, tmp_path: Path):
        # Genau der 12-von-5159-Fall aus dem echten Bestand: ein Kommentar
        # im evidence.commits-Block, der ein leeres `commits: []` erklaert.
        text = (
            "---\nkey: a\ntitle: A\nkind: step\nstatus: open\ntheme: engine\n"
            "opened: 2026-01-01\nevidence:\n  commits: []  # noch nicht committet\n"
            "---\n\nKoerper.\n"
        )
        (tmp_path / "a.md").write_text(text, encoding="utf-8")
        e = WorkEngine.from_directory(tmp_path)
        with pytest.raises(FrontmatterWriteError):
            e.apply_update("a", {"status": "active"})
        assert (tmp_path / "a.md").read_text(encoding="utf-8") == text

    def test_comment_inside_a_quoted_string_does_not_false_positive(self, tmp_path: Path):
        # Ein '#' innerhalb eines gequoteten Strings ist KEIN YAML-Kommentar
        # — die Erkennung darf hier nicht faelschlich verweigern.
        text = (
            "---\nkey: a\ntitle: 'Ticket #123'\nkind: step\nstatus: open\n"
            "theme: engine\nopened: 2026-01-01\n---\n\nKoerper.\n"
        )
        (tmp_path / "a.md").write_text(text, encoding="utf-8")
        e = WorkEngine.from_directory(tmp_path)
        _old_text, _new_text, new_item = e.apply_update("a", {"status": "active"})
        assert new_item.status == "active"

    def test_apostrophe_mid_word_does_not_hide_a_real_comment_after_it(self):
        # a regression review: eine erste Fassung tolerierte einen
        # Apostroph mitten im Wort ("Owner's decision") als Quote-Start —
        # ein echter Kommentar SPAETER auf derselben Zeile las sich dann
        # faelschlich als "innerhalb eines Strings" und wurde uebersehen.
        assert _has_frontmatter_comment("title: Owner's decision  # echter Kommentar") is True
        assert _has_frontmatter_comment("title: Don't stop  # echter Kommentar") is True

    def test_quote_state_persists_across_lines_no_false_positive_on_multiline_title(self):
        # Gegenprobe zu WARNING-4: ein GENUINE mehrzeiliger gequoteter
        # Titel darf NICHT als Kommentar-Fund gelten, obwohl die erste
        # Zeile ein ungeschlossenes Anfuehrungszeichen traegt — der Quote
        # bleibt ueber den Zeilenumbruch hinweg offen.
        multiline = "title: 'Ein langer\n  Titel der ueber\n  mehrere Zeilen geht'\ntheme: infra"
        assert _has_frontmatter_comment(multiline) is False

    def test_hash_embedded_in_a_word_is_not_a_comment_marker(self):
        # Gegenprobe zu WARNING-4/dem "Hinweis": ein '#' MITTEN in einem
        # Wort ("Backend#1") ist laut YAML-Spezifikation kein Kommentar —
        # ein Kommentar beginnt nur am Zeilenanfang oder nach Whitespace.
        assert _has_frontmatter_comment("title: Fork-Stability Backend#1 — aclose-Race") is False
        assert _has_frontmatter_comment("title: Lock-#1-Liefergrenze fuer autonome Tasks") is False
        # Aber ein '#' NACH Whitespace bleibt ein echter Kommentar.
        assert _has_frontmatter_comment("title: Backend#1  # das hier ist ein Kommentar") is True

    def test_doubled_single_quote_escape_does_not_break_quote_tracking(self):
        # a regression review (Anker-Checker-Sprint, 2026-09-06): `''`
        # innerhalb eines einfach-quotierten YAML-Skalars ist das Escape
        # fuer EIN woertliches Anfuehrungszeichen, keine Schluss-Quote. Eine
        # fruehere Fassung schloss die Quote beim ERSTEN `'` des Paars und
        # konnte sie beim zweiten nicht wieder oeffnen (`at_value_start` war
        # dann `False`) — der Rest des mehrzeiligen Skalars galt faelschlich
        # als "ausserhalb jeder Quote", und ein spaeteres '#' in normaler
        # Prosa ("Transition #4") wurde faelschlich als Kommentar gelesen.
        # Regression case: a multi-line quoted scalar with an escaped quote.
        text = (
            "reuse:\n  searched_for:\n"
            "  - 'review-guide.md Phase 2 ''Status-Read'' -- liest nur ''die "
            "ersten 30 Zeilen''\n"
            "    des Headers, nie den Body; Transition #4 (begonnen/ -> fertig/) "
            "verschiebt allein\n"
            "    auf Basis dieses Header-Markers'\n"
        )
        assert _has_frontmatter_comment(text) is False
        # Gegenprobe: ein Escape-Paar am Stringende darf nicht crashen
        # (Lookahead ueber das Stringende hinaus).
        assert _has_frontmatter_comment("key: 'it''s'") is False
        # Gegenprobe: ein echter Kommentar NACH einem escapten Paar auf
        # derselben Zeile bleibt erkennbar.
        assert _has_frontmatter_comment("key: 'it''s over'  # echter Kommentar") is True

    def test_full_field_editing_round_trips_through_a_reload(self, tmp_path: Path):
        _write(tmp_path, "a.md",
               "key: a\ntitle: Alt\nkind: step\nstatus: open\ntheme: engine\n"
               "opened: 2026-01-01\npart_of: parent_goal\nblocked_by: []")
        _write(tmp_path, "parent_goal.md",
               "key: parent_goal\ntitle: Parent\nkind: goal\nstatus: open\n"
               "theme: engine\nopened: 2026-01-01")
        e = WorkEngine.from_directory(tmp_path)
        saved = e.save_update("a", {
            "title": "Neu betitelt äöü",
            "theme": "chat",
            "evidence": {"commits": ["abc123"], "files": ["x.py"]},
            "reuse": {"searched_for": ["foo"], "found": [], "conclusion": "nichts passendes"},
        })
        assert saved.title == "Neu betitelt äöü"
        assert saved.theme == "chat"
        assert saved.evidence.commits == ["abc123"]
        assert saved.reuse.searched_for == ["foo"]
        reloaded = WorkEngine.from_directory(tmp_path)
        r = reloaded.get("a")
        assert r.title == "Neu betitelt äöü"
        assert r.theme == "chat"
        assert r.evidence.files == ["x.py"]
        assert r.reuse.conclusion == "nichts passendes"
        assert r.part_of == "parent_goal"  # unveraenderte Felder bleiben erhalten

    def test_render_work_item_text_produces_valid_reparseable_yaml(self, tmp_path: Path):
        _write(tmp_path, "a.md",
               "key: a\ntitle: A\nkind: step\nstatus: open\ntheme: engine\n"
               "opened: 2026-01-01", body="Ein Fliesstext.\n\nZweiter Absatz.")
        e = WorkEngine.from_directory(tmp_path)
        item = e.get("a")
        text = render_work_item_text(item)
        assert text.startswith("---\n")
        assert "Ein Fliesstext." in text
        assert "Zweiter Absatz." in text
        # Muss selbst wieder gueltiges YAML-Frontmatter sein.
        end = text.index("\n---", 4)
        parsed = yaml.safe_load(text[4:end])
        assert parsed["key"] == "a"
        assert parsed["title"] == "A"
