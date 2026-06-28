import os
from pathlib import Path

from mcp_context_toolkit.mcp_server import _Reloader, _tree_mtime
from mcp_context_toolkit.memory import MemoryEngine


def _write_mem(d: Path, name: str, body: str = "x") -> None:
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.md").write_text(f"---\nname: {name}\n---\n\n{body}\n", encoding="utf-8")


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
