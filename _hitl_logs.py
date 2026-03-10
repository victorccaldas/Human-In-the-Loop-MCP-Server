"""Shared helpers for repo-local HITL log files."""

from __future__ import annotations

import contextlib
import json
import os
import time
from typing import Any, Optional

if os.name == "nt":
    import msvcrt
else:
    import fcntl


_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
_LOGS_DIR = os.path.join(_REPO_ROOT, "logs")


def _ensure_dir(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return path


def get_repo_logs_dir() -> str:
    """Return the repo-local logs directory, creating it when needed."""
    return _ensure_dir(_LOGS_DIR)


def get_remote_input_diag_log_path() -> str:
    """Return the shared remote-input diagnostic log path."""
    return os.path.join(get_repo_logs_dir(), "remote_input_diag.log")


def get_shared_telegram_logs_dir(scope_key: Optional[str] = None) -> str:
    """Return the repo-local shared Telegram log directory."""
    base_dir = _ensure_dir(os.path.join(get_repo_logs_dir(), "shared-telegram"))
    if scope_key:
        return _ensure_dir(os.path.join(base_dir, scope_key))
    return base_dir


@contextlib.contextmanager
def _exclusive_file_lock(handle):
    try:
        if os.name == "nt":
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            if os.name == "nt":
                handle.seek(0, os.SEEK_CUR)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass


def append_log_line(path: str, message: str) -> None:
    """Append a timestamped UTF-8 log line, ignoring best-effort failures.
    
    If the file write fails, the error is printed to stderr as a last-resort
    fallback so the diagnostic is never silently lost.
    """
    try:
        _ensure_dir(os.path.dirname(path))
        with open(path, "a", encoding="utf-8") as handle:
            with _exclusive_file_lock(handle):
                handle.write(
                    f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] "
                    f"[pid={os.getpid()}] {message}\n"
                )
                handle.flush()
    except Exception as write_exc:
        try:
            import sys
            print(
                f"[HITL-Logs] append_log_line FAILED for {path}: {type(write_exc).__name__}: {write_exc} | "
                f"Original message: {message}",
                file=sys.stderr,
            )
        except Exception:
            pass


def append_json_line(path: str, payload: dict[str, Any]) -> None:
    """Append one JSON object per line with best-effort file locking.
    
    If the file write fails, the error is printed to stderr as a last-resort
    fallback so the diagnostic is never silently lost.
    """
    try:
        _ensure_dir(os.path.dirname(path))
        with open(path, "a", encoding="utf-8") as handle:
            with _exclusive_file_lock(handle):
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
                handle.flush()
    except Exception as write_exc:
        try:
            import sys
            print(
                f"[HITL-Logs] append_json_line FAILED for {path}: {type(write_exc).__name__}: {write_exc} | "
                f"Original payload: {payload}",
                file=sys.stderr,
            )
        except Exception:
            pass