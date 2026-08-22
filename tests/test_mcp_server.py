import os
from pathlib import Path

import pytest

from mcp_context_toolkit.domain import DomainEngine
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


class TestDiscoverAgentMemoryDir:
    def test_discovers_agent_memory(self, tmp_path: Path, monkeypatch):
        agent_mem = tmp_path / ".context" / "agent-memory"
        agent_mem.mkdir(parents=True)
        monkeypatch.chdir(tmp_path)
        from mcp_context_toolkit.mcp_server import _discover_agent_memory_dir
        found = _discover_agent_memory_dir()
        assert found == agent_mem




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
    `TalosDiscoveryAdapter` to import and drive, without depending on the
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
    gegen den echten Bestand. Der Checker hatte davor NULL Testabdeckung
    (WARNING-2 derselben Runde): alle `kanten_gebrochen`-Tests in
    `test_work.py` stubben den Checker, der echte `modul`-Zweig lief nie.

    Migriert (mct-Reifeplan Etappe 4, Host-Adapter-Protokoll): testet jetzt
    `TalosDiscoveryAdapter` aus `host_adapters.py` statt der vorherigen
    privaten `_kanten_check_factory` in `mcp_server.py`."""

    def test_modul_ohne_verzeichnis_ist_geplant_nicht_gebrochen(self, tmp_path: Path):
        from mcp_context_toolkit.host_adapters import TalosDiscoveryAdapter

        _write_fake_plan_graph(tmp_path)
        work_dir = tmp_path / ".claude" / "work"
        work_dir.mkdir(parents=True)
        adapter = TalosDiscoveryAdapter(work_dir)
        assert adapter.has_symbol_index()
        assert adapter.resolve_symbol("modules", "noch_nicht_gebaut") == ("geplant", None)

    def test_modul_mit_verzeichnis_ist_ok(self, tmp_path: Path):
        from mcp_context_toolkit.host_adapters import TalosDiscoveryAdapter

        _write_fake_plan_graph(tmp_path)
        (tmp_path / "modules" / "vorhanden").mkdir(parents=True)
        work_dir = tmp_path / ".claude" / "work"
        work_dir.mkdir(parents=True)
        adapter = TalosDiscoveryAdapter(work_dir)
        assert adapter.has_symbol_index()
        assert adapter.resolve_symbol("modules", "vorhanden") == ("ok", None)

    def test_nicht_modul_typen_delegieren_an_zustand(self, tmp_path: Path):
        from mcp_context_toolkit.host_adapters import TalosDiscoveryAdapter

        _write_fake_plan_graph(tmp_path)
        work_dir = tmp_path / ".claude" / "work"
        work_dir.mkdir(parents=True)
        adapter = TalosDiscoveryAdapter(work_dir)
        assert adapter.has_symbol_index()
        assert adapter.resolve_symbol("uses", "modules/real.py::vorhanden") == ("ok", None)
        assert adapter.resolve_symbol("uses", "modules/real.py::weg") == (
            "gebrochen", "Symbol existiert nirgends im Baum")

    def test_fehlendes_plan_graph_gibt_none_zurueck(self, tmp_path: Path):
        from mcp_context_toolkit.host_adapters import TalosDiscoveryAdapter

        work_dir = tmp_path / ".claude" / "work"
        work_dir.mkdir(parents=True)
        assert not TalosDiscoveryAdapter(work_dir).has_symbol_index()

    def test_kaputtes_plan_graph_degradiert_laut_auf_stderr(self, tmp_path: Path, capsys):
        # WARNING-3: ein ausdrueckliches check_symbols=True darf nicht still
        # im selben None wie der Default-Pfad landen.
        from mcp_context_toolkit.host_adapters import TalosDiscoveryAdapter

        scripts = tmp_path / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "plan_graph.py").write_text("raise RuntimeError('kaputt')\n", encoding="utf-8")
        work_dir = tmp_path / ".claude" / "work"
        work_dir.mkdir(parents=True)
        assert not TalosDiscoveryAdapter(work_dir).has_symbol_index()
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
        assert data["broken_reuse_refs"] == {}          # "geplant" ist gesund, kein Befund

    def test_work_lint_check_symbols_stays_none_without_a_symbol_index(self, tmp_path: Path):
        # W8-Reviewer-Fund WARNING-2 (mct-Reifeplan Etappe 4): der Guard, der
        # "nie geprueft" (None) von "geprueft, sauber" ({}) unterscheidet, war
        # ungetestet — ein Mutationstest (Guard entfernt) blieb gruen. KEIN
        # scripts/plan_graph.py hier, also KEIN Symbolindex verfuegbar.
        import asyncio
        import json

        from mcp_context_toolkit.mcp_server import build_server

        work_dir = tmp_path / ".claude" / "work"
        _write_work(work_dir, "w")
        rules = _Reloader(lambda: None, [])
        work = _Reloader(lambda: WorkEngine.from_directory(work_dir), [work_dir])
        server = build_server(rules, None, None, work, None, work_dir)

        result = asyncio.run(server.call_tool("work_lint", {"check_symbols": True}))
        data = json.loads(result[0][0].text)
        assert data["broken_reuse_refs"] is None


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


class TestDocsTool:
    """Fourth domain (Work-Record `doku_lesend_indexieren_statt_zerreissen`,
    Etappe F): docs/**/*.md stays documents, made searchable via docs_recall
    — citations (file + heading + line), never torn into cards."""

    def _write_doc(self, d: Path, rel: str, body: str) -> None:
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")

    def _server_with_docs(self, tmp_path: Path):
        from mcp_context_toolkit.docs import DocsEngine
        from mcp_context_toolkit.mcp_server import build_server

        self._write_doc(tmp_path, "guide.md", "# Datenbank-Migration\n\nSchema-Aenderungen hier.\n")
        rules = _Reloader(lambda: None, [])
        docs = _Reloader(lambda: DocsEngine.from_directory(tmp_path), [tmp_path])
        return build_server(rules, None, None, None, None, None, docs)

    def test_docs_recall_absent_without_a_docs_dir(self, tmp_path: Path):
        from mcp_context_toolkit.mcp_server import build_server

        rules = _Reloader(lambda: None, [])
        server = build_server(rules)
        inv = server.context_toolkit_inventory
        assert inv["docs"] == []

    def test_docs_recall_present_with_a_docs_dir(self, tmp_path: Path):
        server = self._server_with_docs(tmp_path)
        inv = server.context_toolkit_inventory
        assert inv["docs"] == ["docs_recall"]

    def test_docs_recall_returns_citation_via_tool_call(self, tmp_path: Path):
        import asyncio
        import json

        server = self._server_with_docs(tmp_path)
        result = asyncio.run(server.call_tool("docs_recall", {"query": "Datenbank Migration Schema"}))
        data = json.loads(result[0][0].text)
        assert data[0]["path"] == "guide.md"
        assert data[0]["heading"] == "Datenbank-Migration"
        assert data[0]["line"] == 1

    def test_docs_recall_truncates_long_section_text(self, tmp_path: Path):
        # W8-Reviewer-Fund ERROR-1, Etappe F: docs_recall used to return the
        # full section body — measured up to ~4,736 tokens for a normal
        # 8-hit call against the real corpus, contradicting the tool's own
        # docstring ("citation, not a full record").
        import asyncio
        import json

        long_text = "Suchbegriff. " + ("Fuelltext. " * 200)  # well over 400 chars
        self._write_doc(tmp_path, "big.md", f"# Grosser Abschnitt\n\n{long_text}\n")
        rules = _Reloader(lambda: None, [])
        from mcp_context_toolkit.docs import DocsEngine
        from mcp_context_toolkit.mcp_server import build_server

        docs = _Reloader(lambda: DocsEngine.from_directory(tmp_path), [tmp_path])
        server = build_server(rules, None, None, None, None, None, docs)
        result = asyncio.run(server.call_tool("docs_recall", {"query": "Suchbegriff"}))
        data = json.loads(result[0][0].text)
        assert len(data[0]["text"]) < len(long_text)
        assert data[0]["text"].endswith("…")

    def test_banner_reports_docs_when_present(self, tmp_path: Path, capsys):
        from mcp_context_toolkit.docs import DocsEngine
        from mcp_context_toolkit.engine import RulesEngine
        from mcp_context_toolkit.mcp_server import _print_banner

        self._write_doc(tmp_path, "a.md", "# A\n\nText.\n")
        docs_engine = DocsEngine.from_directory(tmp_path)
        _print_banner(RulesEngine(), None, None, None, docs_engine)
        banner = capsys.readouterr().err
        docs_line = next(line for line in banner.splitlines() if line.strip().startswith("Docs"))
        assert "1 file(s)" in docs_line

    def test_banner_shows_no_store_found_without_docs(self, capsys):
        from mcp_context_toolkit.engine import RulesEngine
        from mcp_context_toolkit.mcp_server import _print_banner

        _print_banner(RulesEngine(), None, None, None, None)
        banner = capsys.readouterr().err
        docs_line = next(line for line in banner.splitlines() if line.strip().startswith("Docs"))
        assert "no store found" in docs_line


class TestLimitClamping:
    """Etappe 7 of the public-readiness plan: every `limit:` parameter
    across the recall-style tools now runs through `core.clamp_limit`
    instead of a bare `[:limit]` slice — a negative value is otherwise a
    valid, reversed Python slice (docs_recall's original incident, see
    docs.py's recall() docstring), and an unclamped huge value can return
    most of a real corpus in one call. These tests exercise the actual
    MCP surface (real `call_tool`), not the engine methods in isolation —
    `test_memory.py`/`test_work.py`/`test_docs.py` already cover the
    unclamped-vs-clamped slice behavior at that layer; this layer proves
    the clamp survives the trip through the registered tool closures too."""

    def _server_with_all_domains(self, tmp_path: Path):
        from mcp_context_toolkit.docs import DocsEngine
        from mcp_context_toolkit.engine import RulesEngine
        from mcp_context_toolkit.mcp_server import build_server

        mem_dir = tmp_path / "memory" / "project"
        for i in range(3):
            _write_mem(mem_dir, f"mem{i}", body=f"searchword content {i}")
        work_dir = tmp_path / "work"
        for i in range(3):
            _write_work(work_dir, f"item{i}")
            (work_dir / f"item{i}.md").write_text(
                f"---\nkey: item{i}\ntitle: searchword item {i}\nkind: goal\n"
                f"status: open\ntheme: engine\nopened: 2026-07-01\n---\n\nx\n",
                encoding="utf-8",
            )
        docs_dir = tmp_path / "docs"
        docs_dir.mkdir(parents=True, exist_ok=True)
        for i in range(3):
            (docs_dir / f"d{i}.md").write_text(
                f"# Section {i}\n\nsearchword body {i}.\n", encoding="utf-8",
            )
        rules_dir = tmp_path / "rules"
        _write_rule(rules_dir, "r1")

        rules = _Reloader(lambda: RulesEngine.from_directory(rules_dir), [rules_dir])
        memory = _Reloader(lambda: MemoryEngine.from_directory(mem_dir), [mem_dir])
        work = _Reloader(lambda: WorkEngine.from_directory(work_dir), [work_dir])
        docs = _Reloader(lambda: DocsEngine.from_directory(docs_dir), [docs_dir])
        return build_server(rules, memory, None, work, None, None, docs)

    def _call(self, server, tool, args):
        import asyncio
        import json

        result = asyncio.run(server.call_tool(tool, args))
        return json.loads(result[0][0].text)

    def test_memory_recall_negative_limit_clamps_to_one(self, tmp_path: Path):
        server = self._server_with_all_domains(tmp_path)
        data = self._call(server, "recall", {"query": "searchword", "limit": -1})
        assert len(data) == 1

    def test_memory_recall_zero_limit_clamps_to_one(self, tmp_path: Path):
        server = self._server_with_all_domains(tmp_path)
        data = self._call(server, "recall", {"query": "searchword", "limit": 0})
        assert len(data) == 1

    def test_memory_recall_huge_limit_returns_at_most_the_corpus(self, tmp_path: Path):
        server = self._server_with_all_domains(tmp_path)
        data = self._call(server, "recall", {"query": "searchword", "limit": 999999})
        assert len(data) == 3  # bounded by the corpus, not the clamp ceiling

    def test_work_recall_negative_limit_clamps_to_one(self, tmp_path: Path):
        server = self._server_with_all_domains(tmp_path)
        data = self._call(server, "work_recall", {"query": "searchword", "limit": -1})
        assert len(data) == 1

    def test_docs_recall_negative_limit_clamps_to_one(self, tmp_path: Path):
        server = self._server_with_all_domains(tmp_path)
        data = self._call(server, "docs_recall", {"query": "searchword", "limit": -1})
        assert len(data) == 1

    def test_query_domain_negative_limit_clamps_to_one(self, tmp_path: Path):
        domains_dir = tmp_path / ".context" / "domains"
        _write_domain(domains_dir, "mem_view", """
id: mem_view
title: "Memory View"
profile: memory
source: memory
""")
        server = self._server_with_all_domains(tmp_path)
        # query_domain needs its own domain_reloader — rebuild with one attached.
        from mcp_context_toolkit.docs import DocsEngine
        from mcp_context_toolkit.domain import DomainEngine
        from mcp_context_toolkit.engine import RulesEngine
        from mcp_context_toolkit.mcp_server import build_server

        rules_dir = tmp_path / "rules"
        mem_dir = tmp_path / "memory" / "project"
        work_dir = tmp_path / "work"
        docs_dir = tmp_path / "docs"
        rules = _Reloader(lambda: RulesEngine.from_directory(rules_dir), [rules_dir])
        memory = _Reloader(lambda: MemoryEngine.from_directory(mem_dir), [mem_dir])
        work = _Reloader(lambda: WorkEngine.from_directory(work_dir), [work_dir])
        docs = _Reloader(lambda: DocsEngine.from_directory(docs_dir), [docs_dir])
        domains = _Reloader(lambda: DomainEngine.from_directory(domains_dir), [domains_dir])
        server = build_server(rules, memory, None, work, None, None, docs,
                              None, None, domains)
        data = self._call(server, "query_domain",
                          {"domain_id": "mem_view", "query": "searchword", "limit": -1})
        assert len(data) == 1

    def test_recall_invalid_limit_type_is_a_clean_mcp_error_not_a_crash(self, tmp_path: Path):
        # FastMCP validates `limit: int` against the tool's own schema before
        # the function body ever runs — this proves it, rather than assuming
        # it (Sol's proposal: "ungueltiger Typ -> stabiler MCP-Fehlercode").
        # It surfaces as a `ToolError` with a pydantic validation message
        # naming the offending field, not a raw traceback from application
        # code — exactly the "stable error", never reaching clamp_limit().
        import asyncio

        from mcp.server.fastmcp.exceptions import ToolError

        server = self._server_with_all_domains(tmp_path)
        with pytest.raises(ToolError, match="limit"):
            asyncio.run(
                server.call_tool("recall", {"query": "searchword", "limit": "acht"})
            )

    def test_memory_usage_negative_limit_clamps_to_one(self, tmp_path: Path):
        from mcp_context_toolkit.mcp_server import build_server
        from mcp_context_toolkit.usage import UsageStore

        usage = UsageStore(tmp_path / "_usage.json")
        for name in ("a", "b", "c"):
            usage.record_open(name)
        mem_dir = tmp_path / "memory"
        _write_mem(mem_dir, "a")
        rules = _Reloader(lambda: None, [])
        memory = _Reloader(lambda: MemoryEngine.from_directory(mem_dir), [mem_dir])
        server = build_server(rules, memory, usage)
        data = self._call(server, "memory_usage", {"limit": -1})
        # Unclamped, report()[:-1] on 3 rows drops only the last (2 survive);
        # clamped, [:1] returns exactly 1 — same distinguishing shape as the
        # recall() negative-limit tests above.
        assert len(data) == 1


class TestListWorkTopicOverview:
    """`list_work(include_archived=True)` with no theme/status/kind filter
    has no natural size bound at all — see list_work's docstring. Rather
    than truncate arbitrarily, that one call shape returns a per-theme
    topic overview instead of the raw item dump; every filtered call
    (a single theme, status, or kind is naturally small) is unaffected."""

    def _server(self, tmp_path: Path):
        from mcp_context_toolkit.mcp_server import build_server

        work_dir = tmp_path / "work"
        work_dir.mkdir(parents=True)
        items = [
            ("e1", "engine", "open"), ("e2", "engine", "open"),
            ("e3", "engine", "done"), ("c1", "chat", "dropped"),
        ]
        for key, theme, status in items:
            closed = "\nclosed: 2026-07-05" if status != "open" else ""
            (work_dir / f"{key}.md").write_text(
                f"---\nkey: {key}\ntitle: {key}\nkind: goal\nstatus: {status}\n"
                f"theme: {theme}\nopened: 2026-07-01{closed}\n---\n\nx\n",
                encoding="utf-8",
            )
        rules = _Reloader(lambda: None, [])
        work = _Reloader(lambda: WorkEngine.from_directory(work_dir), [work_dir])
        return build_server(rules, None, None, work)

    def _call(self, server, tool, args):
        import asyncio
        import json

        result = asyncio.run(server.call_tool(tool, args))
        return json.loads(result[0][0].text)

    def test_unfiltered_archived_listing_returns_topics_not_items(self, tmp_path: Path):
        server = self._server(tmp_path)
        data = self._call(server, "list_work", {"include_archived": True})
        assert "topics" in data and "hint" in data
        by_theme = {t["theme"]: t for t in data["topics"]}
        assert by_theme["engine"] == {
            "theme": "engine", "total": 3, "open": 2, "active": 0,
            "done": 1, "dropped": 0, "superseded": 0,
        }
        assert by_theme["chat"]["total"] == 1
        assert by_theme["chat"]["dropped"] == 1

    def test_filtered_archived_listing_still_returns_real_items(self, tmp_path: Path):
        server = self._server(tmp_path)
        data = self._call(server, "list_work",
                          {"include_archived": True, "theme": "engine"})
        assert isinstance(data, list)
        assert {row["key"] for row in data} == {"e1", "e2", "e3"}

    def test_plain_unfiltered_call_also_returns_topics_scoped_to_the_live_backlog(
        self, tmp_path: Path,
    ):
        # A plain list_work() (no include_archived) has no natural size
        # bound either — measured against the real store: ~91K tokens for
        # the live backlog alone. It gets the same topic-overview
        # treatment, but counts only open/active items (include_archived
        # defaults to False), never silently pulling in closed ones.
        server = self._server(tmp_path)
        data = self._call(server, "list_work", {})
        assert "topics" in data and "hint" in data
        by_theme = {t["theme"]: t for t in data["topics"]}
        # done/dropped/superseded are OMITTED (not reported as 0) in the
        # live-backlog case — reporting them at 0 would misread as "no
        # closed work exists" when it was just filtered out before counting.
        assert by_theme["engine"] == {"theme": "engine", "total": 2, "open": 2, "active": 0}
        assert "chat" not in by_theme  # its only item (c1) is dropped

    def test_filtered_call_without_include_archived_still_returns_real_items(
        self, tmp_path: Path,
    ):
        server = self._server(tmp_path)
        data = self._call(server, "list_work", {"theme": "engine"})
        assert isinstance(data, list)
        # Live-backlog default: closed items excluded even when filtered.
        assert {row["key"] for row in data} == {"e1", "e2"}

    def test_filtered_call_exceeding_the_response_budget_is_capped(
        self, tmp_path: Path,
    ):
        # A single FILTERED value is not a reliable size bound on a large
        # store — `status="done"` alone measured ~231K tokens on the real
        # TALOS work store, bigger than the unfiltered case this etappe
        # already capped. Every item-list response shares one budget
        # regardless of which filter produced it.
        import json

        from mcp_context_toolkit.mcp_server import build_server
        from mcp_context_toolkit.server.tools_work import _LIST_WORK_MAX_CHARS

        work_dir = tmp_path / "work"
        work_dir.mkdir(parents=True)
        # _work_summary() carries no body text — only `title` is long
        # enough to inflate a row, so that's what has to blow the budget.
        long_title = "Item " + "x" * 500
        for i in range(200):
            (work_dir / f"i{i}.md").write_text(
                f"---\nkey: i{i}\ntitle: '{long_title} {i}'\nkind: goal\n"
                f"status: open\ntheme: engine\nopened: 2026-07-01\n---\n\nx\n",
                encoding="utf-8",
            )
        rules = _Reloader(lambda: None, [])
        work = _Reloader(lambda: WorkEngine.from_directory(work_dir), [work_dir])
        server = build_server(rules, None, None, work)
        data = self._call(server, "list_work", {"theme": "engine"})
        assert data["truncated"] is True
        assert data["omitted"] > 0
        assert len(data["items"]) + data["omitted"] == 200
        assert "hint" in data
        # The cap is on the SERIALIZED response, not the item count.
        serialized = json.dumps(data, indent=2, ensure_ascii=False)
        assert len(serialized) <= _LIST_WORK_MAX_CHARS * 1.15  # shrink-step slack


def _write_domain(d: Path, domain_id: str, yaml_body: str) -> None:
    dom_dir = d / domain_id
    dom_dir.mkdir(parents=True, exist_ok=True)
    (dom_dir / "domain.yaml").write_text(yaml_body, encoding="utf-8")


def _write_rule(d: Path, key: str, *, rule_type: str = "workflow",
                priority: str = "mandatory") -> None:
    """Minimal schema-valid rule YAML — same shape as
    test_engine.py::_write_rule_yaml, with `type` parametrized too (needed
    to test domain filter.types)."""
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{key}.yaml").write_text(
        f"""key: {key}
title: "Rule {key}"
type: {rule_type}
scope: all
priority: {priority}
modules: [all]
applies_to:
  files:
    - "**/*"
summary: |
  Summary for {key} — at least ten characters.
content: |
  Body for {key} mentioning searchword.
created: 2026-07-08
""",
        encoding="utf-8",
    )


class TestDomainTools:
    """`list_domains`/`get_domain`/`query_domain` end to end via real
    `call_tool` calls —
    the domain mechanism was previously an unwired library (`domain.py`
    existed, nothing ever imported it); these tests exercise the actual
    MCP surface, not just DomainEngine in isolation (test_domain.py
    already covers that layer)."""

    def _server_with_rules_view(self, tmp_path: Path):
        from mcp_context_toolkit.engine import RulesEngine
        from mcp_context_toolkit.mcp_server import build_server

        rules_dir = tmp_path / ".context" / "rules"
        _write_rule(rules_dir, "sec1", rule_type="security", priority="non_negotiable")
        _write_rule(rules_dir, "sec2", rule_type="security", priority="recommended")
        _write_rule(rules_dir, "wf1", rule_type="workflow", priority="mandatory")

        domains_dir = tmp_path / ".context" / "domains"
        _write_domain(domains_dir, "critical_security", """
id: critical_security
title: "Critical Security Rules"
profile: rules
source: rules
filter:
  types: [security]
  priorities: [non_negotiable]
""")

        rules = _Reloader(lambda: RulesEngine.from_directory(rules_dir), [rules_dir])
        domains = _Reloader(lambda: DomainEngine.from_directory(domains_dir), [domains_dir])
        return build_server(rules, domain_reloader=domains)

    def _server_with_work_view(self, tmp_path: Path):
        from mcp_context_toolkit.mcp_server import build_server

        work_dir = tmp_path / ".context" / "work"
        _write_work(work_dir, "infra_item", status="open")
        (work_dir / "infra_item.md").write_text(
            "---\nkey: infra_item\ntitle: Infra searchword item\nkind: goal\n"
            "status: open\ntheme: infra\nopened: 2026-07-01\n---\n\nx\n",
            encoding="utf-8",
        )
        (work_dir / "chat_item.md").write_text(
            "---\nkey: chat_item\ntitle: Chat searchword item\nkind: goal\n"
            "status: open\ntheme: chat\nopened: 2026-07-01\n---\n\nx\n",
            encoding="utf-8",
        )

        domains_dir = tmp_path / ".context" / "domains"
        _write_domain(domains_dir, "infra_only", """
id: infra_only
title: "Infra Work Items"
profile: work
source: work
filter:
  themes: [infra]
""")

        rules = _Reloader(lambda: None, [])
        work = _Reloader(lambda: WorkEngine.from_directory(work_dir), [work_dir])
        domains = _Reloader(lambda: DomainEngine.from_directory(domains_dir), [domains_dir])
        return build_server(rules, work_reloader=work, domain_reloader=domains)

    def test_list_domains_absent_without_a_domains_dir(self):
        from mcp_context_toolkit.mcp_server import build_server

        rules = _Reloader(lambda: None, [])
        server = build_server(rules)
        assert server.context_toolkit_inventory["domain"] == []

    def test_list_domains_reports_configured_domains(self, tmp_path: Path):
        import asyncio
        import json

        server = self._server_with_rules_view(tmp_path)
        result = asyncio.run(server.call_tool("list_domains", {}))
        data = json.loads(result[0][0].text)
        assert data == [{
            "id": "critical_security", "title": "Critical Security Rules",
            "profile": "rules", "source": "rules", "is_view": True,
            "keywords": [], "paths": [],
        }]

    def test_get_domain_unknown_id_returns_error(self, tmp_path: Path):
        import asyncio
        import json

        server = self._server_with_rules_view(tmp_path)
        result = asyncio.run(server.call_tool("get_domain", {"domain_id": "does_not_exist"}))
        data = json.loads(result[0][0].text)
        assert "error" in data

    def test_get_domain_known_id_returns_full_manifest(self, tmp_path: Path):
        import asyncio
        import json

        server = self._server_with_rules_view(tmp_path)
        result = asyncio.run(server.call_tool("get_domain", {"domain_id": "critical_security"}))
        data = json.loads(result[0][0].text)
        assert data["filter"]["types"] == ["security"]
        assert data["filter"]["priorities"] == ["non_negotiable"]

    def test_query_domain_rules_view_applies_filter_before_ranking(self, tmp_path: Path):
        # 3 rules exist; only sec1 is security+non_negotiable — sec2 (security
        # but recommended) and wf1 (workflow) must NOT appear even though all
        # three mention "searchword" in their content.
        import asyncio
        import json

        server = self._server_with_rules_view(tmp_path)
        result = asyncio.run(server.call_tool(
            "query_domain", {"domain_id": "critical_security", "query": "searchword"}))
        data = json.loads(result[0][0].text)
        assert [r["key"] for r in data] == ["sec1"]

    def test_query_domain_work_view_applies_theme_filter(self, tmp_path: Path):
        import asyncio
        import json

        server = self._server_with_work_view(tmp_path)
        result = asyncio.run(server.call_tool(
            "query_domain", {"domain_id": "infra_only", "query": "searchword"}))
        data = json.loads(result[0][0].text)
        assert [r["key"] for r in data] == ["infra_item"]

    def test_query_domain_unknown_id_returns_error(self, tmp_path: Path):
        import asyncio
        import json

        server = self._server_with_rules_view(tmp_path)
        result = asyncio.run(server.call_tool(
            "query_domain", {"domain_id": "does_not_exist", "query": "x"}))
        data = json.loads(result[0][0].text)
        assert "error" in data

    def test_query_domain_rules_unfiltered_view_is_rejected(self, tmp_path: Path):
        # WARNING-1: rules have no free-text body worth ranking on their
        # own without narrowing — the docstring promises this error.
        import asyncio
        import json

        from mcp_context_toolkit.engine import RulesEngine
        from mcp_context_toolkit.mcp_server import build_server

        rules_dir = tmp_path / ".context" / "rules"
        _write_rule(rules_dir, "sec1", rule_type="security", priority="non_negotiable")
        domains_dir = tmp_path / ".context" / "domains"
        _write_domain(domains_dir, "all_rules", """
id: all_rules
title: "All Rules, No Filter"
profile: rules
source: rules
""")
        rules = _Reloader(lambda: RulesEngine.from_directory(rules_dir), [rules_dir])
        domains = _Reloader(lambda: DomainEngine.from_directory(domains_dir), [domains_dir])
        server = build_server(rules, domain_reloader=domains)
        result = asyncio.run(server.call_tool(
            "query_domain", {"domain_id": "all_rules", "query": "x"}))
        data = json.loads(result[0][0].text)
        assert "error" in data

    def test_query_domain_physical_rules_domain_uses_its_own_store(self, tmp_path: Path):
        # ERROR-2 (mct-Reifeplan Etappe 3 W8-review): a physical rules domain
        # (root set, no source) used to silently return the SERVER's rules
        # instead of its own — `is_view` was computed but never checked in
        # the rules branch.
        import asyncio
        import json

        from mcp_context_toolkit.engine import RulesEngine
        from mcp_context_toolkit.mcp_server import build_server

        server_rules_dir = tmp_path / "server" / ".context" / "rules"
        _write_rule(server_rules_dir, "server_only", rule_type="security", priority="mandatory")

        domains_dir = tmp_path / "server" / ".context" / "domains"
        own_rules_dir = domains_dir / "own_security" / "data"
        _write_rule(own_rules_dir, "own_only", rule_type="security", priority="non_negotiable")
        _write_domain(domains_dir, "own_security", """
id: own_security
title: "Own Physical Rules Store"
profile: rules
root: ./data
filter:
  types: [security]
""")
        rules = _Reloader(lambda: RulesEngine.from_directory(server_rules_dir), [server_rules_dir])
        domains = _Reloader(lambda: DomainEngine.from_directory(domains_dir), [domains_dir])
        server = build_server(rules, domain_reloader=domains)
        result = asyncio.run(server.call_tool(
            "query_domain", {"domain_id": "own_security", "query": "rule"}))
        data = json.loads(result[0][0].text)
        assert [r["key"] for r in data] == ["own_only"]

    def test_query_domain_physical_root_cannot_escape_its_domain_directory(self, tmp_path: Path):
        # WARNING-3: a domain.yaml's `root:` must not resolve outside its
        # own domain directory — path traversal via `../..` or an absolute
        # path.
        import asyncio
        import json

        from mcp_context_toolkit.mcp_server import build_server

        secret_dir = tmp_path / "secret"
        _write_mem(secret_dir, "leaked", body="should never be readable via a domain")

        domains_dir = tmp_path / ".context" / "domains"
        _write_domain(domains_dir, "escape_attempt", f"""
id: escape_attempt
title: "Escape Attempt"
profile: memory
root: {os.path.relpath(secret_dir, domains_dir / "escape_attempt")}
""")
        rules = _Reloader(lambda: None, [])
        domains = _Reloader(lambda: DomainEngine.from_directory(domains_dir), [domains_dir])
        server = build_server(rules, domain_reloader=domains)
        result = asyncio.run(server.call_tool(
            "query_domain", {"domain_id": "escape_attempt", "query": "leaked"}))
        data = json.loads(result[0][0].text)
        assert "error" in data

    def test_query_domain_physical_memory_domain_loads_its_own_directory(self, tmp_path: Path):
        import asyncio
        import json

        from mcp_context_toolkit.mcp_server import build_server

        domains_dir = tmp_path / ".context" / "domains"
        data_dir = domains_dir / "honey_bunny" / "data"
        _write_mem(data_dir, "winter_storage", body="Bunnies store honey for winter searchword.")
        _write_domain(domains_dir, "honey_bunny", """
id: honey_bunny
title: "Honey Bunny Notes"
profile: memory
root: ./data
""")
        rules = _Reloader(lambda: None, [])
        domains = _Reloader(lambda: DomainEngine.from_directory(domains_dir), [domains_dir])
        server = build_server(rules, domain_reloader=domains)
        result = asyncio.run(server.call_tool(
            "query_domain", {"domain_id": "honey_bunny", "query": "searchword"}))
        data = json.loads(result[0][0].text)
        assert [d["name"] for d in data] == ["winter_storage"]


class TestBausteinFunctionDocTools:
    """Fifth domain (Work-Record `mcp_baustein_funktions_doku_domaene_
    konvergiertes_design`), deliberately SEPARATE from docs_recall above:
    exact-key lookup over a purpose entry, not ranked search over prose."""

    def _write_baustein_docs(self, tmp_path: Path, entries: dict) -> Path:
        p = tmp_path / "baustein-docs.json"
        p.write_text(__import__("json").dumps(entries), encoding="utf-8")
        return p

    def _write_function_docs(self, tmp_path: Path, entries: dict) -> Path:
        p = tmp_path / "function-docs.json"
        p.write_text(__import__("json").dumps(entries), encoding="utf-8")
        return p

    def test_json_doc_store_returns_none_for_missing_key(self, tmp_path: Path):
        from mcp_context_toolkit.mcp_server import _JsonDocStore

        p = self._write_baustein_docs(tmp_path, {"py:a": {"zweck": "x"}})
        store = _JsonDocStore(p)
        assert store.get("py:does_not_exist") is None

    def test_json_doc_store_returns_entry_for_known_key(self, tmp_path: Path):
        from mcp_context_toolkit.mcp_server import _JsonDocStore

        p = self._write_baustein_docs(tmp_path, {"py:a": {"zweck": "x"}})
        store = _JsonDocStore(p)
        assert store.get("py:a") == {"zweck": "x"}

    def test_json_doc_store_missing_file_returns_none_not_crash(self, tmp_path: Path):
        from mcp_context_toolkit.mcp_server import _JsonDocStore

        store = _JsonDocStore(tmp_path / "does-not-exist.json")
        assert store.get("anything") is None

    def test_json_doc_store_malformed_json_returns_none_not_crash(self, tmp_path: Path):
        from mcp_context_toolkit.mcp_server import _JsonDocStore

        p = tmp_path / "broken.json"
        p.write_text("{not valid json", encoding="utf-8")
        store = _JsonDocStore(p)
        assert store.get("anything") is None

    def test_get_baustein_doc_absent_without_a_file(self, tmp_path: Path):
        from mcp_context_toolkit.mcp_server import build_server

        rules = _Reloader(lambda: None, [])
        server = build_server(rules)
        inv = server.context_toolkit_inventory
        assert inv["baustein"] == []

    def test_get_baustein_doc_end_to_end_via_tool_call(self, tmp_path: Path, monkeypatch):
        import asyncio
        import json

        from mcp_context_toolkit.mcp_server import build_server

        # Synthetischer site_kit.py-Resolver für autarken Testlauf ohne TALOS-Checkout
        (tmp_path / ".context").mkdir(parents=True, exist_ok=True)
        site_kit_dir = tmp_path / "tools" / "code-graph"
        site_kit_dir.mkdir(parents=True, exist_ok=True)
        (site_kit_dir / "site_kit.py").write_text(
            "def path_to_id(p: str):\n"
            "    return 'py:a' if 'a.py' in p else None\n",
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)

        baustein_file = self._write_baustein_docs(
            tmp_path, {"py:a": {"zweck": "Testzweck fuer Modul a"}})
        rules = _Reloader(lambda: None, [])
        server = build_server(rules, None, None, None, None, None, None, baustein_file)
        inv = server.context_toolkit_inventory
        assert inv["baustein"] == ["get_baustein_doc"]

        result = asyncio.run(server.call_tool("get_baustein_doc", {"pfad": "backend/app/a.py"}))
        data = json.loads(result[0][0].text)
        assert data == {"zweck": "Testzweck fuer Modul a"}

    def test_get_function_doc_absent_without_a_file(self, tmp_path: Path):
        from mcp_context_toolkit.mcp_server import build_server

        rules = _Reloader(lambda: None, [])
        server = build_server(rules)
        inv = server.context_toolkit_inventory
        assert inv["baustein"] == []

    def test_get_function_doc_end_to_end_via_tool_call(self, tmp_path: Path):
        import asyncio
        import json

        from mcp_context_toolkit.mcp_server import build_server

        anchor = "backend/app/a.py::Foo.bar"
        function_file = self._write_function_docs(
            tmp_path, {anchor: {"zweck": "Kuerzt Foo.bar in einem Satz."}})
        rules = _Reloader(lambda: None, [])
        server = build_server(rules, None, None, None, None, None, None,
                              None, function_file)
        inv = server.context_toolkit_inventory
        assert inv["baustein"] == ["get_function_doc"]

        result = asyncio.run(server.call_tool("get_function_doc", {"anchor": anchor}))
        data = json.loads(result[0][0].text)
        assert data == {"zweck": "Kuerzt Foo.bar in einem Satz."}

    def test_get_function_doc_unknown_anchor_returns_empty(self, tmp_path: Path):
        import asyncio
        import json

        from mcp_context_toolkit.mcp_server import build_server

        function_file = self._write_function_docs(tmp_path, {})
        rules = _Reloader(lambda: None, [])
        server = build_server(rules, None, None, None, None, None, None,
                              None, function_file)
        result = asyncio.run(server.call_tool("get_function_doc", {"anchor": "x.py::y"}))
        assert json.loads(result[0][0].text) == {}

    def test_both_baustein_and_function_tool_register_together(self, tmp_path: Path):
        from mcp_context_toolkit.mcp_server import build_server

        baustein_file = self._write_baustein_docs(tmp_path, {})
        function_file = self._write_function_docs(tmp_path, {})
        rules = _Reloader(lambda: None, [])
        server = build_server(rules, None, None, None, None, None, None,
                              baustein_file, function_file)
        inv = server.context_toolkit_inventory
        assert set(inv["baustein"]) == {"get_baustein_doc", "get_function_doc"}


class TestPathToIdFactory:
    """Contract verification of TalosDiscoveryAdapter.path_to_id (site_kit.py
    file-import). Migrated (mct-Reifeplan Etappe 4) from the previous
    private `_path_to_id_factory` in mcp_server.py to `host_adapters.py`."""

    def test_synthetic_path_to_id_factory(self, tmp_path: Path, monkeypatch):
        from mcp_context_toolkit.host_adapters import TalosDiscoveryAdapter

        (tmp_path / ".context").mkdir(parents=True, exist_ok=True)
        site_kit_dir = tmp_path / "tools" / "code-graph"
        site_kit_dir.mkdir(parents=True, exist_ok=True)
        (site_kit_dir / "site_kit.py").write_text(
            "def path_to_id(p: str):\n"
            "    return 'py:a' if 'a.py' in p else None\n",
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)

        adapter = TalosDiscoveryAdapter()
        assert adapter.path_to_id("backend/app/a.py") == "py:a"
        assert adapter.path_to_id("backend/app/b.py") is None
