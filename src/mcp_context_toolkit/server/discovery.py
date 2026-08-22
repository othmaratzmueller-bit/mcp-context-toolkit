"""Walk-up auto-discovery for every optional store the server can serve.
Each function returns None on "not found" — the caller gates registration
on that, never a hard failure."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

from mcp_context_toolkit import capability
from mcp_context_toolkit.core import store_conventions
from mcp_context_toolkit.host_adapters import _find_project_root


def _discover_rules_dir() -> Path | None:
    """Locate the PROJECT rules tier. Env CONTEXT_RULES_DIR wins, else walk up
    for <conv>/rules. Returns None if none found — the server then runs with
    the shared tier only (or no rules at all): a workspace without own rules
    must NOT lose the memory tools or the shared grundregeln (same gating
    rationale as the optional memory tiers)."""
    env = os.environ.get("CONTEXT_RULES_DIR")
    if env:
        p = Path(env).expanduser().resolve()
        return p if p.is_dir() else None

    cwd = Path.cwd()
    for candidate in [cwd, *cwd.parents]:
        for conv in store_conventions():
            rules = candidate / conv / "rules"
            if rules.is_dir():
                return rules
    return None


def _discover_memory_dir() -> Path | None:
    """Locate the PROJECT memory directory. Env CONTEXT_MEMORY_DIR wins, else
    walk up for .context/memory (or .claude/memory). Returns None if none found —
    memory tools are then simply not registered and rules keep working untouched."""
    env = os.environ.get("CONTEXT_MEMORY_DIR")
    if env:
        p = Path(env).expanduser()
        return p if p.is_dir() else None
    for candidate in [Path.cwd(), *Path.cwd().parents]:
        for conv in store_conventions():
            mem = candidate / conv / "memory"
            if mem.is_dir():
                return mem
    return None


def _discover_agent_memory_dir() -> Path | None:
    """Locate the PROJECT agent-memory directory. Env CONTEXT_AGENT_MEMORY_DIR wins,
    else walk up for .context/agent-memory (or .claude/agent-memory). Returns None if absent."""
    env = os.environ.get("CONTEXT_AGENT_MEMORY_DIR")
    if env:
        p = Path(env).expanduser()
        return p if p.is_dir() else None
    for candidate in [Path.cwd(), *Path.cwd().parents]:
        for conv in store_conventions():
            mem = candidate / conv / "agent-memory"
            if mem.is_dir():
                return mem
    return None


def _discover_docs_dir() -> Path | None:
    """Locate the PROJECT `docs/` tree (prose documentation, e.g. project
    architecture/governance docs — Work-Record `doku_lesend_indexieren_statt_
    zerreissen`). Env CONTEXT_DOCS_DIR wins, else the project root's `docs/`
    sibling. Returns None if none found — the docs tools are then simply not
    registered, same gating shape as memory/work."""
    env = os.environ.get("CONTEXT_DOCS_DIR")
    if env:
        p = Path(env).expanduser()
        return p if p.is_dir() else None
    root = _find_project_root()
    if root is None:
        return None
    docs = root / "docs"
    return docs if docs.is_dir() else None


def _discover_wissen() -> capability.CapabilityQuelle | None:
    """Wire the capability index: project root, index file, builder, tokenizer.

    Returns None — and the tool is simply not registered — when any piece is
    missing. Deliberately fail-closed on the TOKENIZER in particular: index and
    query must split words identically, and a fallback tokenizer would answer
    plausibly while silently missing every term the index actually holds. A
    missing tool is diagnosable; a subtly wrong one is not.
    """
    env = os.environ.get("CONTEXT_INDEX_FILE")
    for candidate in [Path.cwd(), *Path.cwd().parents]:
        for conv in store_conventions():
            if not (candidate / conv).is_dir():
                continue
            index = Path(env) if env else candidate / conv / ".index" / "wissen.json"
            bauer = candidate / "scripts" / "wissens_index.py"
            if not bauer.is_file():
                continue
            spec = importlib.util.spec_from_file_location("wissens_index", bauer)
            if spec is None or spec.loader is None:
                continue
            try:
                modul = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(modul)
                zerlege = modul.zerlege
            except Exception:
                return None
            return capability.CapabilityQuelle(candidate, index, zerlege, bauer)
    return None


def _load_json_data(file_path: Path | None) -> dict:
    """Load a JSON data file (e.g., baustein-docs.json or function-docs.json).

    Helper for `get_baustein_doc` and `get_function_doc`. Returns an empty dict
    if `file_path` is None, file does not exist, or fails to parse.

    Explicit configuration via arguments or env vars (`CONTEXT_BAUSTEIN_DOCS_FILE` /
    `CONTEXT_FUNCTION_DOCS_FILE`) instead of hard-coding a host path."""
    if file_path is None or not file_path.is_file():
        return {}
    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _optional_dir(env_name: str) -> Path | None:
    """Resolve an optional tier directory from an env var. Returns None if the
    var is unset/empty or does not point at an existing directory. Used for the
    cross-project 'user' memory tier (CONTEXT_USER_MEMORY_DIR), the org 'core'
    memory tier (CONTEXT_CORE_MEMORY_DIR) and the shared org RULES tier
    (CONTEXT_SHARED_RULES_DIR) — all purely additive, absent by default."""
    raw = os.environ.get(env_name)
    if not raw:
        return None
    p = Path(raw).expanduser()
    return p if p.is_dir() else None


def _optional_file(env_name: str) -> Path | None:
    """Resolve an optional FILE from an env var — same shape as
    `_optional_dir`, one level down (a single JSON file, not a tree). Used
    for the baustein/function doc domain (Work-Record `mcp_baustein_
    funktions_doku_domaene_konvergiertes_design`): those JSON files already
    exist at a TALOS-specific path (`tools/code-graph/data/*.json`) outside
    any `.claude/` convention, so this engine stays generic by taking the
    path from config (`CONTEXT_BAUSTEIN_DOCS_FILE`/`CONTEXT_FUNCTION_DOCS_
    FILE`) instead of hard-coding a TALOS path."""
    raw = os.environ.get(env_name)
    if not raw:
        return None
    p = Path(raw).expanduser()
    return p if p.is_file() else None
