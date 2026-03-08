# pyright: reportMissingImports=false

import threading
import time
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _telegram_shared_hub import (
    SharedTelegramHubService,
    TelegramCredentials,
    UnsafeMixedTelegramStateError,
    _resolve_hub_launch_command,
    detect_unsafe_direct_polling,
    ensure_shared_telegram_hub,
    get_shared_telegram_mode,
    get_runtime_paths,
    get_host_runtime_root,
)


@pytest.fixture()
def runtime_override(tmp_path, monkeypatch):
    """Keep host-global runtime files isolated per test."""
    monkeypatch.setenv("HITL_MCP_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("HITL_SHARED_TELEGRAM_MODE", "auto")
    return tmp_path


@pytest.fixture()
def credentials() -> TelegramCredentials:
    """Use a deterministic fake bot/chat pair for shared-hub tests."""
    return TelegramCredentials(
        bot_token="123456:test-token",
        chat_id="987654321",
        config_path=None,
        source="test",
    )


def _start_service(credentials: TelegramCredentials) -> SharedTelegramHubService:
    """Start a hub service without the Telegram poller for local HTTP tests."""
    service = SharedTelegramHubService(credentials, port=0, start_poller=False)
    service.start()
    return service


def test_host_runtime_root_uses_override(runtime_override):
    """Runtime files should honor the host-global override environment variable."""
    assert get_host_runtime_root() == str(runtime_override)


def test_shared_mode_defaults_to_auto(monkeypatch):
    """Unset shared-mode env should still use the same-host-safe default."""
    monkeypatch.delenv("HITL_SHARED_TELEGRAM_MODE", raising=False)

    assert get_shared_telegram_mode() == "auto"


def test_shared_client_wait_for_reply_routes_reply(runtime_override, credentials):
    """A waiting client should receive replies that the hub routes for a prompt."""
    service = _start_service(credentials)
    try:
        client = ensure_shared_telegram_hub(credentials=credentials, launcher_script=__file__)
        result = {}
        cancel_event = threading.Event()

        def _wait_for_reply():
            result["text"] = client.wait_for_reply(42, cancel_event)

        waiter = threading.Thread(target=_wait_for_reply, daemon=True)
        waiter.start()
        time.sleep(0.2)
        service.submit_reply(42, "reply from telegram", reply_message_id=1001)
        waiter.join(timeout=3)

        assert result["text"] == "reply from telegram"
    finally:
        service.stop()


def test_complete_prompt_unblocks_waiters(runtime_override, credentials):
    """Local completions should wake waiters without returning a Telegram reply."""
    service = _start_service(credentials)
    try:
        client = ensure_shared_telegram_hub(credentials=credentials, launcher_script=__file__)
        result = {}
        cancel_event = threading.Event()

        def _wait_for_reply():
            result["text"] = client.wait_for_reply(77, cancel_event)

        waiter = threading.Thread(target=_wait_for_reply, daemon=True)
        waiter.start()
        time.sleep(0.2)
        client.complete_prompt(77, status="local_reply", source="tkinter")
        waiter.join(timeout=3)

        assert result["text"] is None
    finally:
        service.stop()


def test_detect_unsafe_direct_polling_when_hub_is_running(runtime_override, credentials, monkeypatch):
    """Legacy direct polling must fail clearly when a shared hub is already active."""
    service = _start_service(credentials)
    monkeypatch.setenv("HITL_SHARED_TELEGRAM_MODE", "off")
    try:
        with pytest.raises(UnsafeMixedTelegramStateError):
            detect_unsafe_direct_polling(credentials)
    finally:
        service.stop()


def test_ensure_shared_hub_reuses_existing_descriptor(runtime_override, credentials, monkeypatch):
    """Auto-discovery should reuse a healthy hub instead of launching another owner."""
    service = _start_service(credentials)
    try:
        def _unexpected_launch(*args, **kwargs):
            raise AssertionError("launcher should not run when descriptor is healthy")

        monkeypatch.setattr("_telegram_shared_hub._launch_hub_subprocess", _unexpected_launch)
        client = ensure_shared_telegram_hub(credentials=credentials, launcher_script=__file__)
        assert client.descriptor.port == service.port
        assert get_runtime_paths(credentials).descriptor_path.endswith("hub-descriptor.json")
    finally:
        service.stop()


def test_resolve_hub_launch_command_uses_module_for_console_script(monkeypatch):
    """Installed console-script launches should relaunch via the importable module."""
    monkeypatch.setattr(sys, "argv", ["hitl-mcp-server.exe"])
    monkeypatch.setattr(
        "_telegram_shared_hub.importlib.util.find_spec",
        lambda name: SimpleNamespace(name=name),
    )

    command, cwd = _resolve_hub_launch_command(None)

    assert command == [sys.executable, "-m", "human_loop_server"]
    assert cwd == str(Path(__file__).resolve().parents[1])


def test_resolve_hub_launch_command_prefers_explicit_python_script(monkeypatch):
    """Direct .py launches should preserve the explicit launcher script path."""
    monkeypatch.setattr(
        "_telegram_shared_hub.importlib.util.find_spec",
        lambda name: SimpleNamespace(name=name),
    )

    command, cwd = _resolve_hub_launch_command(__file__)

    assert command == [sys.executable, __file__]
    assert cwd == str(Path(__file__).resolve().parent)
