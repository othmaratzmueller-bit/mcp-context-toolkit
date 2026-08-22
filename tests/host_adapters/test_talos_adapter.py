import json
from pathlib import Path

import pytest

from mcp_context_toolkit.host_adapters import TalosDiscoveryAdapter, _find_project_root


class TestTalosHostAdapter:
    """Optional host-adapter test running specifically inside a TALOS
    checkout. Goes through the public `HostAdapter` protocol
    (`TalosDiscoveryAdapter.path_to_id`) — not a private `_`-prefixed
    function imported from `mcp_server.py` — so a second host implementing
    the same protocol could run an equivalent test against its own adapter
    (mct-Reifeplan Etappe 4). `_find_project_root` is used here only for
    this test's OWN skip-detection convenience, not as the interface under
    test (that is `path_to_id`) — it is a cross-module helper shared with
    `mcp_server.py` (`host_adapters.py`'s own module), not private to this
    test."""

    def test_resolves_real_baustein_docs_keys(self):
        adapter = TalosDiscoveryAdapter()
        root = _find_project_root()
        if root is None or not (root / "tools" / "code-graph" / "site_kit.py").is_file():
            pytest.skip("no tools/code-graph/site_kit.py in this checkout")
        data_file = (Path(__file__).resolve().parents[3]
                     / "tools" / "code-graph" / "data" / "baustein-docs.json")
        if not data_file.is_file():
            pytest.skip("baustein-docs.json not present in this checkout")
        data = json.loads(data_file.read_text(encoding="utf-8"))
        for key, entry in data.items():
            assert adapter.path_to_id(entry["path"]) == key
