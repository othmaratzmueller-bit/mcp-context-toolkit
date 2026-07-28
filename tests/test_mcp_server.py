import os
from pathlib import Path

from mcp_context_toolkit.mcp_server import _Reloader, _tree_mtime
from mcp_context_toolkit.memory import MemoryEngine
from mcp_context_toolkit.work import WorkEngine


def _write_mem(d: Path, name: str, body: str = "x") -> None:
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.md").write_text(f"---\nname: {name}\n---\n\n{body}\n", encoding="utf-8")


def _write_work(d: Path, key: str, status: str = "open") -> None:
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{key}.md").write_text(
        f"---\nkey: {key}\ntitle: {key}\nkind: goal\nstatus: {status}\n"
        f"theme: engine\nopened: 2026-07-01\n---\n\nx\n",
        encoding="utf-8",
    )


def _bump(path: Path) -> None:
    """Force a detectable mtime change, dodging coarse-FS 1s granularity that
    would otherwise make a fast test write land in the same second."""
    target = _tree_mtime(path.parent) + 100
    os.utime(path, (target, target))


class TestTreeMtime:
    def test_missing_tree_is_zero(self, tmp_path: Path):
        assert _tree_mtime(tmp_path / "does-not-exist") == 0.0

    def test_reflects_a_file_edit(self, tmp_path: Path):
        _write_mem(tmp_path, "a")
        before = _tree_mtime(tmp_path)
        _bump(tmp_path / "a.md")                  # edit an EXISTING file
        assert _tree_mtime(tmp_path) > before


class TestReloader:
    def _counting_loader(self, root: Path, counter: dict):
        def loader():
            counter["n"] += 1
            return MemoryEngine.from_directory(root)
        return loader

    def test_builds_once_at_init(self, tmp_path: Path):
        _write_mem(tmp_path, "a")
        c = {"n": 0}
        _Reloader(self._counting_loader(tmp_path, c), [tmp_path])
        assert c["n"] == 1

    def test_no_change_no_reload(self, tmp_path: Path):
        _write_mem(tmp_path, "a")
        c = {"n": 0}
        r = _Reloader(self._counting_loader(tmp_path, c), [tmp_path])
        r.current()
        r.current()
        assert c["n"] == 1                        # nothing changed → no rebuild

    def test_reload_on_change(self, tmp_path: Path):
        _write_mem(tmp_path, "a")
        c = {"n": 0}
        r = _Reloader(self._counting_loader(tmp_path, c), [tmp_path])
        assert r.current().get("b") is None       # b not there yet
        _write_mem(tmp_path, "b")
        _bump(tmp_path / "b.md")
        assert r.current().get("b") is not None    # change picked up
        assert c["n"] == 2                         # exactly one rebuild

    def test_none_watch_dir_is_ignored(self, tmp_path: Path):
        # A None watch dir (e.g. no user-tier memory configured) must not crash.
        _write_mem(tmp_path, "a")
        r = _Reloader(lambda: MemoryEngine.from_directory(tmp_path), [tmp_path, None])
        assert r.current().get("a") is not None



