from datetime import date
from pathlib import Path

import pytest

from claude_rules.engine import RulesEngine, _compile_glob
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
