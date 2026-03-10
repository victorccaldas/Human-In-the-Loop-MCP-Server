# pyright: reportMissingImports=false

import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import human_loop_server as server
import _telegram_shared_hub as shared_hub


def test_notification_setting_round_trips_to_dialog_config(tmp_path, monkeypatch):
    config_path = tmp_path / "dialog_config.json"
    monkeypatch.setattr(server, "_CONFIG_FILE", str(config_path))

    server._save_remote_input_notifications_enabled(False)
    assert server._get_remote_input_notifications_enabled() is False

    server._save_remote_input_notifications_enabled(True)
    assert server._get_remote_input_notifications_enabled() is True

    stored = json.loads(config_path.read_text(encoding="utf-8"))
    assert stored["remote_input_notifications_enabled"] is True


def test_direct_telegram_notifications_command_updates_config(tmp_path, monkeypatch):
    config_path = tmp_path / "dialog_config.json"
    monkeypatch.setattr(server, "_CONFIG_FILE", str(config_path))
    sent_messages = []

    class _FakeTelegramBridge:
        def send_text(self, text):
            sent_messages.append(text)

    handled = server._try_handle_telegram_admin_command(_FakeTelegramBridge(), "/tkinter_sound_off")

    assert handled is True
    assert sent_messages == ["🔔 Remote-input notifications disabled."]
    assert server._get_remote_input_notifications_enabled() is False


def test_shared_hub_notification_command_updates_config(tmp_path, monkeypatch):
    config_path = tmp_path / "dialog_config.json"
    monkeypatch.setattr(shared_hub, "_DIALOG_CONFIG_FILE", str(config_path))

    reply = shared_hub.handle_remote_input_notification_telegram_command("/tkinter_sound_on")

    assert reply == "🔔 Notificações de get_remote_input ativadas."
    stored = json.loads(config_path.read_text(encoding="utf-8"))
    assert stored["remote_input_notifications_enabled"] is True


def test_legacy_notifications_aliases_are_no_longer_handled(tmp_path, monkeypatch):
    config_path = tmp_path / "dialog_config.json"
    monkeypatch.setattr(server, "_CONFIG_FILE", str(config_path))
    monkeypatch.setattr(shared_hub, "_DIALOG_CONFIG_FILE", str(config_path))

    class _FakeTelegramBridge:
        def send_text(self, text):
            raise AssertionError("Legacy alias should not be handled")

    assert server._try_handle_telegram_admin_command(_FakeTelegramBridge(), "/notifications_off") is False
    assert shared_hub.handle_remote_input_notification_telegram_command("/notifications_on") is None


def test_create_remote_input_dialog_beeps_only_when_enabled(monkeypatch):
    observed = {"beeps": 0}

    class _ImmediateQueue:
        def put(self, callback):
            callback()

    class _FakeDialogWindow:
        def lift(self):
            return None

        def destroy(self):
            return None

    class _FakeMultilineInputDialog:
        def __init__(self, parent, title, prompt, default_value="", done_event=None, **kwargs):
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

    monkeypatch.setattr(server, "_ensure_persistent_root", lambda: object())
    monkeypatch.setattr(server, "_dialog_request_queue", _ImmediateQueue())
    monkeypatch.setattr(server, "MultilineInputDialog", _FakeMultilineInputDialog)
    monkeypatch.setattr(server, "is_telegram_configured", lambda: False)
    monkeypatch.setattr(server, "_play_remote_input_notification_beep", lambda: observed.__setitem__("beeps", observed["beeps"] + 1))

    monkeypatch.setattr(server, "_get_remote_input_notifications_enabled", lambda: True)
    result = server.create_remote_input_dialog("Title", "Prompt")
    assert result == "done"
    assert observed["beeps"] == 1

    monkeypatch.setattr(server, "_get_remote_input_notifications_enabled", lambda: False)
    result = server.create_remote_input_dialog("Title", "Prompt")
    assert result == "done"
    assert observed["beeps"] == 1