# pyright: reportMissingImports=false

import asyncio
import threading
import time
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import human_loop_server as server


class _ImmediateQueue:
    """Execute queued GUI callbacks synchronously for deterministic tests."""

    def put(self, callback):
        callback()


@pytest.mark.asyncio
async def test_get_remote_input_cancellation_re_raises_and_signals_worker(monkeypatch):
    """Transport cancellation should signal the worker thread before re-raising."""
    worker_started = threading.Event()
    worker_cleanup = threading.Event()
    captured = {}

    def _fake_remote_input_dialog(
        title,
        prompt,
        default_value="",
        name_or_role="",
        external_cancel=None,
    ):
        captured["external_cancel"] = external_cancel
        worker_started.set()
        assert external_cancel is not None
        if external_cancel.wait(timeout=2.0):
            worker_cleanup.set()
        return None

    monkeypatch.setattr(server, "_check_bypass", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "ensure_gui_initialized", lambda: True)
    monkeypatch.setattr(server, "is_telegram_configured", lambda: False)
    monkeypatch.setattr(server, "create_remote_input_dialog", _fake_remote_input_dialog)

    # Access .fn to get the underlying async function from the FunctionTool wrapper
    task = asyncio.create_task(server.get_remote_input.fn("Title", "Prompt"))
    await asyncio.to_thread(worker_started.wait, 1.0)

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert worker_cleanup.wait(1.0)
    assert isinstance(captured["external_cancel"], threading.Event)


@pytest.mark.asyncio
async def test_get_remote_input_cancellation_does_not_cancel_sibling_request(monkeypatch):
    """Cancelling one request should not propagate transport cancellation to another."""
    worker_started = threading.Event()
    release_second = threading.Event()
    captured_events = {}

    def _fake_remote_input_dialog(
        title,
        prompt,
        default_value="",
        name_or_role="",
        external_cancel=None,
    ):
        assert external_cancel is not None
        captured_events[prompt] = external_cancel
        worker_started.set()
        if prompt == "first":
            external_cancel.wait(timeout=2.0)
            return None
        release_second.wait(timeout=2.0)
        if external_cancel.is_set():
            return "unexpected-cancel"
        return "second result"

    monkeypatch.setattr(server, "_check_bypass", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "ensure_gui_initialized", lambda: True)
    monkeypatch.setattr(server, "is_telegram_configured", lambda: False)
    monkeypatch.setattr(server, "create_remote_input_dialog", _fake_remote_input_dialog)

    first_task = asyncio.create_task(server.get_remote_input.fn("Title", "first"))
    second_task = asyncio.create_task(server.get_remote_input.fn("Title", "second"))

    await asyncio.to_thread(worker_started.wait, 1.0)
    await asyncio.sleep(0.05)

    first_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first_task

    release_second.set()
    second_result = await second_task

    assert captured_events["first"].is_set() is True
    assert captured_events["second"].is_set() is False
    assert second_result["success"] is True
    assert second_result["user_input"] == "second result"


@pytest.mark.asyncio
async def test_get_remote_input_cancellation_waits_for_bounded_worker_cleanup(monkeypatch):
    """Cancellation should give the worker a chance to release shared resources before returning."""
    resource_lock = threading.Lock()
    first_started = threading.Event()
    timings = {}

    def _fake_remote_input_dialog(
        title,
        prompt,
        default_value="",
        name_or_role="",
        external_cancel=None,
    ):
        assert external_cancel is not None
        if prompt == "first":
            assert resource_lock.acquire(blocking=False)
            first_started.set()
            external_cancel.wait(timeout=2.0)
            timings["cleanup_started"] = time.monotonic()
            time.sleep(0.3)
            resource_lock.release()
            timings["cleanup_finished"] = time.monotonic()
            return None

        acquired = resource_lock.acquire(blocking=False)
        try:
            return "second result" if acquired else "resource busy"
        finally:
            if acquired:
                resource_lock.release()

    monkeypatch.setattr(server, "_check_bypass", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "ensure_gui_initialized", lambda: True)
    monkeypatch.setattr(server, "is_telegram_configured", lambda: False)
    monkeypatch.setattr(server, "create_remote_input_dialog", _fake_remote_input_dialog)

    first_task = asyncio.create_task(server.get_remote_input.fn("Title", "first"))
    await asyncio.to_thread(first_started.wait, 1.0)

    cancel_started = time.monotonic()
    first_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first_task
    cancel_finished = time.monotonic()

    second_result = await server.get_remote_input.fn("Title", "second")

    assert cancel_finished >= timings["cleanup_finished"]
    assert cancel_finished - cancel_started >= 0.25
    assert second_result["success"] is True
    assert second_result["user_input"] == "second result"


