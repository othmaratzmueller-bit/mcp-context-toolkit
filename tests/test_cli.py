import json
from pathlib import Path

import pytest

from mcp_context_toolkit.cli import (
    _cmd_doctor,
    _cmd_export_studio,
    _cmd_migrate_decisions,
    _cmd_validate_all,
    _docs_payload,
    _work_payload,
    main,
)
from mcp_context_toolkit.work import WorkEngine


class TestMigrateDecisions:
    """W8-Reviewer-Fund ERROR-1 (mct-Reifeplan Etappe 3): `_cmd_migrate_decisions`
    existed since rc9 but was never reachable — no argparse flag called it, no
    test exercised it, and the CHANGELOG documented a CLI command that did not
    exist. Both layers (the function itself, and the actual `main()` argv
    wiring) get their own test here so a future refactor of either can't
    silently disconnect them again."""

    def _write_legacy_decision(self, d: Path, key: str) -> None:
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{key}.yaml").write_text(
            f"title: Decision {key}\ndate: 2026-01-01\nreason: because.\n",
            encoding="utf-8",
        )

    def test_dry_run_previews_without_writing(self, tmp_path: Path, capsys):
        src = tmp_path / "decisions"
        dst = tmp_path / "work" / "decisions"
        self._write_legacy_decision(src, "d1")

        rc = _cmd_migrate_decisions(src, dst, dry_run=True)
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data == {"ok": True, "migrated": 1, "dry_run": True,
                        "targets": [str(dst / "d1.md")],
                        "overwritten": [], "conflicts": {},
                        "dropped_fields": {}, "unmapped_status": {}}
        assert not dst.exists()

    def test_real_run_writes_a_decision_card(self, tmp_path: Path, capsys):
        src = tmp_path / "decisions"
        dst = tmp_path / "work" / "decisions"
        self._write_legacy_decision(src, "d1")

        rc = _cmd_migrate_decisions(src, dst, dry_run=False)
        assert rc == 0
        card = (dst / "d1.md").read_text(encoding="utf-8")
        assert "kind: decision" in card
        assert "key: d1" in card

    def test_missing_from_dir_is_an_error(self, tmp_path: Path, capsys):
        rc = _cmd_migrate_decisions(tmp_path / "nope", tmp_path / "out", dry_run=True)
        assert rc == 1
        data = json.loads(capsys.readouterr().out)
        assert data["ok"] is False

    def test_main_wires_migrate_decisions_flag_end_to_end(self, tmp_path, monkeypatch, capsys):
        """The gap ERROR-1 actually found: is `--migrate-decisions` reachable
        via `main()`'s real argv parsing, not just via the underlying
        function called directly?"""
        src = tmp_path / "decisions"
        dst = tmp_path / "work" / "decisions"
        self._write_legacy_decision(src, "d1")

        monkeypatch.setattr("sys.argv", [
            "context-toolkit-query", "--migrate-decisions",
            "--from", str(src), "--to", str(dst), "--dry-run",
        ])
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 0
        data = json.loads(capsys.readouterr().out)
        assert data["ok"] is True
        assert data["dry_run"] is True

    def test_main_requires_both_from_and_to(self, monkeypatch, capsys):
        monkeypatch.setattr("sys.argv", ["context-toolkit-query", "--migrate-decisions"])
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 2

    def test_uses_the_real_key_field_not_the_date_prefixed_filename(self, tmp_path: Path, capsys):
        # A real bug this etappe found: legacy decision files are commonly
        # named <date>_<slug>.yaml with the real slug in `key:` — using the
        # filename stem as the Work Card key carried the leading digits into
        # a key WorkItem's `^[a-z][a-z0-9_]*$` pattern rejects outright, so
        # the migrated card would silently fail to load.
        src = tmp_path / "decisions"
        dst = tmp_path / "work" / "decisions"
        src.mkdir(parents=True)
        (src / "2026-01-01_layered_config.yaml").write_text(
            "key: layered_config\ntitle: Layered config\ndate: 2026-01-01\n"
            "reason: because.\n",
            encoding="utf-8",
        )
        rc = _cmd_migrate_decisions(src, dst, dry_run=False)
        assert rc == 0
        assert (dst / "layered_config.md").is_file()
        assert not (dst / "2026-01-01_layered_config.md").exists()
        card = (dst / "layered_config.md").read_text(encoding="utf-8")
        assert "key: layered_config" in card

    def test_key_fallback_sanitizes_a_digit_prefixed_filename(self, tmp_path: Path, capsys):
        # No `key:` field at all — the fallback must still produce a valid
        # WorkItem key, not carry the leading digits through.
        src = tmp_path / "decisions"
        dst = tmp_path / "work" / "decisions"
        src.mkdir(parents=True)
        (src / "2026-01-01_no_key_field.yaml").write_text(
            "title: No key field\nreason: because.\n", encoding="utf-8",
        )
        rc = _cmd_migrate_decisions(src, dst, dry_run=False)
        assert rc == 0
        targets = list(dst.glob("*.md"))
        assert len(targets) == 1
        assert targets[0].stem[0].isalpha()

    def test_decision_status_maps_to_work_status(self, tmp_path: Path, capsys):
        # A second real bug: status was previously hard-coded to `done` for
        # every migrated card regardless of the source `status:` field — a
        # rejected/deprecated decision would misreport as completed work.
        src = tmp_path / "decisions"
        dst = tmp_path / "work" / "decisions"
        src.mkdir(parents=True)
        (src / "d1.yaml").write_text(
            "title: D1\ndate: 2026-01-01\nstatus: rejected\nreason: because.\n",
            encoding="utf-8",
        )
        rc = _cmd_migrate_decisions(src, dst, dry_run=False)
        assert rc == 0
        assert "status: dropped" in (dst / "d1.md").read_text(encoding="utf-8")

    def test_theme_is_configurable_not_hardcoded_to_engine(self, tmp_path: Path, capsys):
        src = tmp_path / "decisions"
        dst = tmp_path / "work" / "decisions"
        self._write_legacy_decision(src, "d1")
        rc = _cmd_migrate_decisions(src, dst, dry_run=False, theme="billing")
        assert rc == 0
        assert "theme: billing" in (dst / "d1.md").read_text(encoding="utf-8")

    def test_dropped_fields_are_reported_not_silently_lost(self, tmp_path: Path, capsys):
        src = tmp_path / "decisions"
        dst = tmp_path / "work" / "decisions"
        src.mkdir(parents=True)
        (src / "d1.yaml").write_text(
            "key: d1\ntitle: D1\ndate: 2026-01-01\nreason: because.\n"
            "applies_to:\n  modules: [billing]\n  files: []\n"
            "owner: someone\n",
            encoding="utf-8",
        )
        rc = _cmd_migrate_decisions(src, dst, dry_run=True)
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert "owner" in data["dropped_fields"]["d1"]
        assert "applies_to.modules" in data["dropped_fields"]["d1"]

    def test_repeated_run_reports_overwritten_targets(self, tmp_path: Path, capsys):
        src = tmp_path / "decisions"
        dst = tmp_path / "work" / "decisions"
        self._write_legacy_decision(src, "d1")

        rc1 = _cmd_migrate_decisions(src, dst, dry_run=False)
        assert rc1 == 0
        capsys.readouterr()  # discard first run's output

        rc2 = _cmd_migrate_decisions(src, dst, dry_run=False)
        assert rc2 == 0
        data = json.loads(capsys.readouterr().out)
        assert data["overwritten"] == [str(dst / "d1.md")]

    def test_adr_style_stem_with_no_key_field_sanitizes_fully(self, tmp_path: Path, capsys):
        # The case that broke the first fix: sanitizing only the LEADING
        # invalid characters leaves "adr-007-cache" (no digit prefix to
        # strip) unvalidated and still rejected by WorkItem.key's pattern.
        src = tmp_path / "decisions"
        dst = tmp_path / "work" / "decisions"
        src.mkdir(parents=True)
        (src / "adr-007-cache.yaml").write_text(
            "title: Cache strategy\nreason: because.\n", encoding="utf-8",
        )
        rc = _cmd_migrate_decisions(src, dst, dry_run=False)
        assert rc == 0
        assert (dst / "adr_007_cache.md").is_file()

    def test_invalid_source_key_field_gets_sanitized_too(self, tmp_path: Path, capsys):
        # An untrusted key: field is exactly as likely to violate the
        # pattern as a filename — it must not pass through unchecked.
        src = tmp_path / "decisions"
        dst = tmp_path / "work" / "decisions"
        src.mkdir(parents=True)
        (src / "d1.yaml").write_text(
            "key: 'ADR-007: Cache!'\ntitle: D1\nreason: because.\n", encoding="utf-8",
        )
        rc = _cmd_migrate_decisions(src, dst, dry_run=False)
        assert rc == 0
        targets = list(dst.glob("*.md"))
        assert len(targets) == 1
        assert targets[0].stem[0].isalpha()

    def test_same_run_collision_is_reported_including_in_dry_run(self, tmp_path: Path, capsys):
        src = tmp_path / "decisions"
        dst = tmp_path / "work" / "decisions"
        src.mkdir(parents=True)
        (src / "a.yaml").write_text("key: shared\ntitle: A\nreason: a.\n", encoding="utf-8")
        (src / "b.yaml").write_text("key: shared\ntitle: B\nreason: b.\n", encoding="utf-8")

        rc = _cmd_migrate_decisions(src, dst, dry_run=True)
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["conflicts"] == {"shared": ["a.yaml", "b.yaml"]}

    def test_unknown_status_defaults_to_open_and_is_reported(self, tmp_path: Path, capsys):
        src = tmp_path / "decisions"
        dst = tmp_path / "work" / "decisions"
        src.mkdir(parents=True)
        (src / "d1.yaml").write_text(
            "key: d1\ntitle: D1\nstatus: withdrawn\nreason: because.\n", encoding="utf-8",
        )
        rc = _cmd_migrate_decisions(src, dst, dry_run=False)
        assert rc == 0
        card = (dst / "d1.md").read_text(encoding="utf-8")
        assert "status: open" in card
        capsys.readouterr()

        rc2 = _cmd_migrate_decisions(src, dst, dry_run=True)
        assert rc2 == 0
        data = json.loads(capsys.readouterr().out)
        assert data["unmapped_status"] == {"d1": "withdrawn"}

    def test_rejected_status_note_is_kept_in_the_body(self, tmp_path: Path, capsys):
        src = tmp_path / "decisions"
        dst = tmp_path / "work" / "decisions"
        src.mkdir(parents=True)
        (src / "d1.yaml").write_text(
            "key: d1\ntitle: D1\nstatus: deprecated\nreason: because.\n", encoding="utf-8",
        )
        rc = _cmd_migrate_decisions(src, dst, dry_run=False)
        assert rc == 0
        assert "legacy status: deprecated" in (dst / "d1.md").read_text(encoding="utf-8")

    def test_supersedes_is_mapped_not_dropped(self, tmp_path: Path, capsys):
        src = tmp_path / "decisions"
        dst = tmp_path / "work" / "decisions"
        src.mkdir(parents=True)
        (src / "d1.yaml").write_text(
            "key: d1\ntitle: D1\nsupersedes: old_decision\nreason: because.\n",
            encoding="utf-8",
        )
        rc = _cmd_migrate_decisions(src, dst, dry_run=True)
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert "d1" not in data["dropped_fields"]

        rc2 = _cmd_migrate_decisions(src, dst, dry_run=False)
        assert rc2 == 0
        assert "supersedes: old_decision" in (dst / "d1.md").read_text(encoding="utf-8")

    def test_non_dict_yaml_is_skipped_not_a_crash(self, tmp_path: Path, capsys):
        src = tmp_path / "decisions"
        dst = tmp_path / "work" / "decisions"
        src.mkdir(parents=True)
        (src / "bad.yaml").write_text("- just\n- a\n- list\n", encoding="utf-8")
        self._write_legacy_decision(src, "d1")

        rc = _cmd_migrate_decisions(src, dst, dry_run=False)
        assert rc == 0
        assert (dst / "d1.md").is_file()
        assert not (dst / "bad.md").exists()

    def test_invalid_theme_is_rejected_at_parse_time(self, monkeypatch, capsys, tmp_path):
        # Validated against the real WorkTheme rule (_validate_theme_slug),
        # not a hand-written regex that could diverge from it — that rule
        # only rejects empty/whitespace-containing themes and one reserved
        # sentinel, so the case exercised here is "has a space", the one
        # unambiguous rejection the model itself guarantees.
        src = tmp_path / "decisions"
        dst = tmp_path / "work" / "decisions"
        self._write_legacy_decision(src, "d1")
        monkeypatch.setattr("sys.argv", [
            "context-toolkit-query", "--migrate-decisions",
            "--from", str(src), "--to", str(dst), "--dry-run",
            "--theme", "not a slug",
        ])
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 2

    def test_examples_migrations_after_matches_a_fresh_run(self, tmp_path: Path, capsys):
        """`examples/migrations/README.md` promises `after/` is exactly what
        running the command against `before/` produces, not hand-edited —
        this is what keeps that promise true as the command evolves.
        Re-runs the real migration into a scratch dir and diffs file-by-file
        against the checked-in `after/`."""
        examples_dir = Path(__file__).resolve().parents[1] / "examples" / "migrations"
        before = examples_dir / "before"
        after = examples_dir / "after"
        scratch = tmp_path / "after"

        rc = _cmd_migrate_decisions(before, scratch, dry_run=False, theme="migrations")
        assert rc == 0
        capsys.readouterr()

        expected = {p.name for p in after.glob("*.md")}
        actual = {p.name for p in scratch.glob("*.md")}
        assert actual == expected
        for name in expected:
            assert (scratch / name).read_text(encoding="utf-8") == \
                (after / name).read_text(encoding="utf-8"), f"{name} drifted from a fresh run"

        # Backs the README's claim that both migrated cards load cleanly —
        # strict=True raises on the first malformed file, so reaching this
        # line at all is the proof; .get() confirms both keys resolve.
        engine = WorkEngine.from_directory(after, strict=True)
        assert engine.get("layered_config") is not None
        assert engine.get("per_module_env_reads") is not None

        # strict-load + .get() alone missed two real bugs in an earlier
        # round (a dangling `supersedes` reference and a `closed:` date on
        # an open-status card) — lint() is what actually caught them.
        lint = engine.lint()
        assert not lint.get("dangling_refs")
        assert not lint.get("unclosed_pairs")


