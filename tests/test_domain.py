from pathlib import Path

from mcp_context_toolkit.domain import DomainEngine, parse_domain_manifest


class TestDomainMechanics:
    def test_parse_domain_manifest(self, tmp_path: Path):
        m_file = tmp_path / "domain.yaml"
        m_file.write_text(
            "id: honey_bunny\n"
            "title: Honey Bunny Domain\n"
            "profile: memory\n"
            "trigger:\n"
            "  keywords: [bunny, honey]\n"
            "attributes:\n"
            "  risk_level: low\n",
            encoding="utf-8",
        )
        manifest = parse_domain_manifest(m_file)
        assert manifest.id == "honey_bunny"
        assert manifest.title == "Honey Bunny Domain"
        assert manifest.profile == "memory"
        assert manifest.trigger.keywords == ["bunny", "honey"]
        assert manifest.attributes == {"risk_level": "low"}

    def test_discover_domains(self, tmp_path: Path):
        dom_dir = tmp_path / ".context" / "domains" / "honey-bunny"
        dom_dir.mkdir(parents=True, exist_ok=True)
        (dom_dir / "domain.yaml").write_text(
            "id: honey_bunny\n"
            "title: Honey Bunny\n"
            "profile: memory\n",
            encoding="utf-8",
        )
        engine = DomainEngine.discover_domains(tmp_path)
        domains = engine.list_domains()
        assert len(domains) == 1
        assert domains[0]["id"] == "honey_bunny"
        assert engine.get_domain("honey_bunny") is not None
