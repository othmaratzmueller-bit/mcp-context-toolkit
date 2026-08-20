from pathlib import Path

from mcp_context_toolkit.docs import DocsEngine, _split_sections


def _write_doc(d: Path, rel: str, body: str) -> None:
    p = d / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


class TestSplitSections:
    def test_single_heading(self):
        sections = _split_sections("a.md", "# Titel\n\nText darunter.\n")
        assert len(sections) == 1
        assert sections[0].heading == "Titel"
        assert sections[0].text == "Text darunter."
        assert sections[0].line == 1

    def test_multiple_headings_split_into_multiple_sections(self):
        text = "# Erste\n\nErster Text.\n\n## Zweite\n\nZweiter Text.\n"
        sections = _split_sections("a.md", text)
        assert [s.heading for s in sections] == ["Erste", "Zweite"]
        assert sections[1].line == 5

    def test_lead_in_before_first_heading_becomes_its_own_section(self):
        text = "Einleitung ohne Heading.\n\n# Erste Ueberschrift\n\nText.\n"
        sections = _split_sections("a.md", text)
        assert sections[0].heading == ""
        assert sections[0].text == "Einleitung ohne Heading."
        assert sections[1].heading == "Erste Ueberschrift"

    def test_no_heading_at_all_is_one_lead_in_section(self):
        sections = _split_sections("a.md", "Nur Fliesstext, keine Ueberschrift.\n")
        assert len(sections) == 1
        assert sections[0].heading == ""

    def test_empty_file_yields_no_sections(self):
        assert _split_sections("a.md", "") == []

    def test_heading_with_no_body_is_dropped(self):
        # A trailing "## Naechste" heading with nothing under it yet must not
        # produce a phantom empty section.
        text = "# Titel\n\nText.\n\n## Leer\n"
        sections = _split_sections("a.md", text)
        assert [s.heading for s in sections] == ["Titel"]

    def test_hash_comment_inside_code_fence_is_not_a_heading(self):
        # W8-Reviewer-Fund WARNING-1, Etappe F: measured 12.0% of all
        # heading-regex hits across the real docs/ tree came from inside a
        # fenced code block (shell/HTTP examples with a "#"-commented line).
        text = (
            "# Echte Ueberschrift\n\n"
            "Text davor.\n\n"
            "```bash\n"
            "# Das ist ein Shell-Kommentar, keine Markdown-Ueberschrift\n"
            "curl -X POST /r/client/exec\n"
            "```\n\n"
            "Text danach, gehoert noch zur echten Ueberschrift.\n"
        )
        sections = _split_sections("a.md", text)
        assert [s.heading for s in sections] == ["Echte Ueberschrift"]
        assert "Shell-Kommentar" in sections[0].text
        assert "Text danach" in sections[0].text

    def test_tilde_fence_also_suppresses_headings(self):
        text = "# Titel\n\n~~~\n# kein Heading\n~~~\nText.\n"
        sections = _split_sections("a.md", text)
        assert [s.heading for s in sections] == ["Titel"]

    def test_unclosed_fence_suppresses_headings_to_end_of_file(self):
        # No matching closing fence — degrade to "everything after the open
        # stays inside the fence", never crash or mis-split.
        text = "# Titel\n\n```\n# kein Heading\nweiterer Text\n"
        sections = _split_sections("a.md", text)
        assert [s.heading for s in sections] == ["Titel"]


