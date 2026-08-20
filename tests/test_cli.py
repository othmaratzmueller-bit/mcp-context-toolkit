import json

from mcp_context_toolkit.cli import _cmd_doctor, _cmd_validate_all


class TestCLIDiagnostics:
    def test_cmd_doctor_text(self, capsys):
        rc = _cmd_doctor(json_format=False)
        assert rc == 0
        captured = capsys.readouterr().out
        assert "=== mcp-context-toolkit doctor ===" in captured
        assert "Rules Store:" in captured

    def test_cmd_doctor_json(self, capsys):
        rc = _cmd_doctor(json_format=True)
        assert rc == 0
        captured = capsys.readouterr().out
        data = json.loads(captured)
        assert data["status"] == "ok"
        assert "stores" in data

    def test_cmd_validate_all(self, capsys):
        rc = _cmd_validate_all()
        assert rc == 0
        captured = capsys.readouterr().out
        data = json.loads(captured)
        assert data["ok"] is True
