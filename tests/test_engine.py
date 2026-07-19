from datetime import date
from pathlib import Path

import pytest

from mcp_context_toolkit.engine import RulesEngine, _compile_glob, fingerprint_rules
from mcp_context_toolkit.models import Rule, RuleApplyTo, Decision, DecisionAppliesTo


def _make_rule(key: str, files: list[str], **overrides) -> Rule:
    defaults = dict(
        key=key,
        title=f"Test {key}",
        type="security",
        scope="backend",
        priority="mandatory",
        modules=["all"],
        summary="Test rule — minimum ten chars",
        content="body",
        applies_to=RuleApplyTo(files=files),
        created=date(2026, 1, 1),
    )
    defaults.update(overrides)
    return Rule(**defaults)  # type: ignore[arg-type]


def _make_decision(key: str, files: list[str], **overrides) -> Decision:
    defaults = dict(
        key=key,
        title=f"Decision {key}",
        date=date(2026, 1, 1),
        status="accepted",
        applies_to=DecisionAppliesTo(files=files),
        reason="Test decision — at least ten chars long.",
    )
    defaults.update(overrides)
    return Decision(**defaults)  # type: ignore[arg-type]


def _write_rule_yaml(
    d: Path, key: str, *, priority: str = "mandatory",
    files: list[str] | None = None, title: str | None = None,
) -> None:
    """Write a minimal but schema-valid rule YAML into directory ``d`` — used by
    the tier-layering tests that exercise real from_roots / load_directory loads."""
    d.mkdir(parents=True, exist_ok=True)
    files = files or ["**/*"]
    title = title or f"Rule {key}"
    files_block = "\n".join(f'    - "{f}"' for f in files)
    (d / f"{key}.yaml").write_text(
        f"""key: {key}
title: "{title}"
type: workflow
scope: all
priority: {priority}
modules: [all]
applies_to:
  files:
{files_block}
summary: |
  Summary for {key} — at least ten characters.
content: |
  Body for {key}.
created: 2026-07-08
""",
        encoding="utf-8",
    )


class TestCompileGlob:
    @pytest.mark.parametrize(
        "path,pattern,expected",
        [
            ("backend/app/api/chat.py", "backend/app/api/*.py", True),
            ("backend/app/api/routers/chat.py", "backend/app/api/*.py", False),
            ("backend/app/api/routers/chat.py", "backend/app/api/**/*.py", True),
            ("backend/app/api/foo.py", "backend/app/api/**/*.py", True),
            ("backend/app/api/a/b/c/d.py", "backend/app/api/**/*.py", True),
            ("web/static/js/main.js", "web/static/**/*.js", True),
            ("web/static/css/main.css", "web/static/**/*.js", False),
            ("a/b.txt", "a/b.???", True),
            ("a/b.tstx", "a/b.???", False),
            ("a/subdir/b.txt", "a/b.???", False),
        ],
    )
    def test_patterns(self, path, pattern, expected):
        assert (_compile_glob(pattern).match(path) is not None) == expected


