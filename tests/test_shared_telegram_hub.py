# pyright: reportMissingImports=false

import threading
import time
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _telegram_bridge import TelegramBridge
from _telegram_shared_hub import (
    SharedHubDescriptor,
    SharedTelegramHubService,
    TelegramCredentials,
    UnsafeMixedTelegramStateError,
    _HUB_VERSION,
    _write_descriptor,
    _resolve_hub_launch_command,
    detect_unsafe_direct_polling,
    ensure_shared_telegram_hub,
    find_running_hub_descriptor,
    get_host_runtime_root,
    get_shared_telegram_mode,
    get_runtime_paths,
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


def test_pending_descriptor_is_not_pruned_before_hub_readiness(runtime_override, credentials, monkeypatch):
    """Reachable-but-booting hubs must keep their descriptor until ready."""
    paths = get_runtime_paths(credentials)
    descriptor = SharedHubDescriptor(
        version=_HUB_VERSION,
        host="127.0.0.1",
        port=43123,
        pid=99999,
        scope_key=credentials.scope_key,
        started_at="2026-03-08T00:00:00+00:00",
        script_path=__file__,
        runtime_scope_dir=paths.scope_dir,
    )
    _write_descriptor(descriptor, paths.descriptor_path)

    class _PendingResponse:
        status_code = 200

        def json(self):
            return {
                "ok": False,
                "ready": False,
                "scope_key": credentials.scope_key,
                "http_ready": True,
                "poller_expected": True,
                "poller_ready": False,
            }

    monkeypatch.setattr(
        "_telegram_shared_hub.requests.get",
        lambda *args, **kwargs: _PendingResponse(),
    )

    assert find_running_hub_descriptor(credentials) is None
    assert Path(paths.descriptor_path).is_file()


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



def test_shared_client_wait_for_reply_details_preserves_source_metadata(runtime_override, credentials):
    """Shared-hub replies should preserve source metadata for higher-level labels."""
    service = _start_service(credentials)
    try:
        client = ensure_shared_telegram_hub(credentials=credentials, launcher_script=__file__)
        cancel_event = threading.Event()

        service.submit_reply(
            88,
            "reply from telegram",
            reply_message_id=2002,
            source="telegram_reply",
        )

        details = client.wait_for_reply_details(88, cancel_event)

        assert details is not None
        assert details["text"] == "reply from telegram"
        assert details["source"] == "telegram_reply"
        assert isinstance(details["received_at"], int)
    finally:
        service.stop()


def test_shared_hub_health_reports_readiness_fields(runtime_override, credentials):
    """Health output should expose readiness details instead of a plain green flag."""
    service = _start_service(credentials)
    try:
        client = ensure_shared_telegram_hub(credentials=credentials, launcher_script=__file__)
        health = client.health()

        assert health["ok"] is True
        assert health["ready"] is True
        assert health["http_ready"] is True
        assert health["poller_expected"] is False
        assert health["poller_ready"] is True
        assert health["last_poll_error"] is None
    finally:
        service.stop()


def test_shared_hub_reacts_only_to_first_accepted_direct_reply(credentials, monkeypatch):
    """The hub should restore the thumbs-up ack only for the accepted direct reply."""
    reaction_calls = []

    def _fake_post(url, json=None, timeout=None):
        reaction_calls.append((url, json, timeout))
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr("_telegram_shared_hub.requests.post", _fake_post)

    service = SharedTelegramHubService(credentials, port=0, start_poller=False)
    service.complete_prompt(51, "local_reply")
    service._process_update(
        {
            "message": {
                "chat": {"id": credentials.chat_id},
                "text": "too late",
                "message_id": 7001,
                "reply_to_message": {"message_id": 51},
            }
        }
    )
    service._process_update(
        {
            "message": {
                "chat": {"id": credentials.chat_id},
                "text": "accepted",
                "message_id": 7002,
                "reply_to_message": {"message_id": 52},
            }
        }
    )

    assert len(reaction_calls) == 1
    assert reaction_calls[0][0].endswith("/setMessageReaction")
    assert reaction_calls[0][1]["message_id"] == 7002


def test_shared_poll_for_answer_details_prefers_miniapp_queue_over_later_hub_reply():
    """Mini App answers should win in shared mode even if the hub reply arrives later."""
    bridge = object.__new__(TelegramBridge)
    bridge._shared_hub_client = SimpleNamespace()

    hub_release = threading.Event()

    def _wait_for_reply_details(prompt_message_id, cancel_event, poll_interval=0.3):
        assert prompt_message_id == 99
        assert poll_interval == 1.5
        hub_release.wait(timeout=2.0)
        return {
            "text": "reply from telegram",
            "source": "telegram_reply",
            "received_at": 200,
        }

    bridge._shared_hub_client.wait_for_reply_details = _wait_for_reply_details

    cancel_event = threading.Event()
    answer_queue = __import__("queue").SimpleQueue()
    answer_queue.put(
        {
            "text": "reply from mini app",
            "source": "telegram_miniapp",
            "received_at": 100,
        }
    )

    hub_release.set()
    details = bridge.poll_for_answer_details(99, cancel_event, answer_queue=answer_queue)

    assert details is not None
    assert details["text"] == "reply from mini app"
    assert details["source"] == "telegram_miniapp"


@pytest.mark.parametrize("shared_mode", [False, True])
def test_send_prompt_pins_message_in_direct_and_shared_modes(monkeypatch, shared_mode):
    """Prompt sends should pin the new Telegram message in both operating modes."""
    calls = []

    class _FakeResponse:
        def __init__(self, status_code, payload=None, text=""):
            self.status_code = status_code
            self._payload = payload or {}
            self.text = text

        def json(self):
            return self._payload

    def _fake_post(url, json=None, timeout=None):
        calls.append((url, json, timeout))
        if url.endswith("/sendMessage"):
            return _FakeResponse(200, {"result": {"message_id": 9001}})
        if url.endswith("/pinChatMessage"):
            return _FakeResponse(200, {"ok": True})
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr("_telegram_bridge.requests.post", _fake_post)

    bridge = object.__new__(TelegramBridge)
    bridge.bot_token = "123:token"
    bridge.chat_id = "456"
    bridge._update_offset = None
    bridge._shared_hub_client = SimpleNamespace() if shared_mode else None

    message_id = bridge.send_prompt("Title", "Prompt")

    assert message_id == 9001
    assert [call[0].rsplit("/", 1)[-1] for call in calls] == [
        "sendMessage",
        "pinChatMessage",
    ]
    assert calls[1][1]["message_id"] == 9001


@pytest.mark.parametrize("shared_mode", [False, True])
def test_send_prompt_with_miniapp_pins_message_in_direct_and_shared_modes(monkeypatch, shared_mode):
    """Mini App prompt sends should pin the new Telegram message in both operating modes."""
    calls = []

    class _FakeResponse:
        def __init__(self, status_code, payload=None, text=""):
            self.status_code = status_code
            self._payload = payload or {}
            self.text = text

        def json(self):
            return self._payload

    def _fake_post(url, json=None, timeout=None):
        calls.append((url, json, timeout))
        if url.endswith("/sendMessage"):
            return _FakeResponse(200, {"result": {"message_id": 9002}})
        if url.endswith("/pinChatMessage"):
            return _FakeResponse(200, {"ok": True})
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr("_telegram_bridge.requests.post", _fake_post)

    bridge = object.__new__(TelegramBridge)
    bridge.bot_token = "123:token"
    bridge.chat_id = "456"
    bridge._update_offset = None
    bridge._shared_hub_client = SimpleNamespace() if shared_mode else None

    message_id = bridge.send_prompt_with_miniapp(
        "Title",
        "Prompt",
        "https://example.invalid/app",
        "Agent",
    )

    assert message_id == 9002
    assert [call[0].rsplit("/", 1)[-1] for call in calls] == [
        "sendMessage",
        "pinChatMessage",
    ]
    assert calls[0][1]["reply_markup"]["inline_keyboard"][0][0]["web_app"]["url"] == "https://example.invalid/app"
    assert calls[1][1]["message_id"] == 9002


def test_unpin_message_is_best_effort_in_shared_mode(monkeypatch):
    """Shared-mode cleanup should tolerate Telegram unpin rejections without raising."""

    class _FakeResponse:
        status_code = 400
        text = "message is not pinned"

    monkeypatch.setattr(
        "_telegram_bridge.requests.post",
        lambda url, json=None, timeout=None: _FakeResponse(),
    )

    bridge = object.__new__(TelegramBridge)
    bridge.bot_token = "123:token"
    bridge.chat_id = "456"
    bridge._update_offset = None
    bridge._shared_hub_client = SimpleNamespace()

    assert bridge.unpin_message(9001) is False


def test_resolve_hub_launch_command_prefers_explicit_python_script(monkeypatch):
    """Direct .py launches should preserve the explicit launcher script path."""
    monkeypatch.setattr(
        "_telegram_shared_hub.importlib.util.find_spec",
        lambda name: SimpleNamespace(name=name),
    )

    command, cwd = _resolve_hub_launch_command(__file__)

    assert command == [sys.executable, __file__]
    assert cwd == str(Path(__file__).resolve().parent)