class TestCLIDiagnostics:
    def test_cmd_doctor_text(self, capsys):
        rc = _cmd_doctor(json_format=False)
        assert rc == 0
        captured = capsys.readouterr().out
        assert "=== mcp-context-toolkit doctor ===" in captured
        assert "Rules Store:" in captured

    def test_cmd_doctor_json(self, capsys):
        rc = _cmd_doctor(json_format=True)
        assert rc == 0
        captured = capsys.readouterr().out
        data = json.loads(captured)
        assert data["status"] == "ok"
        assert "stores" in data

    def test_cmd_validate_all(self, capsys):
        rc = _cmd_validate_all()
        assert rc == 0
        captured = capsys.readouterr().out
        data = json.loads(captured)
        assert data["ok"] is True


def _write_work_item(d: Path, key: str, theme: str = "engine", status: str = "open",
                     kind: str = "step", part_of: str | None = None) -> None:
    d.mkdir(parents=True, exist_ok=True)
    fm = (f"key: {key}\ntitle: Title {key}\nkind: {kind}\nstatus: {status}\n"
          f"theme: {theme}\nopened: 2026-01-01\n")
    if part_of:
        fm += f"part_of: {part_of}\n"
    if status == "done":
        fm += "closed: 2026-01-02\nevidence:\n  commits: [abc123]\n"
    (d / f"{key}.md").write_text(f"---\n{fm}---\n\nBody for {key}.\n", encoding="utf-8")


