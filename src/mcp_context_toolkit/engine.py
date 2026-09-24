from __future__ import annotations

import contextlib
import hashlib
import json
import re
from collections.abc import Iterable
from pathlib import Path, PurePosixPath

import yaml
from pydantic import ValidationError

from mcp_context_toolkit.core import iter_files, parse_frontmatter
from mcp_context_toolkit.models import Decision, Rule, RulePriority, RuleScope, RuleTier, RuleType

from ._globs import compile_glob as _compile_glob
from .server.serializers import _rule_summary
from .work import WorkEngine

# Etappe C (mcp_decisions_in_work_falten, 2026-08-19): the decision store folded
# into the Work-Store as `kind: decision` cards — WorkStatus back to the narrower
# DecisionStatus vocabulary this engine still speaks externally (Decision model,
# query_decisions_for_file's contract, _format_markdown in cli.py). Lossy on one
# edge: WorkStatus "dropped" collapses both "rejected" and "deprecated" — measured
# over the full migrated corpus (669 cards) there are 0 "deprecated" records, so
# "rejected" is the only value this ever actually produces today. "active" is
# defensive-only: plan-keeper.md's kanten-gate exception lets decision/
# changelog cards skip straight open->done, so a decision should never be caught
# mid-flight — if one ever is, "draft" (still-in-motion) is the safer read than
# claiming it was "accepted".
#
# THREE places carry a status-mapping table for this same concept (W8-Reviewer-
# Fund WARNING-5, 2026-08-19) — they cannot share one import, each runs in a
# different Python environment (this engine's venv / doc-flush.py's system-python
# / handbuch_payload.py's zero-dependency system-python) and none can pull in the
# other two without pulling in a dependency that environment must not have. Same
# direction (WorkStatus -> DecisionStatus) as `tools/code-graph/handbuch_payload.
# py::_WORK_TO_DECISION_STATUS` (same name, on purpose). The INVERSE direction
# (DecisionStatus -> WorkStatus, used when WRITING a new card) lives in
# `.claude/hooks/doc-flush.py::_DECISION_TO_WORK_STATUS`. Add a WorkStatus here,
# add it in both.
_WORK_TO_DECISION_STATUS: dict[str, str] = {
    "open": "draft",
    "active": "draft",
    "done": "accepted",
    "dropped": "rejected",
    "superseded": "superseded",
}

_FINGERPRINT_EXCLUDE = {
    "source_path",
    "created",
    "last_reviewed",
    "review_interval_days",
    "owner",
    "tier",
}

# Sharpness lint (see validate_directory): the injected visibility window and
# the modal/prohibition tokens (DE+EN) that must appear inside it. Word-bounded
# so "no"/"nur" never match inside other words ("nothing", "nurture").
_SHARPNESS_WINDOW = 160
_SHARPNESS_RE = re.compile(
    r"\b(nie|niemals|immer|muss|müssen|muessen|kein|keine|nur|erst|stopp|stop|"
    r"pflicht(?:-review)?|verboten|jede[rs]?|"
    r"never|always|must|every|no|not|only|first|forbidden|required)\b",
    re.IGNORECASE,
)

# Decision injection cut (see query_decisions_for_file): decisions accumulate
# unbounded on hot files, so injection defaults to the newest TOP_K accepted
# ones. Measured before the cut: 27 decisions / 31.5k chars = 78 % of the
# injected payload on a single hot file.
DECISION_INJECT_STATUSES: tuple[str, ...] = ("accepted",)
DECISION_TOP_K = 8