class TestToolInventory:
    """The banner's tool count must be MEASURED, not a maintained literal.

    Regression guard: the count used to be hardcoded ("Tools", 11, ...) and had
    to be bumped by hand whenever a tool was added — it silently drifted from
    what was actually served. These tests fail if the recorded inventory and
    the tools FastMCP really registered ever diverge.
    """

    @staticmethod
    def _server(with_memory: bool, tmp_path: Path, with_work: bool = False):
        from mcp_context_toolkit.engine import RulesEngine
        from mcp_context_toolkit.mcp_server import build_server

        rules = _Reloader(RulesEngine, [])
        memory = None
        if with_memory:
            _write_mem(tmp_path / "memory", "m")
            memory = _Reloader(
                lambda: MemoryEngine.from_directory(tmp_path / "memory"),
                [tmp_path / "memory"],
            )
        work = None
        if with_work:
            _write_work(tmp_path / "work", "w")
            work = _Reloader(
                lambda: WorkEngine.from_directory(tmp_path / "work"),
                [tmp_path / "work"],
            )
        return build_server(rules, memory, None, work)

    def _registered(self, server) -> set:
        import asyncio

        return {t.name for t in asyncio.run(server.list_tools())}

    def test_inventory_matches_registered_tools(self, tmp_path: Path):
        server = self._server(True, tmp_path, with_work=True)
        inv = server.context_toolkit_inventory
        recorded = set(inv["rules"]) | set(inv["memory"]) | set(inv["work"])
        assert recorded == self._registered(server)

    def test_memory_tools_absent_without_a_store(self, tmp_path: Path):
        server = self._server(False, tmp_path)
        inv = server.context_toolkit_inventory
        assert inv["memory"] == []                       # none registered
        assert set(inv["rules"]) == self._registered(server)

    def test_work_tools_absent_without_a_store(self, tmp_path: Path):
        server = self._server(False, tmp_path, with_work=False)
        inv = server.context_toolkit_inventory
        assert inv["work"] == []                          # none registered
        assert set(inv["rules"]) == self._registered(server)

    def test_work_tools_present_with_a_store(self, tmp_path: Path):
        server = self._server(False, tmp_path, with_work=True)
        inv = server.context_toolkit_inventory
        assert set(inv["work"]) == {
            "work_recall", "get_work", "list_work", "work_children",
            "work_lint", "work_status",
        }
        assert set(inv["work"]) <= self._registered(server)

    def test_banner_reports_the_measured_count(self, tmp_path: Path, capsys):
        from mcp_context_toolkit.engine import RulesEngine
        from mcp_context_toolkit.mcp_server import _print_banner

        server = self._server(True, tmp_path, with_work=True)
        inv = server.context_toolkit_inventory
        _print_banner(
            RulesEngine(),
            MemoryEngine.from_directory(tmp_path / "memory"),
            inv,
            WorkEngine.from_directory(tmp_path / "work"),
        )
        banner = capsys.readouterr().err
        total = len(inv["rules"]) + len(inv["memory"]) + len(inv["work"])
        tools_line = next(line for line in banner.splitlines() if "Tools" in line)
        # Read the count back out of the rendered line — asserts the printed
        # figure, independent of column padding.
        assert int(tools_line.split()[1]) == total
        assert f"{len(inv['rules'])} rules" in tools_line
        assert f"{len(inv['memory'])} memory" in tools_line
        assert f"{len(inv['work'])} work" in tools_line
        work_line = next(line for line in banner.splitlines() if line.strip().startswith("Work"))
        assert "1 open" in work_line

    def test_banner_omits_tools_line_when_uncounted(self, tmp_path: Path, capsys):
        """No inventory → no claim. The line is dropped, never guessed."""
        from mcp_context_toolkit.engine import RulesEngine
        from mcp_context_toolkit.mcp_server import _print_banner

        _print_banner(RulesEngine(), None, None)
        assert "Tools" not in capsys.readouterr().err


class TestWorkErrorVisibility:
    """F2 (2026-07-28 review): a work item dropped at load time used to be
    invisible everywhere a real server actually reports through — banner,
    work_lint, work_status. These drive the real tool/banner path end to end,
    the same way a client or an operator would see it."""

    def _server_with_one_bad_file(self, tmp_path: Path):
        from mcp_context_toolkit.mcp_server import build_server

        _write_work(tmp_path, "good", status="open")
        (tmp_path / "bad.md").write_text(
            "---\nkey: bad\ntitle: Bad\nkind: goal\nstatus: open\n"
            "theme: not_a_real_theme\nopened: 2026-07-01\n---\n\nx\n",
            encoding="utf-8",
        )
        rules = _Reloader(lambda: None, [])
        work = _Reloader(lambda: WorkEngine.from_directory(tmp_path, strict=False), [tmp_path])
        return build_server(rules, None, None, work), work

    def test_work_status_escalates_when_records_were_dropped(self, tmp_path: Path):
        import asyncio
        import json

        server, _ = self._server_with_one_bad_file(tmp_path)
        result = asyncio.run(server.call_tool("work_status", {}))
        data = json.loads(result[0][0].text)
        assert data["load_errors"] == 1
        assert "verworfen" in data["recommendation"]

    def test_work_lint_reports_load_errors(self, tmp_path: Path):
        import asyncio
        import json

        server, _ = self._server_with_one_bad_file(tmp_path)
        result = asyncio.run(server.call_tool("work_lint", {}))
        data = json.loads(result[0][0].text)
        assert len(data["load_errors"]) == 1
        assert "bad.md" in data["load_errors"][0]

    def test_banner_shows_skipped_work_files(self, tmp_path: Path, capsys):
        from mcp_context_toolkit.engine import RulesEngine
        from mcp_context_toolkit.mcp_server import _print_banner

        _, work = self._server_with_one_bad_file(tmp_path)
        _print_banner(RulesEngine(), None, None, work.current())
        banner = capsys.readouterr().err
        work_line = next(line for line in banner.splitlines() if line.strip().startswith("Work"))
        assert "1 skipped (invalid)" in work_line
