"""Host adapters — the one place this engine is allowed to know a specific
host exists, and even then only through a Protocol, never an import.

Rules/memory/work/docs stay host-agnostic by construction (plain files under
`.context/`). Two things in this package are NOT: resolving whether a
`reuse`/`kanten` anchor (`path/to/file.py::Symbol`) actually exists in the
project's symbol tree, and mapping a file path to the key a project's own
documentation JSON uses. Both need a symbol index or a path convention that
lives in the HOST project, not in a generic package that ships to strangers.

`HostAdapter` is the formal contract for that: a host implements it, this
package calls it through the Protocol, and never imports the host's code
directly (the built-in `TalosDiscoveryAdapter` below loads TALOS's resolver
scripts by FILE PATH via `importlib`, exactly like a plugin loader would —
not `from tools.code_graph import site_kit`). A second host can implement
the same Protocol without touching a single TALOS file; see `DemoAdapter`
for the minimal shape that takes.

`TalosDiscoveryAdapter` ships in the core package rather than moving out to
`examples/host-profiles/talos/` — a real, remaining gap (see the plan's
"known gaps" list) — because splitting it out requires also changing how a
running TALOS session configures the server (`.mcp.json` / env), which is
riskier than the Protocol extraction this stage is about. What already
moved: it is a formal Protocol implementation now, not a private function
`mcp_server.py` happens to know the name of, and the test that exercises it
imports the Protocol path, not a `_`-prefixed function.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Protocol, runtime_checkable

from mcp_context_toolkit.core import store_conventions


@runtime_checkable
class HostAdapter(Protocol):
    """What a host MAY provide. `resolve_symbol`/`path_to_id` are optional to
    actually resolve anything — a host that can't answer returns `None`/the
    "not checked" state, it never raises. `discover_host_adapter()` always
    returns a real adapter object (never `None` itself); a partial adapter
    (implements one capability meaningfully, degrades the other) is equally
    valid.

    `has_symbol_index()` is part of the Protocol on purpose, not left as a
    TALOS-only convenience method: a caller building a lint `symbol_check`
    needs to know whether an adapter has anything to check at all BEFORE
    calling it, for any adapter — passing `resolve_symbol` in when there is
    no real index would turn "never checked" into a falsely-clean "checked,
    found nothing" (`WorkEngine.lint()`'s own docstring calls this
    distinction load-bearing). An adapter with no symbol index at all
    returns `False` here rather than making every `resolve_symbol` call
    silently report `"ok"`."""

    def has_symbol_index(self) -> bool:
        """Does this adapter have a real symbol index to check `reuse`
        anchors against right now? `False` means "do not call
        `resolve_symbol` as a lint checker" — there is nothing behind it,
        and the caller should treat symbol-checking as not run at all."""
        ...

    def resolve_symbol(self, kind: str, target: str) -> tuple[str, str | None]:
        """Does a `reuse` anchor (`kind` is `uses`/`extends`/`creates`/
        `modules`, `target` is `path/to/file.py::Symbol` or a module name)
        still resolve in the project's real symbol tree? Only meaningful
        when `has_symbol_index()` is `True`. Returns `(state, hint)` —
        `state` one of `"ok"` (resolves), `"geplant"` (module anchor,
        directory not built yet — the normal pre-build state, not a
        break), or anything else (treated as broken, `hint` explains why).
        Used by `WorkEngine.lint(symbol_check=...)`."""
        ...

    def path_to_id(self, path: str) -> str | None:
        """Map a repo-relative file path to the key a project's own
        documentation JSON (`get_baustein_doc`/`get_function_doc`) indexes
        by. `None` if the host has no such mapping or the path is
        unresolvable."""
        ...


def _find_project_root() -> Path | None:
    """Walk up from cwd for a directory carrying a `<convention>/` marker
    (`.claude`/`.context`)."""
    for candidate in [Path.cwd(), *Path.cwd().parents]:
        for conv in store_conventions():
            if (candidate / conv).is_dir():
                return candidate
    return None


class TalosDiscoveryAdapter:
    """The built-in adapter for a TALOS checkout. Discovers TALOS's own
    resolver scripts (`scripts/plan_graph.py`, `tools/code-graph/site_kit.py`)
    by FILE PATH, loads them via `importlib.util.spec_from_file_location` —
    never a package import — and degrades to "not available" for either
    method independently if its script is missing. Outside a TALOS checkout
    both methods simply return the "nothing to resolve" state; nothing here
    requires TALOS to be present."""

    _UNSET = object()

    def __init__(self, work_dir: Path | None = None) -> None:
        self._work_dir = work_dir
        # Both closures do real work to build (tree scan / module import) —
        # memoized per adapter instance, computed at most once no matter how
        # many anchors/paths get resolved through it. `_UNSET` vs. `None` are
        # distinct: `None` is a cached "not available" answer, `_UNSET` means
        # "haven't tried yet".
        self._kanten_check_cache = self._UNSET
        self._path_to_id_cache = self._UNSET

    def has_symbol_index(self) -> bool:
        return self._kanten_check() is not None

    def resolve_symbol(self, kind: str, target: str) -> tuple[str, str | None]:
        check = self._kanten_check()
        if check is None:
            return "ok", None  # no symbol index available — never claim "broken"
        return check(kind, target)

    def path_to_id(self, path: str) -> str | None:
        resolver = self._path_to_id_fn()
        return resolver(path) if resolver is not None else None

    def _kanten_check(self):
        if self._kanten_check_cache is not self._UNSET:
            return self._kanten_check_cache
        self._kanten_check_cache = self._build_kanten_check()
        return self._kanten_check_cache

    def _build_kanten_check(self):
        if self._work_dir is None:
            return None
        projekt = self._work_dir.resolve().parent.parent
        plan_graph_pfad = projekt / "scripts" / "plan_graph.py"
        if not plan_graph_pfad.is_file():
            return None
        spec = importlib.util.spec_from_file_location("_context_plan_graph", plan_graph_pfad)
        if spec is None or spec.loader is None:
            return None
        modul = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(modul)
            best = modul.bestand(modul.SCAN_WURZELN, str(projekt))
            namen = modul.nach_namen(best)
        except Exception as e:
            # Degrade loud (W4-class): unlike a missing plan_graph.py (the
            # normal case outside TALOS), this is a break in EXISTING code —
            # an explicit check_symbols=True call should not land silently in
            # the same "nothing to check" state as the cheap default path.
            print(f"[context-toolkit] plan_graph.py-Checker fehlgeschlagen "
                  f"({plan_graph_pfad}): {e}", file=sys.stderr)
            return None

        def symbol_check(typ: str, ziel: str) -> tuple[str, str | None]:
            if typ == "modules":
                if modul._modul_vorhanden(ziel, str(projekt)):
                    return "ok", None
                return "geplant", None
            return modul._zustand(typ, ziel, best, namen)

        return symbol_check

    def _path_to_id_fn(self):
        if self._path_to_id_cache is not self._UNSET:
            return self._path_to_id_cache
        self._path_to_id_cache = self._build_path_to_id_fn()
        return self._path_to_id_cache

    def _build_path_to_id_fn(self):
        root = _find_project_root()
        if root is None:
            return None
        site_kit_pfad = root / "tools" / "code-graph" / "site_kit.py"
        if not site_kit_pfad.is_file():
            return None
        spec = importlib.util.spec_from_file_location("site_kit", site_kit_pfad)
        if spec is None or spec.loader is None:
            return None
        try:
            modul = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(modul)
            return getattr(modul, "path_to_id", None)
        except Exception as e:
            print(f"[context-toolkit] site_kit.py-Import fehlgeschlagen ({e}) — "
                  f"baustein_doc-Resolver wird deaktiviert", file=sys.stderr)
            return None


class DemoAdapter:
    """Minimal second implementation of `HostAdapter`, proving the Protocol
    needs nothing TALOS-specific — an in-memory dict instead of a symbol
    tree, a one-line path rule instead of a generated index. Used by the
    contract test in `tests/test_host_adapters.py`; a real second host
    would replace the dict/rule with its own index, not this class itself."""

    def __init__(self, known_symbols: dict[str, str] | None = None) -> None:
        self._known = known_symbols or {}

    def has_symbol_index(self) -> bool:
        return bool(self._known)

    def resolve_symbol(self, kind: str, target: str) -> tuple[str, str | None]:
        if target in self._known:
            return "ok", None
        return "gebrochen", f"{kind} target '{target}' not in the demo symbol table"

    def path_to_id(self, path: str) -> str | None:
        return f"py:{path.replace('/', '.').removesuffix('.py')}" if path.endswith(".py") else None


def discover_host_adapter(work_dir: Path | None = None) -> HostAdapter:
    """The one entry point `mcp_server.py` calls. Returns the built-in TALOS
    discovery adapter — ALWAYS a real object, never `None`: `has_symbol_index()`/
    `path_to_id()` returning "nothing available" is how "no TALOS files
    found" and "adapter not installed" are expressed, not a `None` from
    this function. A host wanting a DIFFERENT adapter passes its own
    `HostAdapter`-shaped object directly to `WorkEngine.lint()`/the baustein
    tools instead of going through this function — there is no plugin
    registry to configure yet (tracked as a known gap)."""
    return TalosDiscoveryAdapter(work_dir)
