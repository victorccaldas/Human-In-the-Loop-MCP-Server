# pyright: reportMissingImports=false

import asyncio
import concurrent.futures
import threading
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import human_loop_server as server
from _miniapp_server import MiniAppHTTPServer
from _miniapp_template import MINIAPP_HTML


@pytest.mark.asyncio
async def test_get_user_input_timeout_returns_clean_cancellation(monkeypatch):
    blocker = threading.Event()

    def _blocking_dialog(*_args, **_kwargs):
        blocker.wait(timeout=2.0)
        return "late-result"

    monkeypatch.setattr(server, "_check_bypass", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "ensure_gui_initialized", lambda: True)
    monkeypatch.setattr(server, "create_input_dialog", _blocking_dialog)
    monkeypatch.setattr(server, "_get_tool_timeout", lambda: 0.01)

    result = await server.get_user_input.fn("Title", "Prompt")
    blocker.set()

    assert result["success"] is False
    assert result["cancelled"] is True
    assert "timed out" in result["error"]


@pytest.mark.asyncio
async def test_show_confirmation_dialog_timeout_returns_clean_cancellation(monkeypatch):
    blocker = threading.Event()

    def _blocking_confirmation(*_args, **_kwargs):
        blocker.wait(timeout=2.0)
        return True

    monkeypatch.setattr(server, "_check_bypass", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "ensure_gui_initialized", lambda: True)
    monkeypatch.setattr(server, "show_confirmation", _blocking_confirmation)
    monkeypatch.setattr(server, "_get_tool_timeout", lambda: 0.01)

    result = await server.show_confirmation_dialog.fn("Title", "Prompt")
    blocker.set()

    assert result["success"] is False
    assert result["cancelled"] is True
    assert result["confirmed"] is False
    assert result["response"] is None


@pytest.mark.asyncio
async def test_get_remote_input_timeout_cleanup_uses_background_executor_shutdown(monkeypatch):
    shutdown_calls = []

    class _TimedOutWorkerFuture(concurrent.futures.Future):
        def result(self, timeout=None):
            if timeout == 2.0:
                raise concurrent.futures.TimeoutError()
            return super().result(timeout=timeout)

    class _FakeExecutor:
        def __init__(self, *args, **kwargs):
            self.future = _TimedOutWorkerFuture()

        def submit(self, fn, *args, **kwargs):
            return self.future

        def shutdown(self, wait=True, cancel_futures=False):
            shutdown_calls.append((wait, cancel_futures))

    async def _run_immediately(func, /, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(server, "_check_bypass", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "ensure_gui_initialized", lambda: True)
    monkeypatch.setattr(server.asyncio, "to_thread", _run_immediately)
    monkeypatch.setattr(concurrent.futures, "ThreadPoolExecutor", _FakeExecutor)
    monkeypatch.setattr(server, "append_log_line", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(server, "get_remote_input_diag_log_path", lambda: "diag.log")

    task = asyncio.create_task(server.get_remote_input.fn("Title", "Prompt"))
    await asyncio.sleep(0.01)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert shutdown_calls == [(False, False)]


def test_is_bypass_active_returns_false_when_lock_disappears(monkeypatch, tmp_path):
    lock_path = tmp_path / "bypass.lock"

    monkeypatch.setattr(server, "_get_bypass_lock_path", lambda: str(lock_path))

    def _missing_open(*_args, **_kwargs):
        raise FileNotFoundError()

    monkeypatch.setattr("builtins.open", _missing_open)

    assert server._is_bypass_active() is False


def test_get_multiline_input_custom_prompts_logs_missing_prompt_header(monkeypatch, tmp_path):
    fake_script = tmp_path / "human_loop_server.py"
    fake_csv = tmp_path / "custom_prompts.csv"
    fake_csv.write_text("active,active_color\n1,red\n", encoding="utf-8")
    captured_logs = []

    monkeypatch.setattr(server, "__file__", str(fake_script))
    monkeypatch.setattr(server, "append_log_line", lambda _path, message: captured_logs.append(message))
    monkeypatch.setattr(server, "get_remote_input_diag_log_path", lambda: str(tmp_path / "diag.log"))

    prompts = server._get_multiline_input_custom_prompts()

    assert prompts == []
    assert any("missing required 'prompt' column" in message for message in captured_logs)


def test_ensure_persistent_root_returns_none_when_startup_fails(monkeypatch):
    original_root = server._persistent_root
    original_thread = server._persistent_gui_thread
    original_queue = server._dialog_request_queue

    class _FakeThread:
        def __init__(self, target, daemon, name):
            self._target = target
            self._alive = False

        def start(self):
            self._alive = True
            self._target()
            self._alive = False

        def is_alive(self):
            return self._alive

    monkeypatch.setattr(server, "_TKINTER_AVAILABLE", True)
    monkeypatch.setattr(server, "_persistent_root", None)
    monkeypatch.setattr(server, "_persistent_gui_thread", None)
    monkeypatch.setattr(server, "append_log_line", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(server, "get_remote_input_diag_log_path", lambda: "diag.log")
    monkeypatch.setattr(server.threading, "Thread", _FakeThread)
    monkeypatch.setattr(server.tk, "Tk", lambda: (_ for _ in ()).throw(RuntimeError("no display")))

    try:
        assert server._ensure_persistent_root() is None
    finally:
        monkeypatch.setattr(server, "_persistent_root", original_root)
        monkeypatch.setattr(server, "_persistent_gui_thread", original_thread)
        monkeypatch.setattr(server, "_dialog_request_queue", original_queue)


def test_miniapp_server_stop_calls_server_close_even_after_shutdown_error(monkeypatch):
    calls = []

    class _FakeHTTPD:
        def shutdown(self):
            calls.append("shutdown")
            raise RuntimeError("boom")

        def server_close(self):
            calls.append("server_close")

    class _FakeThread:
        def join(self, timeout=None):
            calls.append(("join", timeout))

        def is_alive(self):
            return False

    server_instance = MiniAppHTTPServer("t", "p", [], "token", "https://example.com")
    server_instance._httpd = _FakeHTTPD()
    server_instance._thread = _FakeThread()
    server_instance._port = 4321

    server_instance.stop()

    assert calls == ["shutdown", "server_close", ("join", 3.0)]
    assert server_instance._httpd is None
    assert server_instance._thread is None


def test_parse_inline_markdown_runs_extracts_basic_styles():
    runs = server._parse_inline_markdown_runs(
        "**Bold** and `code` plus [docs](https://example.invalid)"
    )

    assert ("Bold", ("md_bold",)) in runs
    assert ("code", ("md_code",)) in runs
    assert ("docs", ("md_link",)) in runs
    assert (" (https://example.invalid)", ("md_link_url",)) in runs


def test_parse_inline_markdown_runs_keeps_identifier_underscores_literal():
    runs = server._parse_inline_markdown_runs("`get_user_input` and get_user_input")

    assert ("get_user_input", ("md_code",)) in runs
    assert (" and get_user_input", tuple()) in runs


def test_miniapp_prompt_box_allows_text_selection():
    assert "user-select: text;" in MINIAPP_HTML
    assert "-webkit-user-select: text;" in MINIAPP_HTML