def test_create_remote_input_dialog_headless_transport_cancel_cleans_up(monkeypatch):
    """Headless Telegram-only mode should complete prompt cleanup on cancel."""
    poll_started = threading.Event()
    transport_cancel = threading.Event()
    observed = {}

    class _FakeMiniAppSession:
        def __init__(self):
            self.webapp_url = "https://example.invalid/app"
            self.stop_calls = 0
            self.http_server = type(
                "HttpServer",
                (),
                {"answer_queue": None},
            )()

        def stop(self):
            self.stop_calls += 1

    fake_session = _FakeMiniAppSession()

    class _FakeTelegramBridge:
        def __init__(self):
            observed["bridge"] = self
            self.complete_calls = []
            self.edit_calls = []
            self.unpin_calls = []

        def send_prompt_with_miniapp(self, title, prompt, webapp_url, name_or_role=""):
            return 321

        def send_prompt(self, title, prompt):
            return 321

        def poll_for_answer(self, prompt_message_id, cancel_event, answer_queue=None):
            poll_started.set()
            cancel_event.wait(timeout=2.0)
            return None

        def complete_prompt_session(self, prompt_message_id, *, status, source=None):
            self.complete_calls.append((prompt_message_id, status, source))

        def edit_message(self, message_id, new_text, parse_mode=None):
            self.edit_calls.append((message_id, new_text, parse_mode))
            return True

        def unpin_message(self, message_id):
            self.unpin_calls.append(message_id)
            return True

        def _escape_html(self, text):
            return text

    monkeypatch.setattr(server, "_ensure_persistent_root", lambda: None)
    monkeypatch.setattr(server, "is_telegram_configured", lambda: True)
    monkeypatch.setattr(server, "TelegramBridge", _FakeTelegramBridge)
    monkeypatch.setattr(server, "_get_multiline_input_custom_prompts", lambda: [])
    monkeypatch.setattr(
        server,
        "_build_miniapp_session",
        lambda *args, **kwargs: fake_session,
    )

    result_holder = {}

    def _run_dialog():
        result_holder["value"] = server.create_remote_input_dialog(
            "Title",
            "Prompt",
            external_cancel=transport_cancel,
        )

    thread = threading.Thread(target=_run_dialog, daemon=True)
    thread.start()

    assert poll_started.wait(1.0)
    transport_cancel.set()
    thread.join(timeout=3.0)

    assert not thread.is_alive()
    assert result_holder["value"] is None
    assert fake_session.stop_calls == 1
    assert observed["bridge"].complete_calls == [(321, "transport_cancelled", "transport")]
    assert observed["bridge"].edit_calls == [
        (321, "❌ Request cancelled — MCP connection closed.", None)
    ]
    assert observed["bridge"].unpin_calls == [321]


