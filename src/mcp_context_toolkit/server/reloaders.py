"""Freshness for long-running server state — reload a store only when its
watched directories actually changed, never on every call."""

from __future__ import annotations

from pathlib import Path


def _tree_mtime(root: Path) -> float:
    """Highest mtime across a store tree (the dir itself + every file/subdir).

    Walking the whole tree — not just the dir's own mtime — is deliberate:
    editing an existing file bumps only that file's mtime, while add/remove
    bumps the dir's. Taking the max over everything catches both, so an in-place
    edit is seen as well as a freshly added memory file. 0.0 if the tree is gone.
    """
    try:
        latest = root.stat().st_mtime
    except OSError:
        return 0.0
    for p in root.rglob("*"):
        try:
            mt = p.stat().st_mtime
        except OSError:
            continue  # racing deletion etc. — skip, best-effort
        if mt > latest:
            latest = mt
    return latest


class _Reloader:
    """Holds a built engine + the dirs it was loaded from, and rebuilds it (via
    the supplied loader) only when those trees' mtime changes.

    A long-running MCP process loads its store once at start; without this an
    edit or a consolidation re-bundle from a parallel session stays invisible
    until restart. With it, the next tool call sees the change — a cheap mtime scan,
    reload only on a real change. The engines stay pure-read; the freshness
    concern lives here in the server layer.
    """

    def __init__(self, loader, watch_dirs):
        self._loader = loader
        self._watch = [Path(d) for d in watch_dirs if d is not None]
        self._mtime = self._scan()
        self._engine = self._loader()

    @property
    def watch_dirs(self) -> list[Path]:
        """Watched dirs as a public, copy-stable view (read-only)."""
        return list(self._watch)

    def _scan(self) -> float:
        return max((_tree_mtime(d) for d in self._watch), default=0.0)

    def current(self):
        """Return the engine, rebuilding first iff the watched trees changed."""
        mt = self._scan()
        if mt != self._mtime:
            self._mtime = mt
            self._engine = self._loader()
        return self._engine
