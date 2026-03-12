# pyright: reportMissingImports=false

import os
import tempfile
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import human_loop_server as server


@pytest.mark.asyncio
async def test_get_remote_input_rejects_missing_attachment(monkeypatch):
    called = {"dialog": False}

    def _fake_dialog(*args, **kwargs):
        called["dialog"] = True
        return "unexpected"

    monkeypatch.setattr(server, "_check_bypass", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "create_remote_input_dialog", _fake_dialog)

    result = await server.get_remote_input.fn(
        "Title",
        "Prompt",
        file_path="./missing-file-does-not-exist.txt",
    )

    assert result["success"] is False
    assert "does not exist" in result["error"]
    assert called["dialog"] is False


@pytest.mark.asyncio
async def test_get_remote_input_rejects_oversized_attachment(monkeypatch):
    with tempfile.NamedTemporaryFile(delete=False) as temp_file:
        temp_path = temp_file.name

    try:
        monkeypatch.setattr(server, "_check_bypass", lambda *args, **kwargs: None)
        monkeypatch.setattr(server, "_TELEGRAM_BOT_FILE_LIMIT_BYTES", 16)
        monkeypatch.setattr(server.os.path, "getsize", lambda _path: 32)

        result = await server.get_remote_input.fn(
            "Title",
            "Prompt",
            file_path=temp_path,
        )

        assert result["success"] is False
        assert "50 MB bot limit" in result["error"]
    finally:
        os.unlink(temp_path)


def test_create_remote_input_dialog_passes_attachment_to_gui_dialog(monkeypatch):
    observed = {}

    class _ImmediateQueue:
        def put(self, callback):
            callback()

    class _FakeDialogWindow:
        def lift(self):
            return None

        def destroy(self):
            return None

    class _FakeMultilineInputDialog:
        def __init__(self, parent, title, prompt, default_value="", done_event=None, attachment_path=None, **kwargs):
            observed["attachment_path"] = attachment_path
            self.result = "done"
            self._done_event = done_event
            self._reminder_id = None
            self.dialog = _FakeDialogWindow()
            self.main_frame = object()
            self.theme_colors = {
                "bg_secondary": "#fff",
                "fg_secondary": "#111",
                "accent_color": "#06c",
                "success_color": "#137333",
            }
            if self._done_event is not None:
                self._done_event.set()

    with tempfile.NamedTemporaryFile(delete=False) as temp_file:
        temp_path = temp_file.name

    try:
        monkeypatch.setattr(server, "_ensure_persistent_root", lambda: object())
        monkeypatch.setattr(server, "_dialog_request_queue", _ImmediateQueue())
        monkeypatch.setattr(server, "MultilineInputDialog", _FakeMultilineInputDialog)
        monkeypatch.setattr(server, "is_telegram_configured", lambda: False)

        result = server.create_remote_input_dialog("Title", "Prompt", file_path=temp_path)

        assert result == "done"
        assert observed["attachment_path"] == temp_path
    finally:
        os.unlink(temp_path)


def test_open_path_in_file_browser_uses_windows_explorer(monkeypatch):
    observed = {}

    monkeypatch.setattr(server, "IS_WINDOWS", True)
    monkeypatch.setattr(server, "IS_MACOS", False)
    monkeypatch.setattr(server, "IS_LINUX", False)

    def _fake_popen(command):
        observed["command"] = command
        class _Process:
            pass
        return _Process()

    monkeypatch.setattr(server.subprocess, "Popen", _fake_popen)

    assert server._open_path_in_file_browser(r"C:\temp\example.txt") is True
    assert observed["command"][0] == "explorer"
    assert observed["command"][1].startswith("/select,")