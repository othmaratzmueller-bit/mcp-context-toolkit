"""Task-Registry des deterministischen Harness. Jede Aufgabe stellt bereit:
  NAME, TARGET, PROMPT (was das Modell bekommt) und grade(source) -> {axis: score}.
"""
from __future__ import annotations

from types import SimpleNamespace

from . import cidr_contains, honest_patch, safe_join, ssrf_guard

_MODULES = [safe_join, ssrf_guard, cidr_contains, honest_patch]

TASKS = {
    m.NAME: SimpleNamespace(
        name=m.NAME,
        target=getattr(m, "TARGET", ""),
        prompt=m.PROMPT,
        grade=m.grade,
    )
    for m in _MODULES
}

__all__ = ["TASKS"]