class TestWorkPayload:
    """mct-Context-Studio-Vollausbau Etappe A: the viewer's Work tab reads
    this shape, so its fields are load-bearing, not incidental."""

    def test_stats_and_item_fields(self, tmp_path: Path):
        _write_work_item(tmp_path, "a", theme="engine", status="open")
        _write_work_item(tmp_path, "b", theme="engine", status="done", part_of="a")
        _write_work_item(tmp_path, "c", theme="infra", status="active")

        payload = _work_payload(tmp_path)
        assert payload["kind"] == "context-studio/work"
        assert payload["stats"]["items"] == 3
        assert payload["stats"]["by_theme"] == {"engine": 2, "infra": 1}
        assert payload["stats"]["by_status"] == {"open": 1, "done": 1, "active": 1}
        assert payload["stats"]["edges"] == 1

        by_key = {i["key"]: i for i in payload["items"]}
        assert by_key["a"]["status"] == "open"
        assert by_key["a"]["has_evidence"] is False
        assert by_key["b"]["status"] == "done"
        assert by_key["b"]["has_evidence"] is True
        assert by_key["b"]["part_of"] == "a"
        assert payload["edges"] == [{"source": "b", "target": "a"}]

    def test_include_archived_so_the_browse_tab_sees_everything(self, tmp_path: Path):
        # list(include_archived=True) is the deliberate choice (see the
        # function's docstring) — a `dropped` item must still show up so
        # the viewer, not the export step, decides what to filter.
        _write_work_item(tmp_path, "gone", status="dropped")
        payload = _work_payload(tmp_path)
        assert payload["stats"]["by_status"] == {"dropped": 1}
        assert [i["key"] for i in payload["items"]] == ["gone"]

    def test_load_errors_surface_instead_of_vanishing(self, tmp_path: Path):
        tmp_path.mkdir(parents=True, exist_ok=True)
        (tmp_path / "broken.md").write_text(
            "---\nkey: broken\ntitle: X\ntheme: not_a_real_theme\nopened: 2026-01-01\n---\n",
            encoding="utf-8",
        )
        payload = _work_payload(tmp_path)
        assert payload["stats"]["items"] == 0
        assert len(payload["load_errors"]) == 1


