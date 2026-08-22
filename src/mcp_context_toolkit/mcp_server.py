"""Backward-compatible import path.

The server implementation moved to `mcp_context_toolkit.server` (mct-
Reifeplan Etappe 5: `app.py` + `reloaders.py` + `serializers.py` +
`discovery.py` + `tools_rules.py`/`tools_memory.py`/`tools_work.py`/
`tools_docs.py`/`tools_domains.py`/`tools_baustein.py`/`tools_capability.py`
— one module per domain, so a change to one tool group needs no edit to
another's). This module stays because `pyproject.toml`'s
`context-toolkit-mcp` entry point names `mcp_context_toolkit.mcp_server:main`,
and existing callers/tests import discovery helpers from here directly —
both keep working unchanged, at this same import path, indefinitely."""

from __future__ import annotations

from .server.app import (
    AUTHOR,
    AUTHOR_URL,
    TOOLKIT_NAME,
    _print_banner,
    build_server,
    main,
)
from .server.discovery import (
    _discover_agent_memory_dir,
    _discover_docs_dir,
    _discover_memory_dir,
    _discover_rules_dir,
    _discover_wissen,
    _load_json_data,
    _optional_dir,
    _optional_file,
)
from .server.reloaders import _Reloader, _tree_mtime
from .server.tools_baustein import _JsonDocStore

__all__ = [
    "AUTHOR", "AUTHOR_URL", "TOOLKIT_NAME",
    "_JsonDocStore", "_Reloader",
    "_discover_agent_memory_dir", "_discover_docs_dir", "_discover_memory_dir",
    "_discover_rules_dir", "_discover_wissen",
    "_load_json_data", "_optional_dir", "_optional_file",
    "_print_banner", "_tree_mtime",
    "build_server", "main",
]

if __name__ == "__main__":
    main()