class TestRulesEngine:
    def test_query_for_file_filters_by_glob(self):
        r1 = _make_rule("a", ["backend/**/*.py"])
        r2 = _make_rule("b", ["web/**/*.js"])
        r3 = _make_rule("c", ["backend/app/api/**/*.py"])

        engine = RulesEngine([r1, r2, r3])

        matches = engine.query_for_file("backend/app/api/routers/chat.py")
        keys = {r.key for r in matches}
        assert keys == {"a", "c"}

    def test_query_for_file_respects_excludes(self):
        r = _make_rule(
            "a",
            ["backend/**/*.py"],
            applies_to=RuleApplyTo(
                files=["backend/**/*.py"],
                excludes=["backend/app/services/auth/__init__.py"],
            ),
        )
        engine = RulesEngine([r])

        assert engine.query_for_file("backend/app/api/chat.py") == [r]
        assert engine.query_for_file("backend/app/services/auth/__init__.py") == []

    def test_query_decisions_for_file_matches_globs(self):
        d_backend = _make_decision("dec_a", ["backend/**/*.py"])
        d_frontend = _make_decision("dec_b", ["web/**/*.js"])
        # dec_c has two globs that BOTH match the target — it must still appear once
        d_overlap = _make_decision("dec_c", ["backend/**/*.py", "backend/app/**/*.py"])
        engine = RulesEngine(decisions=[d_backend, d_frontend, d_overlap])

        target = "backend/app/services/reporting.py"
        assert [d.key for d in engine.query_decisions_for_file(target)] == ["dec_a", "dec_c"]
        # non-matching path yields nothing
        assert engine.query_decisions_for_file("README.md") == []

    def test_query_decisions_filters_status_by_default(self):
        accepted = _make_decision("dec_ok", ["backend/**/*.py"])
        draft = _make_decision("dec_draft", ["backend/**/*.py"], status="draft")
        superseded = _make_decision(
            "dec_old", ["backend/**/*.py"], status="superseded"
        )
        engine = RulesEngine(decisions=[accepted, draft, superseded])

        target = "backend/app/x.py"
        assert [d.key for d in engine.query_decisions_for_file(target)] == ["dec_ok"]
        # statuses=None disables the filter (raw access for audits/tooling)
        raw = engine.query_decisions_for_file(target, statuses=None, top_k=None)
        assert {d.key for d in raw} == {"dec_ok", "dec_draft", "dec_old"}

    def test_query_decisions_top_k_keeps_newest(self):
        decisions = [
            _make_decision(f"dec_{i:02d}", ["backend/**/*.py"], date=date(2026, 1, i))
            for i in range(1, 13)
        ]
        engine = RulesEngine(decisions=decisions)

        cut = engine.query_decisions_for_file("backend/app/x.py")
        assert len(cut) == 8
        # newest first, oldest four (01-04) dropped
        assert cut[0].key == "dec_12"
        assert {d.key for d in cut} == {f"dec_{i:02d}" for i in range(5, 13)}
        # top_k=None returns the full match set
        assert len(engine.query_decisions_for_file("backend/app/x.py", top_k=None)) == 12

    def test_query_for_file_tiered_floors_only_on_zero_match(self):
        # Shared-tier grundregeln use broad **/* globs (always match); a project
        # rule scoped to backend/**.
        floor = _make_rule("gr_floor", ["**/*"], tier="shared")
        proj = _make_rule("proj_backend", ["backend/**/*.py"], tier="project")
        engine = RulesEngine([floor, proj])

        # Matched file: project rule wins, shared floor suppressed (no dup).
        assert [r.key for r in engine.query_for_file_tiered("backend/app/x.py")] == ["proj_backend"]
        # Zero-match file (outside project globs): the shared floor surfaces.
        assert [r.key for r in engine.query_for_file_tiered("scripts/tool.sh")] == ["gr_floor"]

    def test_query_for_file_tiered_identical_without_shared(self):
        # No shared tier → every match is project-tier → same as query_for_file.
        r = _make_rule("only", ["**/*.py"], tier="project")
        engine = RulesEngine([r])
        p = "backend/app/x.py"
        assert engine.query_for_file_tiered(p) == engine.query_for_file(p)

    def test_query_for_file_sorts_by_priority(self):
        r_low = _make_rule("low", ["**/*.py"], priority="recommended")
        r_high = _make_rule("high", ["**/*.py"], priority="non_negotiable")
        r_mid = _make_rule("mid", ["**/*.py"], priority="mandatory")

        engine = RulesEngine([r_low, r_high, r_mid])
        ordered = [r.key for r in engine.query_for_file("foo.py")]
        assert ordered == ["high", "mid", "low"]

    def test_query_filters_by_type(self):
        r1 = _make_rule("a", ["**/*"], type="security")
        r2 = _make_rule("b", ["**/*"], type="workflow")
        engine = RulesEngine([r1, r2])
        assert [r.key for r in engine.query(type="security")] == ["a"]

    def test_query_filters_by_module_includes_all(self):
        r_all = _make_rule("a", ["**/*"], modules=["all"])
        r_mod_a = _make_rule("b", ["**/*"], modules=["module_a"])
        r_mod_b = _make_rule("c", ["**/*"], modules=["module_b"])
        engine = RulesEngine([r_all, r_mod_a, r_mod_b])

        result = {r.key for r in engine.query(module="module_a")}
        assert result == {"a", "b"}

    def test_get_rule_returns_none_for_unknown(self):
        engine = RulesEngine([_make_rule("a", ["**/*"])])
        assert engine.get_rule("missing") is None
        assert engine.get_rule("a").key == "a"

    def test_load_directory_reads_example(self, tmp_path: Path):
        root = tmp_path / ".claude" / "rules" / "security"
        root.mkdir(parents=True)
        example_source = Path(__file__).parent.parent / "examples" / "rules" / "security" / "no_hardcoded_secrets.yaml"
        (root / "s1.yaml").write_text(example_source.read_text(encoding="utf-8"))

        engine = RulesEngine.from_directory(tmp_path / ".claude" / "rules")
        assert len(engine.rules) == 1
        rule = engine.rules[0]
        assert rule.key == "no_hardcoded_secrets"
        assert rule.priority == "non_negotiable"

        matches = engine.query_for_file("src/app/main.py")
        assert len(matches) == 1
        assert matches[0].key == "no_hardcoded_secrets"

        no_matches = engine.query_for_file("web/style.css")
        assert no_matches == []

    def test_load_directory_skips_meta_dirs(self, tmp_path: Path):
        rules = tmp_path / ".claude" / "rules"
        (rules / "_meta").mkdir(parents=True)
        (rules / "_meta" / "schema.yaml").write_text("not: a rule")

        engine = RulesEngine.from_directory(rules)
        assert engine.rules == []

    def test_load_directory_raises_on_duplicate_keys(self, tmp_path: Path):
        rules = tmp_path / ".claude" / "rules" / "security"
        rules.mkdir(parents=True)
        example = (
            Path(__file__).parent.parent / "examples" / "rules" / "security" / "no_hardcoded_secrets.yaml"
        ).read_text(encoding="utf-8")
        (rules / "a.yaml").write_text(example)
        (rules / "b.yaml").write_text(example)

        from mcp_context_toolkit.engine import RuleLoadError

        with pytest.raises(RuleLoadError):
            RulesEngine.from_directory(tmp_path / ".claude" / "rules")


