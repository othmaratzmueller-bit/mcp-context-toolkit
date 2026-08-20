import json
from pathlib import Path

import pytest

from mcp_context_toolkit.mcp_server import _find_project_root, _path_to_id_factory


class TestTalosHostAdapter:
    """Optional host-adapter test running specifically inside a TALOS checkout."""

    def test_resolves_real_baustein_docs_keys(self):
        root = _find_project_root()
        if root is None or not (root / "tools" / "code-graph" / "site_kit.py").is_file():
            pytest.skip("no tools/code-graph/site_kit.py in this checkout")
        path_to_id = _path_to_id_factory()
        assert path_to_id is not None
        data_file = (Path(__file__).resolve().parents[3]
                     / "tools" / "code-graph" / "data" / "baustein-docs.json")
        if not data_file.is_file():
            pytest.skip("baustein-docs.json not present in this checkout")
        data = json.loads(data_file.read_text(encoding="utf-8"))
        for key, entry in data.items():
            assert path_to_id(entry["path"]) == key