def test_create_remote_input_dialog_gui_transport_cancel_closes_dialog(monkeypatch):
    """GUI mode should close the local dialog when transport cancellation arrives."""
    dialog_created = threading.Event()
    transport_cancel = threading.Event()
    observed = {}

    class _FakeDialogWindow:
        def after_cancel(self, _reminder_id):
            return None

        def destroy(self):
            observed["destroyed"] = True

        def lift(self):
            observed["lifted"] = True

    class _FakeMultilineInputDialog:
        def __init__(self, parent, title, prompt, default_value="", done_event=None):
            self.result = None
            self._done_event = done_event
            self._reminder_id = None
            self.dialog = _FakeDialogWindow()
            self.theme_colors = {"error_color": "#D93025"}
            observed["dialog"] = self
            dialog_created.set()

        def cancel_clicked(self):
            observed["cancel_called"] = True
            self.result = None
            self.dialog.destroy()
            if self._done_event is not None:
                self._done_event.set()

    monkeypatch.setattr(server, "_ensure_persistent_root", lambda: object())
    monkeypatch.setattr(server, "_dialog_request_queue", _ImmediateQueue())
    monkeypatch.setattr(server, "MultilineInputDialog", _FakeMultilineInputDialog)
    monkeypatch.setattr(server, "is_telegram_configured", lambda: False)

    result_holder = {}

    def _run_dialog():
        result_holder["value"] = server.create_remote_input_dialog(
            "Title",
            "Prompt",
            external_cancel=transport_cancel,
        )

    thread = threading.Thread(target=_run_dialog, daemon=True)
    thread.start()

    assert dialog_created.wait(1.0)
    transport_cancel.set()
    thread.join(timeout=3.0)

    assert not thread.is_alive()
    assert result_holder["value"] is None
    assert observed["cancel_called"] is True
    assert observed["destroyed"] is True


def test_create_remote_input_dialog_headless_preserves_miniapp_source_label(monkeypatch):
    """Headless cleanup should distinguish Mini App answers from direct replies."""
    observed = {}

    class _FakeMiniAppSession:
        def __init__(self):
            self.webapp_url = "https://example.invalid/app"
            self.stop_calls = 0
            self.http_server = type(
                "HttpServer",
                (),
                {"answer_queue": None},
            )()

        def stop(self):
            self.stop_calls += 1

    fake_session = _FakeMiniAppSession()

    class _FakeTelegramBridge:
        def __init__(self):
            observed["bridge"] = self
            self.complete_calls = []
            self.edit_calls = []
            self.unpin_calls = []

        def send_prompt_with_miniapp(self, title, prompt, webapp_url, name_or_role=""):
            return 654

        def send_prompt(self, title, prompt):
            return 654

        def poll_for_answer_details(self, prompt_message_id, cancel_event, answer_queue=None):
            return {
                "text": "submitted from mini app",
                "source": "telegram_miniapp",
                "received_at": 123,
            }

        def complete_prompt_session(self, prompt_message_id, *, status, source=None):
            self.complete_calls.append((prompt_message_id, status, source))

        def edit_message(self, message_id, new_text, parse_mode=None):
            self.edit_calls.append((message_id, new_text, parse_mode))
            return True

        def unpin_message(self, message_id):
            self.unpin_calls.append(message_id)
            return True

        def _escape_html(self, text):
            return text

    monkeypatch.setattr(server, "_ensure_persistent_root", lambda: None)
    monkeypatch.setattr(server, "is_telegram_configured", lambda: True)
    monkeypatch.setattr(server, "TelegramBridge", _FakeTelegramBridge)
    monkeypatch.setattr(server, "_get_multiline_input_custom_prompts", lambda: [])
    monkeypatch.setattr(
        server,
        "_build_miniapp_session",
        lambda *args, **kwargs: fake_session,
    )

    result = server.create_remote_input_dialog("Title", "Prompt")

    assert result == "submitted from mini app"
    assert fake_session.stop_calls == 1
    assert observed["bridge"].complete_calls == [
        (654, "miniapp_reply", "telegram_miniapp")
    ]
    assert observed["bridge"].edit_calls == [
        (
            654,
            "🖥️ <b>Title</b>\n\nOriginal message:\n<blockquote expandable>Prompt</blockquote>\n\nResponse via Telegram Mini App:\n<blockquote expandable>submitted from mini app</blockquote>",
            "HTML",
        )
    ]
    assert observed["bridge"].unpin_calls == [654]