class TestDocsPayload:
    def test_stats_and_section_fields(self, tmp_path: Path):
        tmp_path.mkdir(parents=True, exist_ok=True)
        (tmp_path / "a.md").write_text(
            "Lead-in text.\n\n## First\nbody one\n\n## Second\nbody two\n",
            encoding="utf-8",
        )
        (tmp_path / "b.md").write_text("## Only\nsomething\n", encoding="utf-8")

        payload = _docs_payload(tmp_path)
        assert payload["kind"] == "context-studio/docs"
        assert payload["stats"] == {"sections": 4, "files": 2}
        headings = sorted(s["heading"] for s in payload["sections"])
        assert headings == ["", "First", "Only", "Second"]
        first = next(s for s in payload["sections"] if s["heading"] == "First")
        assert first["path"] == "a.md"
        assert first["line"] == 3

    def test_empty_dir_yields_zero_sections(self, tmp_path: Path):
        tmp_path.mkdir(parents=True, exist_ok=True)
        payload = _docs_payload(tmp_path)
        assert payload["stats"] == {"sections": 0, "files": 0}
        assert payload["sections"] == []


class TestExportStudioIncludesWorkAndDocs:
    """Etappe A's actual "Fertig = Beobachtung": all four domains reach
    out_dir when their store dirs are supplied — and are correctly SKIPPED
    (not crash) when absent, matching the pre-existing memory/rules
    behaviour in `_cmd_export_studio`."""

    def test_writes_work_and_docs_json_when_dirs_given(self, tmp_path: Path, capsys):
        rules_dir = tmp_path / "rules"
        rules_dir.mkdir()
        work_dir = tmp_path / "work"
        _write_work_item(work_dir, "a")
        docs_dir = tmp_path / "docs"
        docs_dir.mkdir()
        (docs_dir / "x.md").write_text("## Heading\ntext\n", encoding="utf-8")
        out_dir = tmp_path / "out"

        rc = _cmd_export_studio(rules_dir, None, out_dir, work_dir=work_dir, docs_dir=docs_dir)
        assert rc == 0
        written = json.loads(capsys.readouterr().out)["written"]
        assert "work.json" in written
        assert "docs.json" in written
        assert json.loads((out_dir / "work.json").read_text())["stats"]["items"] == 1
        assert json.loads((out_dir / "docs.json").read_text())["stats"]["sections"] == 1

    def test_missing_work_and_docs_dirs_are_skipped_not_fatal(self, tmp_path: Path, capsys):
        rules_dir = tmp_path / "rules"
        rules_dir.mkdir()
        out_dir = tmp_path / "out"

        rc = _cmd_export_studio(rules_dir, None, out_dir, work_dir=None, docs_dir=None)
        assert rc == 0
        written = json.loads(capsys.readouterr().out)["written"]
        assert "work.json" not in written
        assert "docs.json" not in written
        assert not (out_dir / "work.json").exists()
        assert not (out_dir / "docs.json").exists()


