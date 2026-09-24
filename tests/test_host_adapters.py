from pathlib import Path

from mcp_context_toolkit.host_adapters import (
    DemoAdapter,
    HostAdapter,
    TalosDiscoveryAdapter,
    discover_host_adapter,
)


class TestHostAdapterProtocol:
    """The formal contract (mct-Reifeplan Etappe 4): a host implements
    `HostAdapter`, the core calls it through the Protocol, never a specific
    host's code. `DemoAdapter` is the proof that a SECOND host needs
    nothing TALOS-specific to satisfy it — no `scripts/plan_graph.py`, no
    `tools/code-graph/site_kit.py`, just its own in-memory index."""

    def test_demo_adapter_satisfies_the_protocol(self):
        assert isinstance(DemoAdapter(), HostAdapter)

    def test_talos_discovery_adapter_satisfies_the_protocol(self):
        assert isinstance(TalosDiscoveryAdapter(), HostAdapter)

    def test_demo_adapter_resolves_known_symbols(self):
        adapter = DemoAdapter(known_symbols={"pkg/mod.py::Thing": "x"})
        assert adapter.resolve_symbol("uses", "pkg/mod.py::Thing") == ("ok", None)

    def test_demo_adapter_reports_unknown_symbols_as_broken(self):
        adapter = DemoAdapter()
        state, hint = adapter.resolve_symbol("uses", "pkg/mod.py::Missing")
        assert state == "gebrochen"
        assert "Missing" in hint

    def test_demo_adapter_path_to_id_generic_rule(self):
        adapter = DemoAdapter()
        assert adapter.path_to_id("pkg/mod.py") == "py:pkg.mod"
        assert adapter.path_to_id("README.md") is None

    def test_discover_host_adapter_never_none(self, tmp_path: Path):
        # Outside a TALOS checkout there is nothing to resolve, but the
        # adapter object itself is always returned — "no symbol index" and
        # "no adapter installed" read the same way to a caller (see
        # `has_symbol_index`), there is no third state.
        adapter = discover_host_adapter(tmp_path)
        assert adapter is not None
        assert isinstance(adapter, HostAdapter)
        assert not adapter.has_symbol_index()
        assert adapter.path_to_id("anything.py") is None

    def test_kanten_check_is_memoized_not_rebuilt_per_call(self, tmp_path: Path):
        # A real symbol-tree scan is expensive (~1.7s measured on the real
        # repo) — resolve_symbol()/has_symbol_index() must reuse ONE
        # built checker per adapter instance, not rebuild it per anchor.
        scripts = tmp_path / "scripts"
        scripts.mkdir(parents=True)
        marker = tmp_path / "build_marker.txt"
        (scripts / "plan_graph.py").write_text(
            "SCAN_WURZELN = ()\n"
            "def bestand(w, p='.'):\n"
            f"    import pathlib; pathlib.Path({str(marker)!r}).write_text('x')\n"
            "    return {}\n"
            "def nach_namen(b):\n"
            "    return {}\n"
            "def _zustand(t, z, b, n):\n"
            "    return 'ok', None\n"
            "def _modul_vorhanden(n, p):\n"
            "    return True\n",
            encoding="utf-8",
        )
        work_dir = tmp_path / ".claude" / "work"
        work_dir.mkdir(parents=True)
        adapter = TalosDiscoveryAdapter(work_dir)
        adapter.resolve_symbol("uses", "a::b")
        assert marker.is_file()
        marker.unlink()
        adapter.resolve_symbol("uses", "c::d")
        adapter.has_symbol_index()
        # If `bestand()` ran again, the marker file would have reappeared.
        assert not marker.is_file()

    def _stub_plan_graph(self, tmp_path: Path) -> None:
        """A fake `scripts/plan_graph.py` whose `_zustand` echoes back the
        `typ` it actually received — isolates the adapter's own type
        translation from the real plan_graph.py's behavior."""
        scripts = tmp_path / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "plan_graph.py").write_text(
            "SCAN_WURZELN = ()\n"
            "TYP_BAUT = 'baut'\n"
            "def bestand(w, p='.'):\n"
            "    return {}\n"
            "def nach_namen(b):\n"
            "    return {}\n"
            "def _zustand(t, z, b, n):\n"
            "    if t == TYP_BAUT:\n"
            "        return 'geplant', None\n"
            "    return 'gebrochen', f'typ war {t!r}'\n"
            "def _modul_vorhanden(n, p):\n"
            "    return True\n",
            encoding="utf-8",
        )

    def test_creates_wird_vor_zustand_auf_baut_uebersetzt(self, tmp_path: Path):
        # WorkItem.reuse traegt "creates" (englisch), plan_graph.py kennt nur
        # "baut" (deutsch) fuer die geplant/gebrochen-Weiche. Ohne Uebersetzung
        # sah jedes noch nicht gebaute `creates:`-Ziel wie eine gebrochene
        # Kante aus (Reviewer-Fund 2026-09-05, `host_adapters.py::symbol_check`
        # gab den rohen englischen Typ unveraendert an `_zustand()` durch).
        self._stub_plan_graph(tmp_path)
        work_dir = tmp_path / ".claude" / "work"
        work_dir.mkdir(parents=True)
        adapter = TalosDiscoveryAdapter(work_dir)
        assert adapter.resolve_symbol("creates", "x::NochNichtGebaut") == ("geplant", None)

    def test_uses_und_extends_bleiben_unuebersetzt(self, tmp_path: Path):
        # Der Stub-`_zustand` echot den erhaltenen Typ zurueck, wenn er NICHT
        # "baut" ist — belegt, dass nur "creates" uebersetzt wird, "uses"/
        # "extends" unveraendert ankommen (dieselbe Grenze wie vor dem Fix).
        self._stub_plan_graph(tmp_path)
        work_dir = tmp_path / ".claude" / "work"
        work_dir.mkdir(parents=True)
        adapter = TalosDiscoveryAdapter(work_dir)
        for typ in ("uses", "extends"):
            state, hint = adapter.resolve_symbol(typ, "x::y")
            assert state == "gebrochen"
            assert hint == f"typ war {typ!r}"
