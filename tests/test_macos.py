"""Tests for macOS support (no Mac needed: ps/osascript are mocked)."""

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from autocad_mcp import macos

PS_OUTPUT = """\
/sbin/launchd
/Applications/Autodesk/AutoCAD 2025/AutoCAD 2025.app/Contents/MacOS/AutoCAD
/Applications/Autodesk/AutoCAD 2025/AutoCAD 2025.app/Contents/MacOS/AdskLicensingAgent
/Applications/Autodesk/AutoCAD LT 2025/AutoCAD LT 2025.app/Contents/MacOS/AutoCAD LT
/System/Applications/Utilities/Terminal.app/Contents/MacOS/Terminal
"""


class TestParseApps:
    def test_full_and_lt_split(self):
        full, lt = macos.parse_autocad_apps(PS_OUTPUT)
        assert full == ["AutoCAD 2025"]
        assert lt == ["AutoCAD LT 2025"]

    def test_lt_only(self):
        full, lt = macos.parse_autocad_apps(
            "/Applications/AutoCAD LT 2024.app/Contents/MacOS/AutoCAD LT\n"
        )
        assert full == []
        assert lt == ["AutoCAD LT 2024"]

    def test_nothing_running(self):
        assert macos.parse_autocad_apps("/sbin/launchd\n") == ([], [])