class TestFingerprint:
    def test_empty_rules_returns_zero(self):
        assert fingerprint_rules([]) == "0"

    def test_deterministic(self):
        rules = [_make_rule("a", ["**/*"]), _make_rule("b", ["**/*"])]
        assert fingerprint_rules(rules) == fingerprint_rules(rules)

    def test_order_independent(self):
        r1 = _make_rule("a", ["**/*"])
        r2 = _make_rule("b", ["**/*"])
        assert fingerprint_rules([r1, r2]) == fingerprint_rules([r2, r1])

    def test_content_change_flips_fingerprint(self):
        original = _make_rule("a", ["**/*"], content="original body")
        edited = _make_rule("a", ["**/*"], content="edited body")
        assert fingerprint_rules([original]) != fingerprint_rules([edited])

    def test_summary_change_flips_fingerprint(self):
        original = _make_rule(
            "a", ["**/*"], summary="Rule one — minimum ten chars"
        )
        edited = _make_rule(
            "a", ["**/*"], summary="Rule one edited — ten chars"
        )
        assert fingerprint_rules([original]) != fingerprint_rules([edited])

    def test_title_change_flips_fingerprint(self):
        original = _make_rule("a", ["**/*"], title="Old Title")
        edited = _make_rule("a", ["**/*"], title="New Title")
        assert fingerprint_rules([original]) != fingerprint_rules([edited])

    def test_priority_change_flips_fingerprint(self):
        low = _make_rule("a", ["**/*"], priority="recommended")
        high = _make_rule("a", ["**/*"], priority="non_negotiable")
        assert fingerprint_rules([low]) != fingerprint_rules([high])

    def test_metadata_change_does_not_flip(self):
        # source_path, created, owner, review_interval are metadata — excluded
        from datetime import date

        r1 = _make_rule("a", ["**/*"], created=date(2026, 1, 1), owner="alice")
        r2 = _make_rule("a", ["**/*"], created=date(2026, 4, 13), owner="bob")
        assert fingerprint_rules([r1]) == fingerprint_rules([r2])

    def test_adding_rule_flips_fingerprint(self):
        r1 = _make_rule("a", ["**/*"])
        r2 = _make_rule("b", ["**/*"])
        assert fingerprint_rules([r1]) != fingerprint_rules([r1, r2])

    def test_different_rules_different_fingerprint(self):
        r1 = _make_rule("a", ["**/*"], content="one")
        r2 = _make_rule("b", ["**/*"], content="two")
        assert fingerprint_rules([r1]) != fingerprint_rules([r2])

    def test_engine_query_for_file_with_fingerprint(self):
        r1 = _make_rule("a", ["backend/**/*.py"])
        r2 = _make_rule("b", ["web/**/*.js"])
        engine = RulesEngine([r1, r2])

        matches_a, fp_a = engine.query_for_file_with_fingerprint(
            "backend/app/main.py"
        )
        assert [r.key for r in matches_a] == ["a"]
        assert fp_a != "0"

        matches_none, fp_none = engine.query_for_file_with_fingerprint(
            "docs/readme.md"
        )
        assert matches_none == []
        assert fp_none == "0"


