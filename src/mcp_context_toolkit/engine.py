from __future__ import annotations

import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Iterable

import yaml
from pydantic import ValidationError

from mcp_context_toolkit.models import Rule, RulePriority, RuleScope, RuleType


_FINGERPRINT_EXCLUDE = {
    "source_path",
    "created",
    "last_reviewed",
    "review_interval_days",
    "owner",
}


def fingerprint_rules(rules: list["Rule"]) -> str:
    """Stable 16-char hex hash over the user-visible fields of a rule set.

    Used for live-reload dedup: if a hook stores this fingerprint per file,
    a subsequent hook invocation can detect whether the rule set matching
    the file has changed since last time. Catches add/delete, content edits,
    summary edits, priority changes, title/short_id changes, glob changes.

    Ignores metadata that doesn't affect what the agent sees (source_path,
    created/reviewed timestamps, owner, review_interval). Rule-order
    independent because rules are sorted by key before hashing.
    """
    if not rules:
        return "0"
    h = hashlib.sha256()
    for r in sorted(rules, key=lambda x: x.key):
        dump = r.model_dump(mode="json", exclude=_FINGERPRINT_EXCLUDE)
        h.update(json.dumps(dump, sort_keys=True, ensure_ascii=False).encode("utf-8"))
        h.update(b"|")
    return h.hexdigest()[:16]


@lru_cache(maxsize=512)
def _compile_glob(pattern: str) -> re.Pattern[str]:
    """Compile a glob pattern with '**' support to a regex.

    Semantics match pathlib PurePosixPath.match for '**' recursive matching
    and '*' single-segment matching. '?' matches a single non-'/' character.
    """
    i = 0
    n = len(pattern)
    out: list[str] = ["^"]
    while i < n:
        c = pattern[i]
        if c == "*":
            if i + 1 < n and pattern[i + 1] == "*":
                j = i + 2
                if j < n and pattern[j] == "/":
                    out.append("(?:.*/)?")
                    i = j + 1
                    continue
                out.append(".*")
                i = j
                continue
            out.append("[^/]*")
            i += 1
            continue
        if c == "?":
            out.append("[^/]")
            i += 1
            continue
        if c in ".+()|^$[]{}\\":
            out.append("\\" + c)
        else:
            out.append(c)
        i += 1
    out.append("$")
    return re.compile("".join(out))


class RuleLoadError(Exception):
    def __init__(self, file: Path, errors: str):
        super().__init__(f"{file}: {errors}")
        self.file = file
        self.errors = errors