class TestFindApp:
    def test_none_off_mac(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        assert macos.find_autocad_app() is None

    def test_finds_full_autocad(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")
        fake = MagicMock(stdout=PS_OUTPUT)
        with patch.object(macos.subprocess, "run", return_value=fake) as run:
            assert macos.find_autocad_app() == "AutoCAD 2025"
            assert run.call_args.args[0] == ["ps", "-axo", "comm="]

    def test_ps_failure(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")
        with patch.object(macos.subprocess, "run", side_effect=OSError("boom")):
            assert macos.find_autocad_app() is None


class TestTriggerScript:
    def test_types_vowel_free_alias(self):
        script = macos.build_trigger_script("AutoCAD 2025")
        assert 'tell application "AutoCAD 2025" to activate' in script
        assert 'keystroke "MCPD"' in script
        assert "key code 36" in script  # Return
        assert script.count("key code 53") == 2  # ESC x2

    def test_alias_has_no_vowels(self):
        # Telex/VNI input sources only transform text that contains vowels
        assert not set(macos.MAC_TRIGGER_COMMAND.lower()) & set("aeiouy")

    def test_restore_focus_optional(self):
        assert "prevApp" in macos.build_trigger_script("AutoCAD 2025")
        no_restore = macos.build_trigger_script("AutoCAD 2025", restore_focus=False)
        assert "set frontmost of application process prevApp" not in no_restore

    def test_app_name_is_quoted(self):
        script = macos.build_trigger_script('Evil" & do shell script "x')
        assert 'tell application "Evil\\" & do shell script \\"x" to activate' in script

    def test_send_trigger_runs_osascript(self):
        fake = MagicMock(returncode=0, stdout="", stderr="")
        with patch.object(macos.subprocess, "run", return_value=fake) as run:
            assert macos.send_trigger("AutoCAD 2025") is True
            assert run.call_args.args[0] == ["osascript", "-"]
            assert 'keystroke "MCPD"' in run.call_args.kwargs["input"]

    def test_send_trigger_reports_failure(self):
        fake = MagicMock(returncode=1, stdout="", stderr="osascript is not allowed assistive access. (-25211)")
        with patch.object(macos.subprocess, "run", return_value=fake):
            assert macos.send_trigger("AutoCAD 2025") is False

    def test_permission_hint(self):
        msg = macos.explain_osascript_error("System Events got an error: not allowed (-1743)")
        assert "Accessibility" in msg


class TestBounds:
    def test_parse(self):
        assert macos.parse_bounds("0, 25, 1440, 875") == (0, 25, 1440, 875)

    def test_parse_bad(self):
        assert macos.parse_bounds("garbage") is None
        assert macos.parse_bounds("0, 0, 0, 100") is None


class TestDetectBackendOnMac:
    def _patch_platform(self, monkeypatch):
        import autocad_mcp.config as cfg

        monkeypatch.setattr(cfg, "WIN32_AVAILABLE", False)
        monkeypatch.setattr(cfg, "MACOS_AVAILABLE", True)
        return cfg

    def test_auto_picks_file_ipc_when_autocad_runs(self, monkeypatch):
        cfg = self._patch_platform(monkeypatch)
        monkeypatch.setenv("AUTOCAD_MCP_BACKEND", "auto")
        with patch("autocad_mcp.macos.find_autocad_app", return_value="AutoCAD 2025"):
            assert cfg.detect_backend() == "file_ipc"

    def test_auto_falls_back_to_ezdxf(self, monkeypatch):
        cfg = self._patch_platform(monkeypatch)
        monkeypatch.setenv("AUTOCAD_MCP_BACKEND", "auto")
        with patch("autocad_mcp.macos.find_autocad_app", return_value=None):
            assert cfg.detect_backend() == "ezdxf"

    def test_explicit_file_ipc_without_autocad_raises(self, monkeypatch):
        cfg = self._patch_platform(monkeypatch)
        monkeypatch.setenv("AUTOCAD_MCP_BACKEND", "file_ipc")
        with patch("autocad_mcp.macos.find_autocad_app", return_value=None):
            with pytest.raises(RuntimeError, match="AutoCAD for Mac"):
                cfg.detect_backend()


class TestFileIPCOnMac:
    async def test_trigger_routes_to_osascript(self):
        from autocad_mcp.backends.file_ipc import FileIPCBackend

        backend = FileIPCBackend()
        backend._mac_app = "AutoCAD 2025"
        with patch("autocad_mcp.macos.send_trigger") as send:
            backend._type_dispatch_trigger()
            send.assert_called_once_with("AutoCAD 2025")

    async def test_initialize_without_autocad(self, monkeypatch):
        from autocad_mcp.backends.file_ipc import FileIPCBackend

        monkeypatch.setattr(sys, "platform", "darwin")
        with patch("autocad_mcp.macos.find_autocad_app", return_value=None):
            result = await FileIPCBackend().initialize()
        assert not result.ok
        assert "window not found" in result.error

    async def test_roundtrip_with_fake_lisp(self, monkeypatch, tmp_path):
        """Full IPC cycle on 'Mac': trigger -> fake dispatcher writes result -> parsed."""
        import json

        from autocad_mcp.backends import file_ipc

        backend = file_ipc.FileIPCBackend()
        backend._ipc_dir = tmp_path
        backend._mac_app = "AutoCAD 2025"

        def fake_trigger(app):
            cmd = next(tmp_path.glob("autocad_mcp_cmd_*.json"))
            req = json.loads(cmd.read_text())
            out = tmp_path / f"autocad_mcp_result_{req['request_id']}.json"
            out.write_text(json.dumps({"request_id": req["request_id"], "ok": True,
                                       "payload": {"echo": req["command"]}}))
            return True

        with patch("autocad_mcp.macos.send_trigger", side_effect=fake_trigger):
            result = await backend._dispatch("ping", {})
        assert result.ok and result.payload == {"echo": "ping"}
        assert not list(tmp_path.iterdir())  # cleaned up


class TestLispMacSupport:
    LSP = (Path(__file__).resolve().parent.parent / "lisp-code" / "mcp_dispatch.lsp").read_text(
        encoding="utf-8"
    )

    def test_mcpd_alias_defined(self):
        assert "(defun c:MCPD () (c:mcp-dispatch))" in self.LSP

    def test_ipc_dir_defaults_match_python(self):
        assert '"/tmp/autocad_mcp/"' in self.LSP
        assert '"C:/temp/"' in self.LSP

    def test_no_activex_calls(self):
        # AutoCAD for Mac has no ActiveX: vla-/vlax-/vlr- must stay out of the dispatcher
        for prefix in ("(vla-", "(vlax-", "(vlr-", "(vl-load-com"):
            assert prefix not in self.LSP