def fingerprint_rules(rules: list[Rule], decisions: list[Decision] | None = None,
                     arbeit: list | None = None) -> str:
    """Stable 16-char hex hash over the user-visible fields of the injected context.

    Used for live-reload dedup: if a hook stores this fingerprint per file,
    a subsequent hook invocation can detect whether the rule set or decision set
    matching the file has changed since last time. Catches add/delete, content edits,
    summary/reason edits, priority/status changes.

    `arbeit` (open work items for the file, 2026-08-05) MUST be part of it. The
    delivery shows them alongside the rules — but if only the work store changes
    and the rules do not, the dedup would suppress the very update that carries
    new information. A hash that ignores part of what it fingerprints turns the
    dedup into a silent staleness filter. Only key+status+title go in: a body
    edit is not worth re-injecting, a status flip is.
    """
    if not rules and not decisions and not arbeit:
        return "0"
    h = hashlib.sha256()
    for r in sorted(rules, key=lambda x: x.key):
        dump = r.model_dump(mode="json", exclude=_FINGERPRINT_EXCLUDE)
        h.update(json.dumps(dump, sort_keys=True, ensure_ascii=False).encode("utf-8"))
        h.update(b"|")
    if decisions:
        for d in sorted(decisions, key=lambda x: x.key):
            dump = d.model_dump(mode="json", exclude={"source_path"})
            h.update(json.dumps(dump, sort_keys=True, ensure_ascii=False).encode("utf-8"))
            h.update(b"|")
    if arbeit:
        # Nur key/status/title — eine Body-Aenderung ist keine neue Auskunft,
        # ein Status-Wechsel oder ein neuer Punkt schon.
        for w in sorted(arbeit, key=lambda x: x.key):
            h.update(f"{w.key}\x1f{w.status}\x1f{w.title}".encode())
            h.update(b"|")
    return h.hexdigest()[:16]


def _erste_zeile(exc: BaseException) -> str:
    """First line of an exception's message — even when there is none.

    ``str(ValueError())`` is ``''``, and ``''.splitlines()`` is ``[]`` — so the
    obvious ``str(exc).splitlines()[0]`` raises ``IndexError`` from inside the
    very handler that exists to keep a broken file from taking the store down
    (Reviewer finding, v1.4.492). Measured as reachable only through an
    injected raiser, not through any YAML tried: recursive anchors,
    ``!!python/object`` and a NUL byte all produce non-empty messages. Latent,
    then — but the handler's whole promise is that it does not raise, and that
    promise must not hang on every exception having something to say.
    """
    return (str(exc) or "-").splitlines()[0]


class RuleLoadError(Exception):
    def __init__(self, file: Path, errors: str):
        super().__init__(f"{file}: {errors}")
        self.file = file
        self.errors = errors


