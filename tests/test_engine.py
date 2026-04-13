from datetime import date
from pathlib import Path

import pytest

from claude_rules.engine import RulesEngine, _compile_glob, fingerprint_rules
from claude_rules.models import Rule, RuleApplyTo


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


class TestCompileGlob:
    @pytest.mark.parametrize(
        "path,pattern,expected",
        [
            ("backend/app/api/chat.py", "backend/app/api/*.py", True),
            ("backend/app/api/routers/chat.py", "backend/app/api/*.py", False),
            ("backend/app/api/routers/chat.py", "backend/app/api/**/*.py", True),
            ("backend/app/api/foo.py", "backend/app/api/**/*.py", True),
            ("backend/app/api/a/b/c/d.py", "backend/app/api/**/*.py", True),
            ("sps_webapp/static/js/main.js", "sps_webapp/static/**/*.js", True),
            ("sps_webapp/static/css/main.css", "sps_webapp/static/**/*.js", False),
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
        r2 = _make_rule("b", ["sps_webapp/**/*.js"])
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
        r_themis = _make_rule("b", ["**/*"], modules=["themis"])
        r_prom = _make_rule("c", ["**/*"], modules=["prometheus"])
        engine = RulesEngine([r_all, r_themis, r_prom])

        result = {r.key for r in engine.query(module="themis")}
        assert result == {"a", "b"}

    def test_get_rule_returns_none_for_unknown(self):
        engine = RulesEngine([_make_rule("a", ["**/*"])])
        assert engine.get_rule("missing") is None
        assert engine.get_rule("a").key == "a"

    def test_load_directory_reads_example(self, tmp_path: Path):
        root = tmp_path / ".claude" / "rules" / "security"
        root.mkdir(parents=True)
        example_source = Path(__file__).parent.parent / "examples" / "s1_user_isolation.yaml"
        (root / "s1.yaml").write_text(example_source.read_text(encoding="utf-8"))

        engine = RulesEngine.from_directory(tmp_path / ".claude" / "rules")
        assert len(engine.rules) == 1
        rule = engine.rules[0]
        assert rule.key == "s1_user_isolation"
        assert rule.priority == "non_negotiable"

        matches = engine.query_for_file("backend/app/api/routers/themis_chat.py")
        assert len(matches) == 1
        assert matches[0].key == "s1_user_isolation"

        no_matches = engine.query_for_file("sps_webapp/static/css/chat.css")
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
            Path(__file__).parent.parent / "examples" / "s1_user_isolation.yaml"
        ).read_text(encoding="utf-8")
        (rules / "a.yaml").write_text(example)
        (rules / "b.yaml").write_text(example)

        from claude_rules.engine import RuleLoadError

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
        r2 = _make_rule("b", ["sps_webapp/**/*.js"])
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
        src = Path(__file__).parent.parent / "examples" / "s1_user_isolation.yaml"
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
        assert engine.rules[0].key == "s1_user_isolation"

    def test_write_fallback_markdown(self, tmp_path: Path):
        rules = tmp_path / ".claude" / "rules" / "security"
        rules.mkdir(parents=True)
        self._write_example(rules)

        engine = RulesEngine.from_directory(tmp_path / ".claude" / "rules")
        target = tmp_path / ".claude" / "rules" / "_meta" / "fallback_rules.md"
        result = engine.write_fallback_markdown(target)

        assert target.exists()
        content = target.read_text(encoding="utf-8")
        assert "claude-rules Fallback" in content
        assert "S1" in content
        assert "User Isolation" in content
        assert result["written"] == 1