class RulesEngine:
    def __init__(self, rules: list[Rule] | None = None):
        self._rules: list[Rule] = rules or []

    @classmethod
    def from_directory(cls, root: Path | str) -> "RulesEngine":
        engine = cls()
        engine.load_directory(root)
        return engine

    def load_directory(
        self, root: Path | str, *, strict: bool = True
    ) -> dict:
        """Load all rule YAMLs under root.

        strict=True (default): raises RuleLoadError if any file fails to parse
        or validate. Used for CI, pytest, manual validation.

        strict=False: skips broken files, loads the valid ones, returns stats
        including the list of errors. Used by the hook path so a single broken
        YAML does not blind the user to all other rules.
        """
        root_path = Path(root)
        if not root_path.exists():
            raise FileNotFoundError(f"Rules directory not found: {root_path}")

        loaded: list[Rule] = []
        errors: list[RuleLoadError] = []

        for yaml_file in self._iter_rule_files(root_path):
            try:
                rule = self._load_file(yaml_file)
                loaded.append(rule)
            except RuleLoadError as e:
                errors.append(e)

        if errors and strict:
            details = "\n".join(str(e) for e in errors)
            raise RuleLoadError(root_path, f"{len(errors)} rule(s) failed to load:\n{details}")

        keys_seen: set[str] = set()
        deduped: list[Rule] = []
        duplicate_errors: list[str] = []
        for rule in loaded:
            if rule.key in keys_seen:
                msg = f"duplicate rule key: {rule.key} (source: {rule.source_path})"
                if strict:
                    raise RuleLoadError(root_path, msg)
                duplicate_errors.append(msg)
                continue
            keys_seen.add(rule.key)
            deduped.append(rule)

        self._rules = deduped
        return {
            "loaded": len(deduped),
            "root": str(root_path),
            "errors": [str(e) for e in errors] + duplicate_errors,
        }

    @staticmethod
    def _iter_rule_files(root: Path) -> Iterable[Path]:
        for path in sorted(root.rglob("*.yaml")):
            if any(part.startswith("_") for part in path.relative_to(root).parts):
                continue
            yield path

    @staticmethod
    def _load_file(path: Path) -> Rule:
        try:
            raw = path.read_text(encoding="utf-8")
            data = yaml.safe_load(raw)
            if not isinstance(data, dict):
                raise RuleLoadError(path, "YAML root must be a mapping")
            data["source_path"] = str(path)
            return Rule(**data)
        except yaml.YAMLError as e:
            raise RuleLoadError(path, f"YAML parse error: {e}") from e
        except ValidationError as e:
            raise RuleLoadError(path, f"schema validation failed: {e}") from e

    @property
    def rules(self) -> list[Rule]:
        return list(self._rules)

    def get_rule(self, key: str) -> Rule | None:
        return next((r for r in self._rules if r.key == key), None)

    @classmethod
    def validate_directory(cls, root: Path | str) -> dict:
        """Dry-run validation over a rules directory.

        Walks every *.yaml, tries to parse + validate, collects errors
        without mutating engine state. Also checks for duplicate keys and
        conflicts_with references that point to nonexistent rules.

        Returns {"ok": bool, "rule_count": N, "errors": [...], "warnings": [...]}.
        Used by the validate_rules MCP tool and by `context-toolkit-query validate`.
        """
        root_path = Path(root)
        if not root_path.exists():
            return {
                "ok": False,
                "rule_count": 0,
                "errors": [f"Rules directory not found: {root_path}"],
                "warnings": [],
            }

        loaded: list[Rule] = []
        errors: list[str] = []
        warnings: list[str] = []

        for yaml_file in cls._iter_rule_files(root_path):
            try:
                loaded.append(cls._load_file(yaml_file))
            except RuleLoadError as e:
                errors.append(str(e))

        seen: set[str] = set()
        for rule in loaded:
            if rule.key in seen:
                errors.append(
                    f"duplicate key {rule.key} (source: {rule.source_path})"
                )
            seen.add(rule.key)

        valid_keys = {r.key for r in loaded}
        for rule in loaded:
            for conflict in rule.conflicts_with:
                if conflict.rule not in valid_keys:
                    warnings.append(
                        f"{rule.key} conflicts_with unknown rule "
                        f"'{conflict.rule}' (source: {rule.source_path})"
                    )

        return {
            "ok": len(errors) == 0,
            "rule_count": len(loaded),
            "errors": errors,
            "warnings": warnings,
        }

    def write_fallback_markdown(self, target: Path | str) -> dict:
        """Generate a plain-markdown fallback file for MCP-outage scenarios.

        Writes only non_negotiable + mandatory rules, grouped by type.
        The target should be a path under .claude/rules/_meta/ (git-ignored
        or manually committed — caller decides). Used as the last-resort
        reference when the context-toolkit MCP server is unreachable.
        """
        target_path = Path(target)
        target_path.parent.mkdir(parents=True, exist_ok=True)

        critical = [
            r for r in self._rules
            if r.priority in ("non_negotiable", "mandatory")
        ]
        critical.sort(
            key=lambda r: (
                {"non_negotiable": 0, "mandatory": 1, "recommended": 2}[r.priority],
                r.type,
                r.key,
            )
        )

        lines: list[str] = [
            "# context-toolkit Fallback (auto-generated)",
            "",
            "Used as the last-resort reference when the context-toolkit MCP",
            "server is unreachable. Contains only non_negotiable + mandatory",
            "rules from `.claude/rules/**/*.yaml`. Regenerated on every",
            "engine load. Do not edit by hand.",
            "",
            f"Generated {len(critical)} critical rules from "
            f"{len(self._rules)} total loaded.",
            "",
        ]

        current_type = None
        for rule in critical:
            if rule.type != current_type:
                lines.append(f"## {rule.type}")
                lines.append("")
                current_type = rule.type
            short = f"[{rule.short_id}] " if rule.short_id else ""
            lines.append(f"### {short}{rule.title} ({rule.priority})")
            lines.append("")
            lines.append(rule.summary.strip())
            lines.append("")
            lines.append(f"**Applies to:** `{', '.join(rule.applies_to.files)}`")
            lines.append("")

        target_path.write_text("\n".join(lines), encoding="utf-8")
        return {"written": len(critical), "target": str(target_path)}

    def list_keys(
        self,
        type: RuleType | None = None,
        scope: RuleScope | None = None,
    ) -> list[str]:
        return [r.key for r in self._filter(type=type, scope=scope)]

    def query(
        self,
        type: RuleType | None = None,
        scope: RuleScope | None = None,
        module: str | None = None,
        priority: RulePriority | None = None,
    ) -> list[Rule]:
        return self._filter(type=type, scope=scope, module=module, priority=priority)

    def query_for_file(self, file_path: str) -> list[Rule]:
        normalized = self._normalize_path(file_path)
        matches: list[Rule] = []
        for rule in self._rules:
            if self._file_matches_rule(normalized, rule):
                matches.append(rule)
        matches.sort(key=lambda r: (self._priority_order(r.priority), r.key))
        return matches

    def query_for_file_with_fingerprint(
        self, file_path: str
    ) -> tuple[list[Rule], str]:
        matches = self.query_for_file(file_path)
        return matches, fingerprint_rules(matches)

    @staticmethod
    def _normalize_path(file_path: str) -> str:
        return str(PurePosixPath(file_path.replace("\\", "/")))

    @classmethod
    def _file_matches_rule(cls, file_path: str, rule: Rule) -> bool:
        for excl in rule.applies_to.excludes:
            if cls._glob_match(file_path, excl):
                return False
        for pattern in rule.applies_to.files:
            if cls._glob_match(file_path, pattern):
                return True
        return False

    @staticmethod
    def _glob_match(path: str, pattern: str) -> bool:
        return _compile_glob(pattern).match(path) is not None

    @staticmethod
    def _priority_order(priority: RulePriority) -> int:
        return {"non_negotiable": 0, "mandatory": 1, "recommended": 2}[priority]

    def _filter(
        self,
        type: RuleType | None = None,
        scope: RuleScope | None = None,
        module: str | None = None,
        priority: RulePriority | None = None,
    ) -> list[Rule]:
        result = self._rules
        if type is not None:
            result = [r for r in result if r.type == type]
        if scope is not None:
            result = [r for r in result if r.scope == scope]
        if priority is not None:
            result = [r for r in result if r.priority == priority]
        if module is not None:
            result = [r for r in result if module in r.modules or "all" in r.modules]
        return list(result)