class RulesEngine:
    def __init__(self, rules: list[Rule] | None = None, decisions: list[Decision] | None = None, graph: dict | None = None):
        self._rules: list[Rule] = rules or []
        self._decisions: list[Decision] = decisions or []
        self._graph: dict = graph or {}
        self._roots: list[tuple[Path, RuleTier]] = []
        # Files skipped while loading — surfaced by the runtime callers (server
        # banner, coder onboarding) so a single broken YAML degrades LOUDLY
        # ("6 loaded, 10 skipped") instead of silently blanking the store.
        #
        # TWO sources, and they behave differently:
        #
        #   [<tier>] …      RULES. Parse/validation failures and same-tier
        #                   duplicate keys. Only ever populated by a lenient
        #                   (strict=False) load — a strict one raises instead.
        #   [decision] …    DECISIONS. Always lenient: the decision store is a
        #                   companion index, and one broken file there must not
        #                   take the rule set down with it.
        #
        # The "empty after a strict load" half of the old comment was true when
        # only rules wrote here. Since the decision loader stopped swallowing
        # its errors it no longer is — and the prefix is what tells them apart.
        self._load_errors: list[str] = []

    @classmethod
    def from_directory(cls, root: Path | str, tier: RuleTier = "project") -> RulesEngine:
        engine = cls()
        engine.load_directory(root, tier=tier)
        return engine

    @classmethod
    def from_roots(
        cls, roots: dict[str, Path | str], *, strict: bool = True
    ) -> RulesEngine:
        """Build from a {tier: path} mapping. The project tier is loaded BEFORE
        the shared tier, so on a (non-security) key collision the project rule
        wins — specific beats general, exactly like MemoryEngine.from_roots.
        A non_negotiable rule on either side of a collision raises RuleLoadError
        (see load_directory). Mirrors the memory 2-tier wiring."""
        engine = cls()
        for tier in ("project", "shared"):
            if tier in roots:
                engine.load_directory(roots[tier], tier=tier, strict=strict)  # type: ignore[arg-type]
        for tier, path in sorted(roots.items()):
            if tier not in ("project", "shared"):
                engine.load_directory(path, tier="unknown", strict=strict)
        return engine

    def load_directory(
        self, root: Path | str, *, tier: RuleTier = "project", strict: bool = True
    ) -> dict:
        """Load all rule YAMLs under root and ACCUMULATE onto already-loaded
        rules (does not replace prior tiers). Vorbild: MemoryEngine.load_directory.

        Load order IS precedence: load the higher-precedence tier first
        (project), then the shared org tier. On a cross-tier key collision the
        already-loaded (earlier) rule wins and the incoming one is skipped —
        EXCEPT when a non_negotiable rule is on either side of the collision:
        that raises RuleLoadError UNCONDITIONALLY (a security rule must never be
        silently shadowed or downgraded — W4 fail-fast). This guard is NOT
        relaxed by strict=False.

        strict=True (default): raises RuleLoadError if any file fails to parse
        or validate, or on a same-tier duplicate key. Used for CI, pytest.

        strict=False: skips broken files / same-tier dups, loads the valid ones,
        returns stats including the list of errors. Used by the hook path so a
        single broken YAML does not blind the user to all other rules.
        """
        root_path = Path(root)
        if not root_path.exists():
            raise FileNotFoundError(f"Rules directory not found: {root_path}")

        loaded: list[Rule] = []
        errors: list[RuleLoadError] = []

        for yaml_file in self._iter_rule_files(root_path):
            try:
                rule = self._load_file(yaml_file)
                rule.tier = tier
                loaded.append(rule)
            except RuleLoadError as e:
                errors.append(e)

        if errors and strict:
            details = "\n".join(str(e) for e in errors)
            raise RuleLoadError(root_path, f"{len(errors)} rule(s) failed to load:\n{details}")

        existing_by_key = {r.key: r for r in self._rules}
        within_tier_seen: set[str] = set()
        added = 0
        shadowed = 0
        duplicate_errors: list[str] = []
        for rule in loaded:
            # Same-tier duplicate key — as before (raise in strict, skip lenient).
            if rule.key in within_tier_seen:
                msg = f"duplicate rule key: {rule.key} (source: {rule.source_path})"
                if strict:
                    raise RuleLoadError(root_path, msg)
                duplicate_errors.append(msg)
                continue
            within_tier_seen.add(rule.key)

            prior = existing_by_key.get(rule.key)
            if prior is not None:
                # Cross-tier collision: the earlier tier wins. A non_negotiable
                # rule on EITHER side is a hard error regardless of strict — no
                # project rule may silently shadow (or downgrade) a security rule.
                if rule.priority == "non_negotiable" or prior.priority == "non_negotiable":
                    raise RuleLoadError(
                        root_path,
                        f"non_negotiable rule collision on key '{rule.key}': tier "
                        f"'{tier}' ({rule.priority}, {rule.source_path}) collides with "
                        f"already-loaded tier '{prior.tier}' ({prior.priority}, "
                        f"{prior.source_path}). A non_negotiable rule must not be "
                        f"silently overridden — resolve the key clash.",
                    )
                shadowed += 1
                continue
            self._rules.append(rule)
            existing_by_key[rule.key] = rule
            added += 1

        self._roots.append((root_path, tier))

        # Decisions + graph: the FIRST loaded tier that provides them wins (no
        # cross-tier replace). On a single-tier load this is identical to before.
        #
        # Etappe C (2026-08-19): decisions live under the Work-Store now
        # (`.claude/work/decisions/`, `kind: decision` cards) — the old sibling
        # `.claude/decisions/*.yaml` store was folded in and retired
        # (mcp_decisions_in_work_falten). Same relative-to-rules-dir shape
        # (`root_path.parent / ...`), only the leaf changed.
        if not self._decisions:
            decisions_dir = root_path.parent / "work" / "decisions"
            if decisions_dir.is_dir():
                self._load_decisions(decisions_dir)

        if not self._graph:
            graph_file = root_path.parent / "graph" / "reference-index.json"
            if graph_file.exists():
                # The graph is an optional convenience index; a missing or
                # malformed one must not stop the rule set from loading.
                with contextlib.suppress(Exception):
                    self._graph = json.loads(graph_file.read_text(encoding="utf-8"))

        all_errors = [str(e) for e in errors] + duplicate_errors
        self._load_errors.extend(f"[{tier}] {msg}" for msg in all_errors)

        return {
            "loaded": added,
            "shadowed": shadowed,
            "tier": tier,
            "root": str(root_path),
            "errors": all_errors,
        }

    def _load_decisions(self, decisions_dir: Path):
        """Load the decision store, and REPORT what could not be loaded.

        Etappe C (2026-08-19): decisions are now `kind: decision` Work-Store
        cards (frontmatter+body, like every other work item) instead of
        standalone YAML files — the fold-in this method exists for
        (mcp_decisions_in_work_falten). The card's WorkItem-shaped fields are
        translated back into a `Decision` object so every downstream reader
        (`query_decisions_for_file`, `fingerprint_rules`, `cli.py`'s
        `_format_markdown`) keeps working against the unchanged `Decision`
        model — the swap is contained entirely inside this one seam.

        The previous body ended in ``except Exception: pass`` — a broken
        decision vanished without a trace. Measured 2026-08-12 over 397 files:
        two carry unparseable YAML (an unterminated quoted scalar in
        ``reason:``) and had been invisible for weeks; nobody could have known,
        because nothing said so. That lesson carries over unchanged: still
        lenient (one broken card must not take the store down), but loud.
        """
        self._decisions = []
        for card_file in iter_files(decisions_dir, ".md"):
            try:
                text = card_file.read_text(encoding="utf-8")
            except OSError as exc:
                self._load_errors.append(
                    f"[decision] {card_file.name}: nicht lesbar — {exc.__class__.__name__}: "
                    f"{_erste_zeile(exc)}"
                )
                continue

            meta, body = parse_frontmatter(text)
            if not meta:
                self._load_errors.append(
                    f"[decision] {card_file.name}: kein gueltiges Frontmatter"
                )
                continue
            if meta.get("kind") != "decision":
                # Fremdkoerper im decisions/-Verzeichnis (falscher kind) —
                # laut melden statt still zu ignorieren oder als Decision
                # fehlzudeuten.
                self._load_errors.append(
                    f"[decision] {card_file.name}: kind={meta.get('kind')!r} statt 'decision'"
                )
                continue

            work_status = str(meta.get("status") or "open")
            decision_status = _WORK_TO_DECISION_STATUS.get(work_status)
            if decision_status is None:
                self._load_errors.append(
                    f"[decision] {card_file.name}: unbekannter status {work_status!r}"
                )
                continue

            files = list((meta.get("evidence") or {}).get("files") or [])
            data = {
                "key": meta.get("key") or card_file.stem,
                "title": meta.get("title"),
                "date": meta.get("opened"),
                "status": decision_status,
                "applies_to": {"modules": [], "files": files},
                "reason": body.strip(),
                "supersedes": meta.get("supersedes"),
                "source_path": str(card_file),
            }
            try:
                self._decisions.append(Decision(**data))
            except ValidationError as exc:
                felder = sorted({".".join(str(p) for p in e["loc"]) for e in exc.errors()})
                self._load_errors.append(
                    f"[decision] {card_file.name}: Schema verletzt — {', '.join(felder)}"
                )
            except Exception as exc:
                # Breit gefangen, und das ist hier kein Schluder: die Zusage
                # dieses Loaders ist „eine kaputte Datei wirft den Store nicht
                # um". Eine Aufzaehlung erwarteter Fehlertypen kann diese Zusage
                # nicht halten — sie haelt sie nur fuer die Typen, an die der
                # Autor gedacht hat, und genau daran ist die erste Fassung
                # gescheitert. Der Unterschied zum alten `except Exception:
                # pass` ist nicht die Breite, sondern dass hier etwas STEHT.
                self._load_errors.append(
                    f"[decision] {card_file.name}: nicht ladbar — "
                    f"{exc.__class__.__name__}: {_erste_zeile(exc)}"
                )

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

    @property
    def load_errors(self) -> list[str]:
        """Files skipped during lenient loads (tier-prefixed). Empty after a
        strict load. Read by the runtime callers to report degraded loads."""
        return list(self._load_errors)

    @property
    def roots(self) -> list[tuple[Path, RuleTier]]:
        """The (path, tier) pairs this engine was loaded from, in load order.
        Used by validate_rules to re-check every tier and by the reloader to
        watch every root — not just the project one."""
        return list(self._roots)

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

        # Sharpness lint: eval-backed finding (2026-07) — on weak models a rule
        # only works when its INJECTED window (the first ~160 summary chars)
        # names the concrete prohibited/required action with a modal verb
        # ("NEVER invent an API signature", "EVERY db.query MUST filter").
        # Virtue prose buries the rule below the visibility cut. Warn (never
        # error) when a non_negotiable/mandatory summary opens without one.
        for rule in loaded:
            if rule.priority == "recommended":
                continue
            head = " ".join(rule.summary.split())[:_SHARPNESS_WINDOW]
            if not _SHARPNESS_RE.search(head):
                warnings.append(
                    f"{rule.key}: summary opens without a modal/prohibition "
                    f"(NIE/IMMER/MUSS/NEVER/ALWAYS/MUST/EVERY...) in the first "
                    f"{_SHARPNESS_WINDOW} chars — weak models will miss the rule "
                    f"(source: {rule.source_path})"
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
                r.type,
                {"non_negotiable": 0, "mandatory": 1, "recommended": 2}[r.priority],
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

    def query_for_file_tiered(self, file_path: str) -> list[Rule]:
        """Project-tier matches if any; otherwise the shared-tier floor.

        The shared org grundregeln use broad (``**/*``) globs, so they would
        otherwise inject on every file and duplicate the project rules that
        already cover it. This surfaces them ONLY where no project rule
        matches — a generic discipline floor for files outside the project's
        rule globs (a top-level script, a config, a greenfield repo). With no
        shared tier loaded, every match is project-tier → identical to
        query_for_file."""
        matches = self.query_for_file(file_path)
        project = [r for r in matches if r.tier == "project"]
        return project if project else matches

    def query_decisions_for_file(
        self,
        file_path: str,
        statuses: tuple[str, ...] | None = DECISION_INJECT_STATUSES,
        top_k: int | None = DECISION_TOP_K,
    ) -> list[Decision]:
        """Match decisions for a file, cut for injection by default.

        Decisions accumulate unbounded on hot files (no lifecycle pruning),
        so the default returns only the ``top_k`` newest with an allowed
        ``status`` — pass ``statuses=None`` / ``top_k=None`` for the raw,
        unfiltered match set (audits, tooling).
        """
        normalized = self._normalize_path(file_path)
        matches: list[Decision] = []
        for d in self._decisions:
            if statuses is not None and d.status not in statuses:
                continue
            for pattern in d.applies_to.files:
                if self._glob_match(normalized, pattern):
                    matches.append(d)
                    break
        # Newest first; equal dates keep deterministic key order (stable sorts).
        matches.sort(key=lambda d: d.key)
        matches.sort(key=lambda d: d.date, reverse=True)
        return matches if top_k is None else matches[:top_k]

    def query_dependencies(self, file_path: str) -> dict:
        if not self._graph:
            return {}
        normalized = self._normalize_path(file_path)
        py_suffix = normalized.replace("/", ".")
        if py_suffix.endswith(".py"):
            py_suffix = py_suffix[:-3]

        for key, val in self._graph.items():
            if key.startswith("js:") and normalized.endswith(key[3:]):
                return val
            if key.startswith("py:") and py_suffix.endswith(key[3:]):
                return val
        return {}

    @staticmethod
    def _normalize_path(file_path: str) -> str:
        return str(PurePosixPath(file_path.replace("\\", "/")))

    @classmethod
    def _file_matches_rule(cls, file_path: str, rule: Rule) -> bool:
        for excl in rule.applies_to.excludes:
            if cls._glob_match(file_path, excl):
                return False
        return any(cls._glob_match(file_path, p) for p in rule.applies_to.files)

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

    def lint_conflicts(self) -> dict[str, list[str]]:
        """Lint rules for potential contradictions, short_id collisions, and glob overlaps."""
        conflicts: list[str] = []
        short_ids: dict[str, str] = {}
        for r in self._rules:
            if r.short_id:
                if r.short_id in short_ids:
                    conflicts.append(f"Kollision bei short_id '{r.short_id}': {r.key} vs {short_ids[r.short_id]}")
                else:
                    short_ids[r.short_id] = r.key

        # Check for glob overlap with opposing priorities
        for i, r1 in enumerate(self._rules):
            for r2 in self._rules[i + 1:]:
                if r1.key == r2.key:
                    continue
                overlap_globs = set(r1.applies_to.files) & set(r2.applies_to.files)
                if overlap_globs and r1.priority != r2.priority and r1.scope == r2.scope:
                    conflicts.append(
                        f"Widersprüchliche Priorität bei Globs {sorted(overlap_globs)}: "
                        f"{r1.key} ({r1.priority}) vs {r2.key} ({r2.priority})"
                    )
        return {"conflicts": conflicts}


def rules_for_file_payload(
    engine: RulesEngine, file_path: str, work_engine: WorkEngine | None = None
) -> dict:
    """Aggregate rules + decisions + dependencies + cross-domain open work for
    one file — the combined payload `query_rules_for_file` (the MCP tool) and
    a second consumer (mct-Context-Studio-Vollausbau Etappe E's chat tool)
    both need. Extracted (S8) so a second caller cannot silently diverge from
    the tool's actual shape — the `work_engine is None` / lookup-raises
    fail-soft behavior below is exactly the kind of detail a naive
    reimplementation would drop.

    `work_engine` is optional and fails soft: the rules/decisions/dependency
    lookups are this function's core job and must never be taken down by a
    problem in an unrelated domain, so any exception from `work_engine.for_file()`
    (a store that failed to load, a schema drift) yields an empty
    `offene_arbeit` list rather than propagating."""
    matches = engine.query_for_file_tiered(file_path)
    rules_out = [_rule_summary(r) for r in matches]

    decisions = engine.query_decisions_for_file(file_path)
    decisions_out = [d.model_dump(mode="json") for d in decisions]

    deps = engine.query_dependencies(file_path)

    arbeit_out: list[dict] = []
    if work_engine is not None:
        try:
            for w in work_engine.for_file(file_path):
                arbeit_out.append({
                    "key": w.key, "title": w.title, "status": w.status,
                    "theme": w.theme, "kind": w.kind,
                    "reuse": w.reuse is not None,
                })
        except Exception:
            arbeit_out = []

    return {
        "rules": rules_out,
        "decisions": decisions_out,
        "dependencies": deps,
        "offene_arbeit": arbeit_out,
    }