class TestDocsEngine:
    def test_loads_sections_from_markdown_files(self, tmp_path: Path):
        _write_doc(tmp_path, "guide.md", "# Guide\n\nErklaerung hier.\n")
        e = DocsEngine.from_directory(tmp_path)
        assert len(e.sections) == 1
        assert e.sections[0].path == "guide.md"

    def test_ignores_non_markdown_files(self, tmp_path: Path):
        _write_doc(tmp_path, "guide.md", "# Guide\n\nText.\n")
        (tmp_path / "notes.txt").write_text("kein Markdown", encoding="utf-8")
        e = DocsEngine.from_directory(tmp_path)
        assert all(s.path.endswith(".md") for s in e.sections)

    def test_recurses_subdirectories(self, tmp_path: Path):
        _write_doc(tmp_path, "governance/policy.md", "# Policy\n\nRegeln.\n")
        e = DocsEngine.from_directory(tmp_path)
        assert e.sections[0].path == "governance/policy.md"

    def test_missing_directory_raises(self, tmp_path: Path):
        import pytest
        with pytest.raises(FileNotFoundError):
            DocsEngine.from_directory(tmp_path / "does-not-exist")

    def test_recall_finds_matching_section_by_heading(self, tmp_path: Path):
        _write_doc(tmp_path, "a.md", "# Datenbank-Migration\n\nText ueber Schema-Aenderungen.\n")
        _write_doc(tmp_path, "b.md", "# Frontend-Design\n\nText ueber CSS-Tokens.\n")
        e = DocsEngine.from_directory(tmp_path)
        hits = e.recall("Datenbank Migration Schema")
        assert hits
        assert hits[0].path == "a.md"

    def test_recall_returns_citation_with_line_number(self, tmp_path: Path):
        _write_doc(tmp_path, "a.md", "# Erste\n\nText.\n\n## Sicherheitsarchitektur\n\nDetails zur Sicherheit.\n")
        e = DocsEngine.from_directory(tmp_path)
        hits = e.recall("Sicherheitsarchitektur Details")
        assert hits[0].heading == "Sicherheitsarchitektur"
        assert hits[0].line == 5

    def test_recall_no_match_returns_empty(self, tmp_path: Path):
        _write_doc(tmp_path, "a.md", "# Titel\n\nText.\n")
        e = DocsEngine.from_directory(tmp_path)
        assert e.recall("voellig_unbekannter_begriff_xyz") == []

    def test_recall_respects_limit(self, tmp_path: Path):
        for i in range(5):
            _write_doc(tmp_path, f"doc{i}.md", f"# Thema {i}\n\nGemeinsamer Suchbegriff wiederholt.\n")
        e = DocsEngine.from_directory(tmp_path)
        assert len(e.recall("Gemeinsamer Suchbegriff", limit=2)) == 2

    def test_recall_drops_weak_tail_below_floor(self, tmp_path: Path):
        # Strong multi-term match vs. a single incidental-term match — the
        # weak one should fall below the relevance floor (same mechanism as
        # memory.py/work.py's own floor tests).
        _write_doc(tmp_path, "strong.md", "# Ziel\n\nZiel Ziel Ziel Ziel Ziel Suchbegriff Suchbegriff.\n")
        _write_doc(tmp_path, "weak.md", "# Anderes\n\nEin Satz der nur einmal Suchbegriff erwaehnt.\n")
        e = DocsEngine.from_directory(tmp_path)
        hits = e.recall("Ziel Suchbegriff")
        paths = [h.path for h in hits]
        assert "strong.md" in paths

    def test_recall_clamps_negative_limit(self, tmp_path: Path):
        # W8-Reviewer-Fund ERROR-1, Etappe F: an LLM-supplied `limit=-1` is a
        # valid Python slice (from-the-end) — measured 1,954 sections
        # (~1.44M tokens) returned for a single call against the real
        # corpus before this clamp existed.
        for i in range(10):
            _write_doc(tmp_path, f"doc{i}.md", f"# Thema {i}\n\nGemeinsamer Suchbegriff wiederholt.\n")
        e = DocsEngine.from_directory(tmp_path)
        hits = e.recall("Gemeinsamer Suchbegriff", limit=-1)
        assert len(hits) >= 1
        assert len(hits) <= 10

    def test_recall_clamps_excessive_limit(self, tmp_path: Path):
        _write_doc(tmp_path, "a.md", "# Thema\n\nSuchbegriff.\n")
        e = DocsEngine.from_directory(tmp_path)
        # Must not raise, must not silently claim more than exists.
        assert len(e.recall("Suchbegriff", limit=10_000)) == 1

    def test_load_directory_skips_dated_snapshot_directories(self, tmp_path: Path):
        # W8-Reviewer-Fund WARNING-2, Etappe F: a frozen audit snapshot
        # (governance/audit/<date>/...) ranking equal to the live document
        # it was copied from means an agent can cite stale governance as
        # current.
        _write_doc(tmp_path, "governance/policy.md", "# Policy\n\nAktueller Text.\n")
        _write_doc(tmp_path, "governance/audit/2026-06-01/policy.md",
                   "# Policy\n\nEingefrorener Text vom Snapshot.\n")
        e = DocsEngine.from_directory(tmp_path)
        paths = [s.path for s in e.sections]
        assert "governance/policy.md" in paths
        assert not any("2026-06-01" in p for p in paths)

    def test_load_directory_skips_archive_directories(self, tmp_path: Path):
        _write_doc(tmp_path, "guide.md", "# Guide\n\nAktuell.\n")
        _write_doc(tmp_path, "archive/2026-01-01-cleanup/old-guide.md", "# Alt\n\nVeraltet.\n")
        e = DocsEngine.from_directory(tmp_path)
        paths = [s.path for s in e.sections]
        assert "guide.md" in paths
        assert not any("archive" in p for p in paths)

    def test_load_directory_does_not_exclude_eval_archive(self, tmp_path: Path):
        # W8-Reviewer-Fund ERROR-2, Etappe F: "archive" as a SUBSTRING match
        # wrongly excluded eval-archive/ too — the deliberate, actively-used
        # drop location for eval results (feedback_eval_archive_output_
        # location), not a frozen duplicate of anything live.
        _write_doc(tmp_path, "eval-archive/2026-08-01-run/report.md", "# Bericht\n\nErgebnis.\n")
        e = DocsEngine.from_directory(tmp_path)
        paths = [s.path for s in e.sections]
        assert any("eval-archive" in p for p in paths)

    def test_load_directory_skips_draft_decision_bundles(self, tmp_path: Path):
        # W8-Reviewer-Fund WARNING-4, Etappe F, Owner-decision 2026-08-20:
        # an unsigned "Entscheidung <...>/" draft bundle outranked the live
        # governance doc it drafts a revision of.
        _write_doc(tmp_path, "governance/policy.md", "# Policy\n\nAktueller Text.\n")
        _write_doc(tmp_path, "Entscheidung GF 01.07.2026/governance-neufassung/policy.md",
                   "# Policy\n\nEntwurfstext, noch nicht unterzeichnet.\n")
        e = DocsEngine.from_directory(tmp_path)
        paths = [s.path for s in e.sections]
        assert "governance/policy.md" in paths
        assert not any("Entscheidung" in p for p in paths)

    def test_load_directory_reload_replaces_not_accumulates(self, tmp_path: Path):
        # W8-Reviewer-Fund WARNING-3, Etappe F: a second load_directory()
        # call on the same instance used to APPEND, double-counting every
        # section on reload.
        _write_doc(tmp_path, "a.md", "# Titel\n\nText.\n")
        e = DocsEngine()
        e.load_directory(tmp_path)
        assert len(e.sections) == 1
        e.load_directory(tmp_path)
        assert len(e.sections) == 1