class TestLogHaikuCompareArgvWiring:
    """W8-Reviewer-Fund (Runde 2, D-1): a prior test called
    `_cmd_log_haiku_compare` directly with Python lists, so it never exercised
    `--haiku-mechanical`/`--haiku-names` as repeated argparse flags nor
    `main()`'s own positional wiring — a swapped `args.haiku_mechanical`/
    `args.haiku_names` in that wiring would pass every other test while
    silently mislabeling the exact comparison data this feature exists to
    produce. This test goes through `main()` with real argv."""

    def test_main_wires_log_haiku_compare_with_repeated_flags(
        self, tmp_path: Path, monkeypatch, capsys,
    ):
        memory_dir = tmp_path / ".claude" / "memory"
        memory_dir.mkdir(parents=True)
        (memory_dir / "a.md").write_text(
            "---\nname: alpha_pkg\ntier: project\n---\n\nbody\n", encoding="utf-8",
        )
        monkeypatch.delenv("CONTEXT_USER_MEMORY_DIR", raising=False)

        monkeypatch.setattr("sys.argv", [
            "context-toolkit-query", "--memory-dir", str(memory_dir),
            "--log-haiku-compare", "1",
            "--haiku-query", "shared topic keyword",
            "--haiku-mechanical", "MECH_A", "--haiku-mechanical", "MECH_B",
            "--haiku-names", "HAIKU_X",
            "--haiku-duration-ms", "789",
        ])
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 0

        state_dir = tmp_path / ".claude" / ".session_state"
        log_files = list(state_dir.glob("*.recall_events.jsonl"))
        assert len(log_files) == 1
        entry = json.loads(log_files[0].read_text(encoding="utf-8").splitlines()[0])
        assert entry["event"] == "haiku_compare"
        assert entry["turn"] == 1
        # The tell-tale for a swapped wiring: mechanical vs. haiku land in the
        # WRONG field but the run still exits 0 and writes valid JSON — only
        # asserting the exact field<->flag pairing catches that.
        assert entry["mechanical_names"] == ["MECH_A", "MECH_B"]
        assert entry["haiku_names"] == ["HAIKU_X"]
        assert entry["duration_ms"] == 789

    def test_main_log_haiku_compare_turn_zero_is_reachable(
        self, tmp_path: Path, monkeypatch, capsys,
    ):
        # `--log-haiku-compare 0` must not be swallowed by an `if args.x:`
        # truthiness check — turn 0 is a real value (log_opened uses it
        # before the first recall of a session).
        memory_dir = tmp_path / ".claude" / "memory"
        memory_dir.mkdir(parents=True)
        monkeypatch.delenv("CONTEXT_USER_MEMORY_DIR", raising=False)
        monkeypatch.setattr("sys.argv", [
            "context-toolkit-query", "--memory-dir", str(memory_dir),
            "--log-haiku-compare", "0", "--haiku-query", "q",
        ])
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 0
        state_dir = tmp_path / ".claude" / ".session_state"
        entry = json.loads(next(state_dir.glob("*.recall_events.jsonl")).read_text(encoding="utf-8").splitlines()[0])
        assert entry["turn"] == 0