def test_create_remote_input_dialog_gui_local_reply_unpins_prompt(monkeypatch):
    """Local GUI answers should still unpin the Telegram prompt during cleanup."""
    observed = {}

    class _FakeLabel:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        def grid(self, *args, **kwargs):
            return None

        def configure(self, *args, **kwargs):
            return None

    class _FakeDialogWindow:
        def after_cancel(self, _reminder_id):
            return None

        def lift(self):
            return None

        def destroy(self):
            return None

    class _FakeMultilineInputDialog:
        def __init__(self, parent, title, prompt, default_value="", done_event=None):
            self.result = "answered locally"
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

    class _FakeTelegramBridge:
        def __init__(self):
            observed["bridge"] = self
            self.complete_calls = []
            self.edit_calls = []
            self.unpin_calls = []

        def send_prompt(self, title, prompt):
            return 777

        def poll_for_answer_details(self, prompt_message_id, cancel_event, answer_queue=None):
            cancel_event.wait(timeout=1.0)
            return None

        def complete_prompt_session(self, prompt_message_id, *, status, source=None):
            self.complete_calls.append((prompt_message_id, status, source))

        def edit_message(self, message_id, new_text, parse_mode=None):
            self.edit_calls.append((message_id, new_text, parse_mode))
            return True

        def unpin_message(self, message_id):
            self.unpin_calls.append(message_id)
            return True

        def _escape_html(self, text):
            return text

    monkeypatch.setattr(server, "_ensure_persistent_root", lambda: object())
    monkeypatch.setattr(server, "_dialog_request_queue", _ImmediateQueue())
    monkeypatch.setattr(server, "MultilineInputDialog", _FakeMultilineInputDialog)
    monkeypatch.setattr(server, "TelegramBridge", _FakeTelegramBridge)
    monkeypatch.setattr(server, "is_telegram_configured", lambda: True)
    monkeypatch.setattr(server, "_build_miniapp_session", lambda *args, **kwargs: None)
    monkeypatch.setattr(server.tk, "Label", _FakeLabel)

    result = server.create_remote_input_dialog("Title", "Prompt")

    assert result == "answered locally"
    assert observed["bridge"].complete_calls == [(777, "local_reply", "tkinter")]
    assert observed["bridge"].edit_calls == [
        (
            777,
            "🖥️ <b>Title</b>\n\nOriginal message:\n<blockquote expandable>Prompt</blockquote>\n\n✅ User answered via local dialog:\n<blockquote expandable>answered locally</blockquote>",
            "HTML",
        )
    ]
    assert observed["bridge"].unpin_calls == [777]


def test_create_remote_input_dialog_gui_local_cancel_unpins_prompt(monkeypatch):
    """Local GUI cancellation should still unpin the Telegram prompt during cleanup."""
    observed = {}

    class _FakeLabel:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        def grid(self, *args, **kwargs):
            return None

        def configure(self, *args, **kwargs):
            return None

    class _FakeDialogWindow:
        def after_cancel(self, _reminder_id):
            return None

        def lift(self):
            return None

        def destroy(self):
            return None

    class _FakeMultilineInputDialog:
        def __init__(self, parent, title, prompt, default_value="", done_event=None):
            self.result = None
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

    class _FakeTelegramBridge:
        def __init__(self):
            observed["bridge"] = self
            self.complete_calls = []
            self.edit_calls = []
            self.unpin_calls = []

        def send_prompt(self, title, prompt):
            return 778

        def poll_for_answer_details(self, prompt_message_id, cancel_event, answer_queue=None):
            cancel_event.wait(timeout=1.0)
            return None

        def complete_prompt_session(self, prompt_message_id, *, status, source=None):
            self.complete_calls.append((prompt_message_id, status, source))

        def edit_message(self, message_id, new_text, parse_mode=None):
            self.edit_calls.append((message_id, new_text, parse_mode))
            return True

        def unpin_message(self, message_id):
            self.unpin_calls.append(message_id)
            return True

        def _escape_html(self, text):
            return text

    monkeypatch.setattr(server, "_ensure_persistent_root", lambda: object())
    monkeypatch.setattr(server, "_dialog_request_queue", _ImmediateQueue())
    monkeypatch.setattr(server, "MultilineInputDialog", _FakeMultilineInputDialog)
    monkeypatch.setattr(server, "TelegramBridge", _FakeTelegramBridge)
    monkeypatch.setattr(server, "is_telegram_configured", lambda: True)
    monkeypatch.setattr(server, "_build_miniapp_session", lambda *args, **kwargs: None)
    monkeypatch.setattr(server.tk, "Label", _FakeLabel)

    result = server.create_remote_input_dialog("Title", "Prompt")

    assert result is None
    assert observed["bridge"].complete_calls == [(778, "local_cancel", "tkinter")]
    assert observed["bridge"].edit_calls == [
        (778, "🖥️ <b>Title</b>\n\nOriginal message:\n<blockquote expandable>Prompt</blockquote>\n\n❌ Cancelled locally", "HTML")
    ]
    assert observed["bridge"].unpin_calls == [778]


