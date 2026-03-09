import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import human_loop_server as server
import _telegram_shared_hub as shared_hub
from _telegram_shared_hub import SharedTelegramHubService, TelegramCredentials
from _telegram_shared_hub import toggle_vscode_auto_message_setting


@pytest.fixture()
def credentials() -> TelegramCredentials:
    """Use a deterministic fake bot/chat pair for Telegram admin command tests."""
    return TelegramCredentials(
        bot_token="123456:test-token",
        chat_id="987654321",
        config_path=None,
        source="test",
    )


def _write_local_config(config_path: Path, settings_path: Path) -> None:
    """Create the local-only config file consumed by the Telegram admin commands."""
    config_path.write_text(
        json.dumps({"vscode_settings_path": str(settings_path)}, indent=2),
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    ("block_automatic_messages", "initial_key", "target_key"),
    [
        (True, "_chat.tools.eligibleForAutoApproval", "chat.tools.eligibleForAutoApproval"),
        (False, "chat.tools.eligibleForAutoApproval", "_chat.tools.eligibleForAutoApproval"),
    ],
)
def test_toggle_vscode_auto_message_setting_renames_only_target_key(
    tmp_path,
    block_automatic_messages,
    initial_key,
    target_key,
):
    """The toggle should rename only the targeted settings key and preserve file shape."""
    config_path = tmp_path / "vscode-auto-messages.local.json"
    settings_path = tmp_path / "settings.json"
    _write_local_config(config_path, settings_path)
    settings_path.write_text(
        "{\n"
        "  // keep formatting intact\n"
        f'  "{initial_key}": true,\n'
        '  "window.zoomLevel": 0\n'
        "}\n",
        encoding="utf-8",
    )

    result = toggle_vscode_auto_message_setting(
        block_automatic_messages=block_automatic_messages,
        local_config_path=str(config_path),
    )

    assert result["status"] == "success"
    updated_content = settings_path.read_text(encoding="utf-8")
    assert f'"{target_key}": true' in updated_content
    assert f'"{initial_key}": true' not in updated_content
    assert '"window.zoomLevel": 0' in updated_content
    assert "// keep formatting intact" in updated_content


def test_toggle_vscode_auto_message_setting_reports_already_in_target_state(tmp_path):
    """A no-op toggle should report the existing desired state without writing."""
    config_path = tmp_path / "vscode-auto-messages.local.json"
    settings_path = tmp_path / "settings.json"
    _write_local_config(config_path, settings_path)
    settings_path.write_text(
        '{\n  "chat.tools.eligibleForAutoApproval": true\n}\n',
        encoding="utf-8",
    )

    result = toggle_vscode_auto_message_setting(
        block_automatic_messages=True,
        local_config_path=str(config_path),
    )

    assert result["status"] == "already-in-target-state"


def test_toggle_vscode_auto_message_setting_reports_missing_local_config(tmp_path, monkeypatch):
    """The Telegram status layer should clearly report when the local config is missing."""
    missing_config = tmp_path / "does-not-exist.json"
    monkeypatch.setattr(
        shared_hub,
        "get_auto_messages_local_config_path",
        lambda: str(missing_config),
    )

    reply = shared_hub.handle_vscode_auto_message_telegram_command(
        "/bloquear_mensagens_automaticas",
    )

    assert reply is not None
    assert "Configuração local ausente" in reply
    assert str(missing_config) in reply


def test_toggle_vscode_auto_message_setting_reports_conflict_when_both_keys_exist(tmp_path):
    """The toggle should refuse to touch ambiguous settings content."""
    config_path = tmp_path / "vscode-auto-messages.local.json"
    settings_path = tmp_path / "settings.json"
    _write_local_config(config_path, settings_path)
    settings_path.write_text(
        "{\n"
        '  "_chat.tools.eligibleForAutoApproval": true,\n'
        '  "chat.tools.eligibleForAutoApproval": true\n'
        "}\n",
        encoding="utf-8",
    )

    result = toggle_vscode_auto_message_setting(
        block_automatic_messages=True,
        local_config_path=str(config_path),
    )

    assert result["status"] == "conflict"
    assert result["reason"] == "both_keys_present"


def test_try_handle_telegram_admin_command_sends_auto_message_reply(monkeypatch):
    """Direct-mode pollers should route the new command replies through Telegram."""
    sent_messages = []

    class _FakeTelegramBridge:
        def send_text(self, text):
            sent_messages.append(text)

    monkeypatch.setattr(
        server,
        "handle_vscode_auto_message_telegram_command",
        lambda text: f"reply for {text}",
    )

    handled = server._try_handle_telegram_admin_command(
        _FakeTelegramBridge(),
        "/permitir_mensagens_automaticas",
    )

    assert handled is True
    assert sent_messages == ["reply for /permitir_mensagens_automaticas"]


def test_shared_hub_process_update_routes_auto_message_command(credentials, monkeypatch):
    """Shared/default mode should answer the new Telegram command through the hub owner."""
    sent_messages = []
    service = SharedTelegramHubService(credentials, port=0, start_poller=False)
    monkeypatch.setattr(
        "_telegram_shared_hub.handle_vscode_auto_message_telegram_command",
        lambda text: f"shared reply for {text}",
    )
    monkeypatch.setattr(service, "_send_text", sent_messages.append)

    service._process_update(
        {
            "message": {
                "chat": {"id": credentials.chat_id},
                "text": "/bloquear_mensagens_automaticas",
                "message_id": 501,
            }
        }
    )

    assert sent_messages == ["shared reply for /bloquear_mensagens_automaticas"]
