from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Iterable

import yaml
from pydantic import ValidationError

from claude_rules.models import Rule, RulePriority, RuleScope, RuleType


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

    def load_directory(self, root: Path | str) -> dict:
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

        if errors:
            details = "\n".join(str(e) for e in errors)
            raise RuleLoadError(root_path, f"{len(errors)} rule(s) failed to load:\n{details}")

        keys_seen: set[str] = set()
        for rule in loaded:
            if rule.key in keys_seen:
                raise RuleLoadError(root_path, f"duplicate rule key: {rule.key}")
            keys_seen.add(rule.key)

        self._rules = loaded
        return {"loaded": len(loaded), "root": str(root_path)}

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