def test_create_remote_input_dialog_gui_unexpected_exception_still_cleans_up_prompt(monkeypatch):
    """GUI-path crashes after prompt send should still complete and unpin the prompt."""
    observed = {}
    original_thread = server.threading.Thread

    class _FakeLabel:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        def grid(self, *args, **kwargs):
            return None

        def configure(self, *args, **kwargs):
            return None

    class _FakeDialogWindow:
        def after_cancel(self, _reminder_id):
            return None

        def lift(self):
            return None

        def destroy(self):
            return None

    class _FakeMultilineInputDialog:
        def __init__(self, parent, title, prompt, default_value="", done_event=None):
            self.result = None
            self._done_event = done_event
            self._reminder_id = None
            self.dialog = _FakeDialogWindow()
            self.main_frame = object()
            self.theme_colors = {
                "bg_secondary": "#fff",
                "fg_secondary": "#111",
                "accent_color": "#06c",
            }

    class _FakeTelegramBridge:
        def __init__(self):
            observed["bridge"] = self
            self.complete_calls = []
            self.unpin_calls = []

        def send_prompt(self, title, prompt):
            return 780

        def complete_prompt_session(self, prompt_message_id, *, status, source=None):
            self.complete_calls.append((prompt_message_id, status, source))

        def unpin_message(self, message_id):
            self.unpin_calls.append(message_id)
            return True

    class _FailingPollerThread(original_thread):
        def start(self):
            if self.name == "tg-remote-input-poller":
                raise RuntimeError("poller bootstrap failed")
            return super().start()

    monkeypatch.setattr(server, "_ensure_persistent_root", lambda: object())
    monkeypatch.setattr(server, "_dialog_request_queue", _ImmediateQueue())
    monkeypatch.setattr(server, "MultilineInputDialog", _FakeMultilineInputDialog)
    monkeypatch.setattr(server, "TelegramBridge", _FakeTelegramBridge)
    monkeypatch.setattr(server, "is_telegram_configured", lambda: True)
    monkeypatch.setattr(server, "_build_miniapp_session", lambda *args, **kwargs: None)
    monkeypatch.setattr(server.tk, "Label", _FakeLabel)
    monkeypatch.setattr(server.threading, "Thread", _FailingPollerThread)

    result = server.create_remote_input_dialog("Title", "Prompt")

    assert result is None
    assert observed["bridge"].complete_calls == [(780, "timeout", "telegram")]
    assert observed["bridge"].unpin_calls == [780]