class TestValidateAndFallback:
    def _write_example(self, rules_dir: Path) -> None:
        src = Path(__file__).parent.parent / "examples" / "rules" / "security" / "no_hardcoded_secrets.yaml"
        (rules_dir / "s1.yaml").write_text(src.read_text(encoding="utf-8"))

    def test_validate_directory_ok(self, tmp_path: Path):
        rules = tmp_path / ".claude" / "rules" / "security"
        rules.mkdir(parents=True)
        self._write_example(rules)

        result = RulesEngine.validate_directory(tmp_path / ".claude" / "rules")
        assert result["ok"] is True
        assert result["rule_count"] == 1
        assert result["errors"] == []

    def test_validate_directory_reports_parse_error(self, tmp_path: Path):
        rules = tmp_path / ".claude" / "rules" / "security"
        rules.mkdir(parents=True)
        (rules / "broken.yaml").write_text("not: valid: yaml:\n  - [unclosed")

        result = RulesEngine.validate_directory(tmp_path / ".claude" / "rules")
        assert result["ok"] is False
        assert len(result["errors"]) == 1
        assert "parse error" in result["errors"][0].lower() or "YAML" in result["errors"][0]

    def test_load_directory_lenient_skips_broken(self, tmp_path: Path):
        rules = tmp_path / ".claude" / "rules" / "security"
        rules.mkdir(parents=True)
        self._write_example(rules)
        (rules / "broken.yaml").write_text("not: valid: yaml:\n  - [unclosed")

        engine = RulesEngine()
        stats = engine.load_directory(tmp_path / ".claude" / "rules", strict=False)
        assert stats["loaded"] == 1  # only s1 loaded
        assert len(stats["errors"]) == 1
        assert len(engine.rules) == 1
        assert engine.rules[0].key == "no_hardcoded_secrets"

    def test_write_fallback_markdown(self, tmp_path: Path):
        rules = tmp_path / ".claude" / "rules" / "security"
        rules.mkdir(parents=True)
        self._write_example(rules)

        engine = RulesEngine.from_directory(tmp_path / ".claude" / "rules")
        target = tmp_path / ".claude" / "rules" / "_meta" / "fallback_rules.md"
        result = engine.write_fallback_markdown(target)

        assert target.exists()
        content = target.read_text(encoding="utf-8")
        assert "context-toolkit Fallback" in content
        assert "SEC1" in content
        assert "No Hardcoded Secrets" in content
        assert result["written"] == 1


