"""Direct tests of `server/tools_rules.py`'s `register()` closures — isolated
from FastMCP (a capturing stub decorator, not `build_server`), so a fail-soft
regression inside the closure body itself is caught without standing up a
full server. Added for W8-Reviewer-Fund ERROR-1 (mct-context-studio-
vollausbau Etappe E): moving `work_reloader.current()` OUTSIDE its
try/except broke the documented fail-soft contract — `TestRulesForFilePayload`
in test_engine.py already covers `rules_for_file_payload`'s OWN internal
`for_file()` try/except, but nothing exercised the closure's OUTER guard
around `.current()` itself (the loader raising, not the lookup)."""

from pathlib import Path

from mcp_context_toolkit.engine import RulesEngine
from mcp_context_toolkit.server import tools_rules
from mcp_context_toolkit.work import WorkEngine


def _capturing_mcp_tool(registry: dict):
    def factory(_group: str):
        def decorator(fn):
            registry[fn.__name__] = fn
            return fn
        return decorator
    return factory


class _FakeReloader:
    """Minimal stand-in for `_Reloader` — `.current()` either returns a fixed
    value or raises, whichever the test needs."""

    def __init__(self, value=None, raises: Exception | None = None):
        self._value = value
        self._raises = raises

    def current(self):
        if self._raises is not None:
            raise self._raises
        return self._value


def _register(rules_engine, work_reloader) -> dict:
    tools = {}
    tools_rules.register(_capturing_mcp_tool(tools), _FakeReloader(rules_engine), work_reloader)
    return tools


class TestQueryRulesForFileFailSoft:
    def test_work_reloader_current_raising_is_fail_soft(self):
        """The regressed path itself (W8-Reviewer-Fund ERROR-1): the LOADER
        raises inside `.current()`, not a lookup inside an already-loaded
        engine. Before the fix this propagated a RuntimeError and took the
        rules/decisions answer down with it."""
        engine = RulesEngine()
        broken = _FakeReloader(raises=RuntimeError("work store kaputt"))
        tools = _register(engine, broken)

        import json
        out = json.loads(tools["query_rules_for_file"]("any/file.py"))

        assert out["offene_arbeit"] == []
        assert out["rules"] == []  # leere RulesEngine, aber KEIN Crash

    def test_work_reloader_none_is_fail_soft(self):
        engine = RulesEngine()
        out_json = _register(engine, None)["query_rules_for_file"]("any/file.py")
        import json
        assert json.loads(out_json)["offene_arbeit"] == []

    def test_working_work_reloader_still_returns_matches(self, tmp_path: Path):
        (tmp_path / "w.md").write_text(
            "---\nkey: w\ntitle: W\nkind: step\nstatus: open\ntheme: engine\n"
            "opened: 2026-07-01\nevidence: {files: [any/file.py]}\n---\n\nx\n",
            encoding="utf-8",
        )
        work_engine = WorkEngine.from_directory(tmp_path)
        engine = RulesEngine()
        tools = _register(engine, _FakeReloader(value=work_engine))

        import json
        out = json.loads(tools["query_rules_for_file"]("any/file.py"))
        assert [w["key"] for w in out["offene_arbeit"]] == ["w"]