def test_create_remote_input_dialog_headless_timeout_unpins_prompt(monkeypatch):
    """Headless timeout cleanup should still unpin the Telegram prompt."""
    observed = {}

    class _FakeTelegramBridge:
        def __init__(self):
            observed["bridge"] = self
            self.complete_calls = []
            self.edit_calls = []
            self.unpin_calls = []

        def send_prompt(self, title, prompt):
            return 779

        def poll_for_answer_details(self, prompt_message_id, cancel_event, answer_queue=None):
            return None

        def complete_prompt_session(self, prompt_message_id, *, status, source=None):
            self.complete_calls.append((prompt_message_id, status, source))

        def edit_message(self, message_id, new_text, parse_mode=None):
            self.edit_calls.append((message_id, new_text, parse_mode))
            return True

        def unpin_message(self, message_id):
            self.unpin_calls.append(message_id)
            return True

        def _escape_html(self, text):
            return text

    monkeypatch.setattr(server, "_ensure_persistent_root", lambda: None)
    monkeypatch.setattr(server, "is_telegram_configured", lambda: True)
    monkeypatch.setattr(server, "TelegramBridge", _FakeTelegramBridge)
    monkeypatch.setattr(server, "_get_multiline_input_custom_prompts", lambda: [])
    monkeypatch.setattr(server, "_build_miniapp_session", lambda *args, **kwargs: None)

    result = server.create_remote_input_dialog("Title", "Prompt")

    assert result is None
    assert observed["bridge"].complete_calls == [(779, "timeout", "telegram")]
    assert observed["bridge"].edit_calls == [(779, "⏰ Timed out — no response received.", None)]
    assert observed["bridge"].unpin_calls == [779]


def test_create_remote_input_dialog_miniapp_send_failure_only_tries_plain_prompt_once(monkeypatch):
    """A Mini App send failure should trigger at most one plain Telegram fallback attempt."""
    observed = {}

    class _FakeMiniAppSession:
        def __init__(self):
            self.webapp_url = "https://example.invalid/app"
            self.stop_calls = 0
            self.http_server = type("HttpServer", (), {"answer_queue": None})()

        def stop(self):
            self.stop_calls += 1

    fake_session = _FakeMiniAppSession()

    class _FakeTelegramBridge:
        def __init__(self):
            observed["bridge"] = self
            self.send_prompt_calls = 0
            self.send_prompt_with_miniapp_calls = 0

        def send_prompt_with_miniapp(self, title, prompt, webapp_url, name_or_role=""):
            self.send_prompt_with_miniapp_calls += 1
            return None

        def send_prompt(self, title, prompt):
            self.send_prompt_calls += 1
            return None

    monkeypatch.setattr(server, "_ensure_persistent_root", lambda: None)
    monkeypatch.setattr(server, "is_telegram_configured", lambda: True)
    monkeypatch.setattr(server, "TelegramBridge", _FakeTelegramBridge)
    monkeypatch.setattr(server, "_get_multiline_input_custom_prompts", lambda: [])
    monkeypatch.setattr(server, "_build_miniapp_session", lambda *args, **kwargs: fake_session)

    result = server.create_remote_input_dialog("Title", "Prompt")

    assert result is None
    assert fake_session.stop_calls == 1
    assert observed["bridge"].send_prompt_with_miniapp_calls == 1
    assert observed["bridge"].send_prompt_calls == 1


@pytest.mark.asyncio
async def test_health_check_is_passive_for_gui_state(monkeypatch):
    """Health checks must report lazy GUI state without forcing Tk initialization."""
    monkeypatch.setattr(
        server,
        "ensure_gui_initialized",
        lambda: (_ for _ in ()).throw(AssertionError("health_check must stay passive")),
    )
    monkeypatch.setattr(server, "_TKINTER_AVAILABLE", True)
    monkeypatch.setattr(server, "_gui_initialized", False)
    monkeypatch.setattr(server, "_persistent_gui_thread", None)
    monkeypatch.setattr(server, "_is_bypass_active", lambda: False)
    monkeypatch.setattr(
        server,
        "describe_shared_hub_status",
        lambda: {
            "mode": "off",
            "enabled": False,
            "configured": False,
            "descriptor_present": False,
            "hub_reachable": False,
            "hub_healthy": False,
            "hub_ready": False,
            "health": None,
            "descriptor": None,
        },
    )

    # Access .fn to get the underlying async function from the FunctionTool wrapper
    result = await server.health_check.fn()

    assert result["status"] == "healthy"
    assert result["gui_available"] is True
    assert result["gui_initialized"] is False
    assert result["gui_thread_running"] is False
    assert result["gui_lazy"] is True
    assert result["startup_ready"] is True
