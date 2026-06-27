"""Usage tracking for knowledge stores — the hot/cold (frecency) signal.

Frequency-dominant by design: a memory's heat is driven by how OFTEN it is
used, not by wall-clock recency. A weekend (or three-week) pause must NOT cool
a heavily-used memory — the TALOS principle "decay by conversation distance,
not wall time" applies to the store too. Timestamps are kept for display and
as a tiebreak only; they never decay the score.

Per-machine and best-effort. This captures only EXPLICIT recall()/get_memory()
hits routed through the MCP. The native auto-recall (autoMemoryDirectory ->
system-reminders) bypasses this layer, so counts are a LOWER BOUND, never a
full tally. The score must — and does — degrade gracefully under that
undercounting: log-damped and frequency-dominant, so a handful of missed hits
cannot flip a ranking.
"""

from __future__ import annotations

import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path

# Heat weights: opening the full body ("I actually used this") is a stronger
# signal than merely surfacing in a recall result list.
_W_OPEN = 3
_W_RECALL = 1


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _heat(opens: int, recalls: int) -> float:
    """Frequency-only, log-damped boost factor. No wall-clock decay."""
    return math.log1p(opens * _W_OPEN + recalls * _W_RECALL)


class UsageStore:
    """Reads/writes a ``_usage.json`` sidecar next to the memories.

    Shape::

        {name: {"opens": int, "recalls": int,
                "last_open": iso|null, "last_recall": iso|null}}

    The ``_``-prefix keeps the file out of ``iter_files`` (never parsed as a
    memory) and it is ``.json`` not ``.md`` — doubly safe. Gitignored: usage is
    a per-machine signal, so dev and prod each keep their own hot set.
    """

    FILENAME = "_usage.json"

    def __init__(self, path: Path):
        self.path = Path(path)
        self._data: dict[str, dict] = self._load()

    @classmethod
    def for_memory_dir(cls, memory_dir: str | Path) -> "UsageStore":
        return cls(Path(memory_dir) / cls.FILENAME)

    def _load(self) -> dict[str, dict]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}  # missing or corrupt -> start clean, never crash the MCP
        return raw if isinstance(raw, dict) else {}

    def _entry(self, name: str) -> dict:
        return self._data.setdefault(
            name, {"opens": 0, "recalls": 0, "last_open": None, "last_recall": None}
        )

    def record_open(self, name: str) -> None:
        """Strong hit: the full body was fetched (get_memory)."""
        self._data = self._load()  # merge parallel-process state before mutating
        entry = self._entry(name)
        entry["opens"] = int(entry.get("opens", 0)) + 1
        entry["last_open"] = _now_iso()
        self._save()

    def record_recall(self, names: list[str]) -> None:
        """Weak hit: surfaced in a recall result (the 'searched for' signal)."""
        if not names:
            return
        self._data = self._load()
        ts = _now_iso()
        for name in names:
            entry = self._entry(name)
            entry["recalls"] = int(entry.get("recalls", 0)) + 1
            entry["last_recall"] = ts
        self._save()

    def boosts(self) -> dict[str, float]:
        """name -> frecency boost factor, for blending into recall scoring."""
        return {
            name: _heat(int(e.get("opens", 0)), int(e.get("recalls", 0)))
            for name, e in self._data.items()
        }

    def report(self) -> list[dict]:
        """Hot -> cold rows for the memory_usage tool and the /dream reorder."""
        rows = [
            {
                "name": name,
                "opens": int(e.get("opens", 0)),
                "recalls": int(e.get("recalls", 0)),
                "last_open": e.get("last_open"),
                "last_recall": e.get("last_recall"),
                "heat": round(_heat(int(e.get("opens", 0)), int(e.get("recalls", 0))), 3),
            }
            for name, e in self._data.items()
        ]
        rows.sort(key=lambda r: (-r["heat"], r["name"]))
        return rows

    def _save(self) -> None:
        tmp = self.path.parent / (self.path.name + ".tmp")
        try:
            tmp.write_text(
                json.dumps(self._data, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            os.replace(tmp, self.path)  # atomic on POSIX
        except OSError:
            pass  # usage is best-effort; never break recall on a write failure
