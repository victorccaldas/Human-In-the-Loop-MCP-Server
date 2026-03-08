# pyright: reportMissingImports=false

import asyncio
import threading
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

    task = asyncio.create_task(server.get_remote_input("Title", "Prompt"))
    await asyncio.to_thread(worker_started.wait, 1.0)

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert worker_cleanup.wait(1.0)
    assert isinstance(captured["external_cancel"], threading.Event)


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
