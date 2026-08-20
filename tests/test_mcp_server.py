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


def _write_fake_plan_graph(tmp_path: Path) -> None:
    """Minimal stand-in for `scripts/plan_graph.py` — just enough surface
    (`SCAN_WURZELN`/`bestand`/`nach_namen`/`_zustand`/`_modul_vorhanden`) for
    `_kanten_check_factory` to import and drive, without depending on the
    real repo's script or its ~1.7s tree scan."""
    scripts = tmp_path / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / "plan_graph.py").write_text(
        '''
SCAN_WURZELN = ("modules",)

def bestand(wurzeln, projekt_dir="."):
    return {"modules/real.py": [{"name": "vorhanden"}]}

def nach_namen(best):
    return {"vorhanden": [{"pfad": "modules/real.py"}]}

def _zustand(typ, ziel, best, namen):
    pfad, _, symbol = ziel.partition("::")
    if any(s["name"] == symbol for s in best.get(pfad, ())):
        return "ok", None
    return "gebrochen", "Symbol existiert nirgends im Baum"

def _modul_vorhanden(name, projekt):
    import os
    return os.path.isdir(os.path.join(projekt, "modules", name))
''',
        encoding="utf-8",
    )


class TestKantenCheckFactory:
    """W8-Reviewer-Fund ERROR-1 (Etappe E): der `modul`-Zweig meldete jedes
    noch nicht angelegte Modul-Verzeichnis als "gebrochen" statt "geplant"
    (der Normalzustand vor dem Bau, wie bei `baut`) — 17/17 Falschmeldungen
    gegen den echten Bestand. `_kanten_check_factory` hatte davor NULL
    Testabdeckung (WARNING-2 derselben Runde): alle `kanten_gebrochen`-Tests
    in `test_work.py` stubben den Checker, der echte `modul`-Zweig lief nie."""

    def test_modul_ohne_verzeichnis_ist_geplant_nicht_gebrochen(self, tmp_path: Path):
        from mcp_context_toolkit.mcp_server import _kanten_check_factory

        _write_fake_plan_graph(tmp_path)
        work_dir = tmp_path / ".claude" / "work"
        work_dir.mkdir(parents=True)
        check = _kanten_check_factory(work_dir)
        assert check is not None
        assert check("modul", "noch_nicht_gebaut") == ("geplant", None)

    def test_modul_mit_verzeichnis_ist_ok(self, tmp_path: Path):
        from mcp_context_toolkit.mcp_server import _kanten_check_factory

        _write_fake_plan_graph(tmp_path)
        (tmp_path / "modules" / "vorhanden").mkdir(parents=True)
        work_dir = tmp_path / ".claude" / "work"
        work_dir.mkdir(parents=True)
        check = _kanten_check_factory(work_dir)
        assert check is not None
        assert check("modul", "vorhanden") == ("ok", None)

    def test_nicht_modul_typen_delegieren_an_zustand(self, tmp_path: Path):
        from mcp_context_toolkit.mcp_server import _kanten_check_factory

        _write_fake_plan_graph(tmp_path)
        work_dir = tmp_path / ".claude" / "work"
        work_dir.mkdir(parents=True)
        check = _kanten_check_factory(work_dir)
        assert check is not None
        assert check("nutzt", "modules/real.py::vorhanden") == ("ok", None)
        assert check("nutzt", "modules/real.py::weg") == (
            "gebrochen", "Symbol existiert nirgends im Baum")

    def test_fehlendes_plan_graph_gibt_none_zurueck(self, tmp_path: Path):
        from mcp_context_toolkit.mcp_server import _kanten_check_factory

        work_dir = tmp_path / ".claude" / "work"
        work_dir.mkdir(parents=True)
        assert _kanten_check_factory(work_dir) is None

    def test_kaputtes_plan_graph_degradiert_laut_auf_stderr(self, tmp_path: Path, capsys):
        # WARNING-3: ein ausdrueckliches check_symbols=True darf nicht still
        # im selben None wie der Default-Pfad landen.
        from mcp_context_toolkit.mcp_server import _kanten_check_factory

        scripts = tmp_path / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "plan_graph.py").write_text("raise RuntimeError('kaputt')\n", encoding="utf-8")
        work_dir = tmp_path / ".claude" / "work"
        work_dir.mkdir(parents=True)
        assert _kanten_check_factory(work_dir) is None
        assert "kaputt" in capsys.readouterr().err

    def test_work_lint_check_symbols_end_to_end_ueber_den_tool_aufruf(self, tmp_path: Path):
        import asyncio
        import json

        from mcp_context_toolkit.mcp_server import build_server

        _write_fake_plan_graph(tmp_path)
        work_dir = tmp_path / ".claude" / "work"
        _write_work(work_dir, "w")
        (work_dir / "w.md").write_text(
            "---\nkey: w\ntitle: w\nkind: goal\nstatus: open\ntheme: engine\n"
            "opened: 2026-07-01\nkanten:\n  gesucht:\n    - x\n"
            "  modul:\n    - noch_nicht_gebaut\n---\n\nx\n",
            encoding="utf-8",
        )
        rules = _Reloader(lambda: None, [])
        work = _Reloader(lambda: WorkEngine.from_directory(work_dir), [work_dir])
        server = build_server(rules, None, None, work, None, work_dir)

        result = asyncio.run(server.call_tool("work_lint", {"check_symbols": True}))
        data = json.loads(result[0][0].text)
        assert data["kanten_gebrochen"] == {}          # "geplant" ist gesund, kein Befund


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