class TestStarterPackNotAutoLoaded:
    """The shipped starter pack (examples/rules) is inert — copy-to-activate.
    Auto-discovery walks for `<dir>/.claude/rules`, so the examples tree can
    never be picked up implicitly. These tests pin that contract so the grundpaket
    can't silently 'poison' a real rule set."""

    def test_autodiscovery_never_picks_up_examples(self, tmp_path: Path, monkeypatch):
        from mcp_context_toolkit.cli import _discover_rules_dir

        # Mimic the published repo layout: examples/rules exists, NO .claude/rules.
        ex = tmp_path / "examples" / "rules" / "security"
        ex.mkdir(parents=True)
        (ex / "x.yaml").write_text("key: x\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        with pytest.raises(FileNotFoundError):
            _discover_rules_dir()

    def test_explicit_example_path_warns_loudly(self, tmp_path: Path, capsys):
        from mcp_context_toolkit.cli import _resolve_rules_dir

        ex = tmp_path / "examples" / "rules"
        ex.mkdir(parents=True)
        _resolve_rules_dir(str(ex))

        assert "EXAMPLE" in capsys.readouterr().err

    def test_real_rules_dir_does_not_warn(self, tmp_path: Path, capsys):
        from mcp_context_toolkit.cli import _resolve_rules_dir

        real = tmp_path / ".claude" / "rules"
        real.mkdir(parents=True)
        _resolve_rules_dir(str(real))

        assert "EXAMPLE" not in capsys.readouterr().err


class TestRulesPayloadStudioFields:
    """The studio/export payload carries the fields an embedding host (the VS Code
    extension) needs: per-rule source_path + tier, and a top-level skipped list."""

    def test_payload_has_source_path_and_tier(self, tmp_path: Path):
        from mcp_context_toolkit.cli import _rules_payload

        proj = tmp_path / "proj" / "rules"
        _write_rule_yaml(proj, "a")
        engine = RulesEngine.from_roots({"project": proj}, strict=False)
        payload = _rules_payload(engine)

        assert payload["rules"][0]["tier"] == "project"
        assert payload["rules"][0]["source_path"].endswith("a.yaml")
        assert payload["skipped"] == []

    def test_payload_skipped_lists_invalid_files(self, tmp_path: Path):
        from mcp_context_toolkit.cli import _rules_payload

        proj = tmp_path / "proj" / "rules"
        shared = tmp_path / "shared"
        proj.mkdir(parents=True)
        (proj / "broken.yaml").write_text(
            "key: p_broken\ntitle: Broken\ntype: workflow\nscope: all\n"
            "priority: mandatory\nmodules: [all]\n"
            "applies_to:\n  files:\n    - \"**/*\"\n"
            "summary: created fehlt also invalide.\ncontent: body\n",
            encoding="utf-8",
        )
        _write_rule_yaml(shared, "g_valid")
        engine = RulesEngine.from_roots({"project": proj, "shared": shared}, strict=False)
        payload = _rules_payload(engine)

        assert payload["stats"]["rules"] == 1
        assert len(payload["skipped"]) == 1
        assert "[project]" in payload["skipped"][0]

    def test_load_tiers_shared_only_when_no_project(self, tmp_path: Path, monkeypatch):
        # A fresh workspace (no project rules) must still surface the shared
        # grundregeln — _load_all_rule_tiers tolerates rules_dir=None. Mirrors the
        # server's project-optional wiring; this is what feeds the VS Code plugin
        # in a workspace that has no .context/rules of its own.
        from mcp_context_toolkit import cli as cli_mod

        shared = tmp_path / "shared"
        _write_rule_yaml(shared, "g_floor")
        monkeypatch.setattr(cli_mod, "discover_shared_rules_dir", lambda: shared)

        engine, warnings = cli_mod._load_all_rule_tiers(None, strict=False)
        assert [r.key for r in engine.rules] == ["g_floor"]
        assert engine.rules[0].tier == "shared"
        assert warnings == []


class TestStoreConventionDiscovery:
    """Auto-discovery accepts `.context/` (generic default) and `.claude/`
    (Claude Code fallback). `.context/` wins when both are present."""

    def test_context_dir_is_discovered(self, tmp_path: Path, monkeypatch):
        from mcp_context_toolkit.cli import _discover_rules_dir

        rules = tmp_path / ".context" / "rules"
        rules.mkdir(parents=True)
        monkeypatch.chdir(tmp_path)

        assert _discover_rules_dir() == rules

    def test_claude_dir_still_discovered_as_fallback(self, tmp_path: Path, monkeypatch):
        from mcp_context_toolkit.cli import _discover_rules_dir

        rules = tmp_path / ".claude" / "rules"
        rules.mkdir(parents=True)
        monkeypatch.chdir(tmp_path)

        assert _discover_rules_dir() == rules

    def test_context_wins_over_claude(self, tmp_path: Path, monkeypatch):
        from mcp_context_toolkit.cli import _discover_rules_dir

        ctx = tmp_path / ".context" / "rules"
        ctx.mkdir(parents=True)
        (tmp_path / ".claude" / "rules").mkdir(parents=True)
        monkeypatch.chdir(tmp_path)

        assert _discover_rules_dir() == ctx

    def test_context_memory_is_discovered(self, tmp_path: Path, monkeypatch):
        from mcp_context_toolkit.cli import _discover_memory_dir

        mem = tmp_path / ".context" / "memory"
        mem.mkdir(parents=True)
        monkeypatch.chdir(tmp_path)

        assert _discover_memory_dir(None) == mem


class TestSharpnessLint:
    """validate_directory warnt, wenn eine non_negotiable/mandatory-Summary in den
    ersten 160 Zeichen kein Modal-/Verbots-Token traegt (eval-belegtes Format:
    schwache Modelle verfehlen Regeln, deren Gebot hinter dem Injektions-Fenster
    liegt). recommended ist ausgenommen."""

    def _write(self, d: Path, key: str, summary: str, priority: str = "mandatory"):
        _write_rule_yaml(d, key, priority=priority)
        p = d / f"{key}.yaml"
        s = p.read_text(encoding="utf-8")
        import re
        p.write_text(re.sub(r"summary: \|\n  .*\n", f"summary: |\n  {summary}\n", s), encoding="utf-8")

    def test_virtue_summary_warns(self, tmp_path: Path):
        d = tmp_path / "rules"
        self._write(d, "vague_rule", "Work carefully and verify things thoroughly when editing code here.")
        r = RulesEngine.validate_directory(d)
        assert any("modal/prohibition" in w for w in r["warnings"])

    def test_prohibition_summary_passes(self, tmp_path: Path):
        d = tmp_path / "rules"
        self._write(d, "sharp_rule", "NEVER invent an API signature from memory - open the source first.")
        r = RulesEngine.validate_directory(d)
        assert not any("modal/prohibition" in w for w in r["warnings"])

    def test_recommended_is_exempt(self, tmp_path: Path):
        d = tmp_path / "rules"
        self._write(d, "soft_rule", "Consider extracting helpers when files grow large over time here.",
                    priority="recommended")
        r = RulesEngine.validate_directory(d)
        assert not any("modal/prohibition" in w for w in r["warnings"])

    def test_modal_outside_window_warns(self, tmp_path: Path):
        d = tmp_path / "rules"
        filler = "This paragraph describes background context of the system architecture in a descriptive tone for a while now " * 2
        self._write(d, "buried_rule", filler + " NEVER do the bad thing.")
        r = RulesEngine.validate_directory(d)
        assert any("modal/prohibition" in w for w in r["warnings"])


class TestStoreConventionsEnv:
    """CONTEXT_STORE_CONVENTIONS makes the walk-up conventions configurable
    (branding, e.g. ".acme") without forking the engine. Default unchanged."""

    def test_env_adds_branded_convention(self, tmp_path: Path, monkeypatch):
        from mcp_context_toolkit.cli import _discover_rules_dir

        rules = tmp_path / ".acme" / "rules"
        rules.mkdir(parents=True)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("CONTEXT_STORE_CONVENTIONS", ".acme,.context,.claude")
        assert _discover_rules_dir() == rules

    def test_default_ignores_unknown_dirs(self, tmp_path: Path, monkeypatch):
        from mcp_context_toolkit.cli import _discover_rules_dir

        (tmp_path / ".acme" / "rules").mkdir(parents=True)
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("CONTEXT_STORE_CONVENTIONS", raising=False)
        with pytest.raises(FileNotFoundError):
            _discover_rules_dir()

    def test_mcp_server_discover_returns_none_without_rules(self, tmp_path: Path, monkeypatch):
        # Project rules are OPTIONAL for the server: no dir -> None, not raise
        # (a bare workspace must not lose memory tools / shared grundregeln).
        from mcp_context_toolkit.mcp_server import _discover_rules_dir as srv_discover

        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("CONTEXT_RULES_DIR", raising=False)
        assert srv_discover() is None

    def test_mcp_server_env_conventions(self, tmp_path: Path, monkeypatch):
        from mcp_context_toolkit.mcp_server import _discover_rules_dir as srv_discover

        rules = tmp_path / ".acme" / "rules"
        rules.mkdir(parents=True)
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("CONTEXT_RULES_DIR", raising=False)
        monkeypatch.setenv("CONTEXT_STORE_CONVENTIONS", ".acme")
        assert srv_discover() == rules


class TestTierLayering:
    """2-tier rules (project + shared org grundregeln). Load order IS precedence:
    project loaded first wins on a non-security collision; a non_negotiable
    collision on either side is a hard error (a security rule is never shadowed)."""

    def test_from_roots_accumulates_both_tiers(self, tmp_path: Path):
        proj = tmp_path / "proj" / "rules"
        shared = tmp_path / "shared"
        _write_rule_yaml(proj, "proj_only")
        _write_rule_yaml(shared, "shared_only")

        e = RulesEngine.from_roots({"project": proj, "shared": shared})
        assert {r.key for r in e.rules} == {"proj_only", "shared_only"}
        assert e.get_rule("proj_only").tier == "project"     # type: ignore[union-attr]
        assert e.get_rule("shared_only").tier == "shared"    # type: ignore[union-attr]

    def test_roots_property_records_load_order(self, tmp_path: Path):
        proj = tmp_path / "proj" / "rules"
        shared = tmp_path / "shared"
        _write_rule_yaml(proj, "a")
        _write_rule_yaml(shared, "b")

        e = RulesEngine.from_roots({"project": proj, "shared": shared})
        assert [t for _, t in e.roots] == ["project", "shared"]

    def test_project_wins_on_mandatory_collision(self, tmp_path: Path):
        proj = tmp_path / "proj" / "rules"
        shared = tmp_path / "shared"
        _write_rule_yaml(proj, "dup", title="Project version", priority="mandatory")
        _write_rule_yaml(shared, "dup", title="Shared version", priority="recommended")

        e = RulesEngine.from_roots({"project": proj, "shared": shared})
        rule = e.get_rule("dup")
        assert rule is not None
        assert rule.title == "Project version"   # project loaded first wins
        assert rule.tier == "project"
        assert len(e.rules) == 1

    def test_non_negotiable_shadow_raises_when_incoming(self, tmp_path: Path):
        # shared carries a non_negotiable rule, project re-uses the key at a lower
        # priority -> project (first) would silently downgrade the security rule.
        from mcp_context_toolkit.engine import RuleLoadError

        proj = tmp_path / "proj" / "rules"
        shared = tmp_path / "shared"
        _write_rule_yaml(proj, "sec", priority="mandatory")
        _write_rule_yaml(shared, "sec", priority="non_negotiable")

        with pytest.raises(RuleLoadError):
            RulesEngine.from_roots({"project": proj, "shared": shared})

    def test_non_negotiable_shadow_raises_when_existing(self, tmp_path: Path):
        # project owns the non_negotiable; a shared key clash is still an error.
        from mcp_context_toolkit.engine import RuleLoadError

        proj = tmp_path / "proj" / "rules"
        shared = tmp_path / "shared"
        _write_rule_yaml(proj, "sec", priority="non_negotiable")
        _write_rule_yaml(shared, "sec", priority="mandatory")

        with pytest.raises(RuleLoadError):
            RulesEngine.from_roots({"project": proj, "shared": shared})

    def test_non_negotiable_collision_not_relaxed_by_lenient(self, tmp_path: Path):
        # The security guard fires even with strict=False (security > leniency).
        from mcp_context_toolkit.engine import RuleLoadError

        proj = tmp_path / "proj" / "rules"
        shared = tmp_path / "shared"
        _write_rule_yaml(proj, "sec", priority="mandatory")
        _write_rule_yaml(shared, "sec", priority="non_negotiable")

        with pytest.raises(RuleLoadError):
            RulesEngine.from_roots({"project": proj, "shared": shared}, strict=False)

    def test_broken_project_yaml_does_not_blank_shared_tier(self, tmp_path: Path):
        # Reproduces the Coder bug (2026-07-14): a schema-invalid project YAML
        # (missing `created`) must NOT take down the valid shared grundregeln.
        # from_roots(strict=False) loads the shared tier and accumulates the
        # skipped project file into engine.load_errors (surfaced by the banner).
        proj = tmp_path / "proj" / "rules"
        shared = tmp_path / "shared"
        proj.mkdir(parents=True)
        (proj / "broken.yaml").write_text(
            "key: p_broken\ntitle: Broken\ntype: workflow\nscope: all\n"
            "priority: mandatory\nmodules: [all]\n"
            "applies_to:\n  files:\n    - \"**/*\"\n"
            "summary: MUSS geladen werden aber created fehlt.\ncontent: body\n",
            encoding="utf-8",
        )
        _write_rule_yaml(shared, "g_code_is_law")

        e = RulesEngine.from_roots({"project": proj, "shared": shared}, strict=False)
        assert [r.key for r in e.rules] == ["g_code_is_law"]
        assert len(e.load_errors) == 1
        assert "[project]" in e.load_errors[0]

    def test_shared_only_tier_loads(self, tmp_path: Path):
        shared = tmp_path / "shared"
        _write_rule_yaml(shared, "grundregel")

        e = RulesEngine.from_roots({"shared": shared})
        assert [r.key for r in e.rules] == ["grundregel"]
        assert e.get_rule("grundregel").tier == "shared"   # type: ignore[union-attr]

    def test_load_directory_accumulates_and_reports_shadowed(self, tmp_path: Path):
        proj = tmp_path / "proj" / "rules"
        shared = tmp_path / "shared"
        _write_rule_yaml(proj, "a")
        _write_rule_yaml(proj, "b")
        _write_rule_yaml(shared, "a")   # mandatory collision -> project wins, shadowed
        _write_rule_yaml(shared, "c")

        e = RulesEngine()
        s1 = e.load_directory(proj, tier="project")
        assert s1["loaded"] == 2 and s1["shadowed"] == 0
        s2 = e.load_directory(shared, tier="shared")
        assert s2["loaded"] == 1      # only c is new
        assert s2["shadowed"] == 1    # a collided, project kept
        assert {r.key for r in e.rules} == {"a", "b", "c"}
        assert e.get_rule("a").tier == "project"   # type: ignore[union-attr]

    def test_fingerprint_ignores_tier(self, tmp_path: Path):
        # Same content, different tier -> identical fingerprint (tier is excluded,
        # so the live-reload hook does not see a spurious "rules changed" signal).
        r_proj = _make_rule("x", ["**/*"], tier="project")
        r_shared = _make_rule("x", ["**/*"], tier="shared")
        assert fingerprint_rules([r_proj]) == fingerprint_rules([r_shared])
