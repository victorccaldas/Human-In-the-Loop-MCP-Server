"""Shared Telegram hub for safely multiplexing Bot API polling on one host.

This module centralizes Telegram ``getUpdates`` ownership behind a single
host-local HTTP service. Multiple HITL-MCP server instances can still send
messages directly, but inbound Telegram polling is routed through this hub so
only one process consumes updates for a given bot/chat pair on the host.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import json
import os
import platform
import socket
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

try:
    import requests
except ImportError:  # pragma: no cover - validated by the caller at runtime
    requests = None  # type: ignore[assignment]


_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_CONFIG = os.path.join(_SCRIPT_DIR, "telegram_config.json")
_HUB_HOST = "127.0.0.1"
_HUB_VERSION = 1
_DEFAULT_WAIT_TIMEOUT_SECONDS = 5.0
_SHARED_MODE_ENV = "HITL_SHARED_TELEGRAM_MODE"
_RUNTIME_DIR_ENV = "HITL_MCP_RUNTIME_DIR"
_SERVER_MODULE_NAME = "human_loop_server"


class SharedTelegramHubError(RuntimeError):
    """Raised when the shared Telegram hub cannot be used safely."""


class UnsafeMixedTelegramStateError(SharedTelegramHubError):
    """Raised when direct polling would conflict with an already running hub."""


@dataclass(frozen=True)
class TelegramCredentials:
    """Resolved Telegram credentials for a bot/chat pair."""

    bot_token: str
    chat_id: str
    config_path: Optional[str]
    source: str

    @property
    def scope_key(self) -> str:
        """Return a stable host-local scope key for this bot/chat pair."""
        digest = hashlib.sha256(
            f"{self.bot_token}:{self.chat_id}".encode("utf-8")
        ).hexdigest()
        return digest[:24]


@dataclass(frozen=True)
class RuntimePaths:
    """Filesystem locations shared across worktrees on the same host."""

    root_dir: str
    scope_dir: str
    descriptor_path: str
    startup_lock_path: str
    bypass_lock_path: str
    bypass_log_path: str
    hub_log_path: str


@dataclass(frozen=True)
class SharedHubDescriptor:
    """Wire format persisted to disk so sibling worktrees can discover the hub."""

    version: int
    host: str
    port: int
    pid: int
    scope_key: str
    started_at: str
    script_path: str
    runtime_scope_dir: str

    @property
    def base_url(self) -> str:
        """Return the HTTP base URL for the running hub."""
        return f"http://{self.host}:{self.port}"


@dataclass
class _PromptMailbox:
    """In-memory mailbox used by the hub to hand replies back to clients."""

    event: threading.Event
    reply_text: Optional[str] = None
    reply_message_id: Optional[int] = None
    reply_source: Optional[str] = None
    reply_received_at: Optional[int] = None
    completed: bool = False
    completion_status: Optional[str] = None
    updated_at: float = 0.0


def _utc_now_iso() -> str:
    """Return an RFC3339-ish UTC timestamp for descriptor and audit files."""
    return datetime.now(timezone.utc).isoformat()


def _ensure_dir(path: str) -> str:
    """Create *path* if needed and return it for fluent call sites."""
    os.makedirs(path, exist_ok=True)
    return path


def get_shared_telegram_mode() -> str:
    """Return the configured shared-Telegram mode.

    Supported values:
    - ``auto`` (default) / ``on`` / ``true`` / ``1``: require the host-local hub
    - ``off``: legacy direct polling
    - ``require``: same as ``auto`` but callers should treat failures as fatal
    """
    # Default to shared mode so same-host Telegram instances converge on one
    # safe getUpdates owner unless the operator explicitly opts out.
    raw = os.environ.get(_SHARED_MODE_ENV, "auto").strip().lower()
    aliases = {"on": "auto", "true": "auto", "1": "auto", "yes": "auto"}
    return aliases.get(raw, raw or "off")


def is_shared_telegram_enabled() -> bool:
    """Return True when this process must use the shared host-local hub."""
    return get_shared_telegram_mode() in {"auto", "require"}


def get_host_runtime_root() -> str:
    """Return the host-global runtime root shared by sibling worktrees."""
    override = os.environ.get(_RUNTIME_DIR_ENV, "").strip()
    if override:
        return _ensure_dir(os.path.abspath(os.path.expanduser(override)))

    system = platform.system().lower()
    if system == "windows":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    elif system == "darwin":
        base = str(Path.home() / "Library" / "Application Support")
    else:
        base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")

    return _ensure_dir(os.path.join(base, "hitl-mcp-server"))


def load_telegram_credentials(
    config_path: Optional[str] = None,
    environ: Optional[dict[str, str]] = None,
) -> Optional[TelegramCredentials]:
    """Resolve Telegram credentials from config file or environment variables."""
    env = environ if environ is not None else os.environ
    config_file = config_path or _DEFAULT_CONFIG

    bot_token: Optional[str] = None
    chat_id: Optional[str] = None
    source = ""

    if os.path.isfile(config_file):
        try:
            with open(config_file, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            bot_token = data.get("bot_token") or None
            chat_id = data.get("chat_id") or None
            if bot_token and chat_id:
                source = "config"
        except Exception as exc:
            print(f"[SharedTelegramHub] Failed to read {config_file}: {exc}")

    if not bot_token:
        bot_token = env.get("TELEGRAM_BOT_TOKEN") or None
    if not chat_id:
        chat_id = env.get("TELEGRAM_CHAT_ID") or None
    if bot_token and chat_id and not source:
        source = "env"

    if not bot_token or not chat_id:
        return None

    return TelegramCredentials(
        bot_token=bot_token,
        chat_id=str(chat_id),
        config_path=config_file if source == "config" else None,
        source=source,
    )


def get_runtime_paths(credentials: Optional[TelegramCredentials] = None) -> RuntimePaths:
    """Return host-global runtime paths, scoped by the Telegram bot/chat pair."""
    creds = credentials or load_telegram_credentials()
    scope_key = creds.scope_key if creds else "default"
    root_dir = _ensure_dir(os.path.join(get_host_runtime_root(), "shared-telegram"))
    scope_dir = _ensure_dir(os.path.join(root_dir, scope_key))
    return RuntimePaths(
        root_dir=root_dir,
        scope_dir=scope_dir,
        descriptor_path=os.path.join(scope_dir, "hub-descriptor.json"),
        startup_lock_path=os.path.join(scope_dir, "hub-start.lock"),
        bypass_lock_path=os.path.join(scope_dir, "bypass_active.lock"),
        bypass_log_path=os.path.join(scope_dir, "bypass_log.jsonl"),
        hub_log_path=os.path.join(scope_dir, "hub.log"),
    )


def get_bypass_lock_file(credentials: Optional[TelegramCredentials] = None) -> str:
    """Return the host-global bypass lock file path."""
    return get_runtime_paths(credentials).bypass_lock_path


def get_bypass_log_file(credentials: Optional[TelegramCredentials] = None) -> str:
    """Return the host-global bypass audit log path."""
    return get_runtime_paths(credentials).bypass_log_path


def is_telegram_configured(config_path: Optional[str] = None) -> bool:
    """Return True when Telegram credentials are available."""
    return load_telegram_credentials(config_path=config_path) is not None


def _read_descriptor(path: str) -> Optional[SharedHubDescriptor]:
    """Load a hub descriptor from disk, returning None when absent/invalid."""
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return SharedHubDescriptor(**data)
    except Exception as exc:
        print(f"[SharedTelegramHub] Failed to read descriptor {path}: {exc}")
        return None


def _write_descriptor(descriptor: SharedHubDescriptor, path: str) -> None:
    """Persist a hub descriptor atomically for sibling worktrees to discover."""
    temp_path = f"{path}.tmp"
    with open(temp_path, "w", encoding="utf-8") as handle:
        json.dump(asdict(descriptor), handle, indent=2)
    os.replace(temp_path, path)


def _remove_file_safely(path: str) -> None:
    """Remove a file when it exists, ignoring transient cleanup races."""
    with contextlib.suppress(FileNotFoundError, PermissionError):
        os.remove(path)


def _is_descriptor_healthy(descriptor: SharedHubDescriptor, timeout: float = 1.5) -> bool:
    """Probe the HTTP health endpoint for a discovered descriptor."""
    if requests is None:
        return False
    try:
        response = requests.get(f"{descriptor.base_url}/health", timeout=timeout)
        if response.status_code != 200:
            return False
        data = response.json()
        return bool(data.get("ok")) and data.get("scope_key") == descriptor.scope_key
    except Exception:
        return False


def find_running_hub_descriptor(
    credentials: Optional[TelegramCredentials] = None,
    *,
    prune_stale: bool = True,
) -> Optional[SharedHubDescriptor]:
    """Return the active descriptor for the current Telegram bot/chat scope."""
    creds = credentials or load_telegram_credentials()
    paths = get_runtime_paths(creds)
    descriptor = _read_descriptor(paths.descriptor_path)
    if descriptor is None:
        return None
    if descriptor.version != _HUB_VERSION:
        if prune_stale:
            _remove_file_safely(paths.descriptor_path)
        return None
    if descriptor.scope_key != (creds.scope_key if creds else descriptor.scope_key):
        return None
    if _is_descriptor_healthy(descriptor):
        return descriptor
    if prune_stale:
        _remove_file_safely(paths.descriptor_path)
    return None


def detect_unsafe_direct_polling(
    credentials: Optional[TelegramCredentials] = None,
) -> None:
    """Raise when legacy direct polling would collide with a running shared hub."""
    descriptor = find_running_hub_descriptor(credentials, prune_stale=False)
    if descriptor is None:
        return
    raise UnsafeMixedTelegramStateError(
        "Unsafe mixed Telegram polling state detected: a shared Telegram hub is "
        "already active for this bot/chat on the host, but this process is "
        "configured for direct polling. Enable HITL_SHARED_TELEGRAM_MODE=auto "
        "or HITL_SHARED_TELEGRAM_MODE=require for every same-host instance."
    )


def _find_free_port() -> int:
    """Reserve and return an ephemeral localhost TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((_HUB_HOST, 0))
        sock.listen(1)
        return int(sock.getsockname()[1])


def _acquire_startup_lock(lock_path: str, stale_after_seconds: float = 30.0) -> bool:
    """Acquire a lightweight file lock for hub startup orchestration."""
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            if time.time() - os.path.getmtime(lock_path) > stale_after_seconds:
                _remove_file_safely(lock_path)
                fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            else:
                return False
        except FileNotFoundError:
            return False

    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(f"{os.getpid()}\n")
    return True


def _release_startup_lock(lock_path: str) -> None:
    """Release the file lock created by _acquire_startup_lock()."""
    _remove_file_safely(lock_path)


def _resolve_hub_launch_command(
    launcher_script: Optional[str],
) -> tuple[list[str], Optional[str]]:
    """Resolve a launcher-safe hub command for direct files and installed entrypoints."""
    script_path = os.path.abspath(launcher_script or sys.argv[0])
    if os.path.isfile(script_path) and script_path.lower().endswith(".py"):
        return [sys.executable, script_path], os.path.dirname(script_path) or None

    # Installed console-script/uvx shims are not safe to relaunch as
    # ``python <sys.argv[0]>``. Re-enter through the importable server module
    # instead so packaged launches can still auto-start the shared hub.
    if importlib.util.find_spec(_SERVER_MODULE_NAME) is not None:
        return [sys.executable, "-m", _SERVER_MODULE_NAME], _SCRIPT_DIR

    raise SharedTelegramHubError(
        "Shared Telegram hub auto-start failed because no importable launcher "
        "was available. Expected either a .py launcher script or the "
        f"'{_SERVER_MODULE_NAME}' module. argv[0]={script_path!r}"
    )


def _launch_hub_subprocess(
    launch_command: list[str],
    cwd: Optional[str],
    port: int,
    environ: dict[str, str],
) -> subprocess.Popen[Any]:
    """Launch the detached hub subprocess that will own Telegram getUpdates."""
    command = [*launch_command, "--telegram-hub", f"--hub-port={port}"]
    kwargs: dict[str, Any] = {
        "env": environ,
        "cwd": cwd,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = (
            getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        )
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(command, **kwargs)


class SharedTelegramHubClient:
    """HTTP client used by normal server instances in shared Telegram mode."""

    def __init__(self, descriptor: SharedHubDescriptor):
        if requests is None:
            raise ImportError(
                "The 'requests' library is required for shared Telegram hub mode."
            )
        self._descriptor = descriptor

    @property
    def descriptor(self) -> SharedHubDescriptor:
        """Expose the discovered descriptor for status reporting/tests."""
        return self._descriptor

    def wait_for_reply(
        self,
        prompt_message_id: int,
        cancel_event: threading.Event,
        *,
        poll_interval: float = 0.3,
    ) -> Optional[str]:
        """Wait for a routed Telegram reply without using getUpdates locally."""
        details = self.wait_for_reply_details(
            prompt_message_id,
            cancel_event,
            poll_interval=poll_interval,
        )
        if details is None:
            return None
        return str(details.get("text") or "")

    def wait_for_reply_details(
        self,
        prompt_message_id: int,
        cancel_event: threading.Event,
        *,
        poll_interval: float = 0.3,
    ) -> Optional[dict[str, Any]]:
        """Wait for a routed reply and preserve source metadata for callers."""
        while not cancel_event.is_set():
            try:
                response = requests.post(
                    f"{self._descriptor.base_url}/wait_for_reply",
                    json={
                        "prompt_message_id": int(prompt_message_id),
                        "timeout_seconds": _DEFAULT_WAIT_TIMEOUT_SECONDS,
                    },
                    timeout=_DEFAULT_WAIT_TIMEOUT_SECONDS + 2.0,
                )
                response.raise_for_status()
                payload = response.json()
                status = payload.get("status")
                if status == "reply":
                    return {
                        "text": payload.get("reply_text") or "",
                        "source": payload.get("source") or "telegram_reply",
                        "received_at": payload.get("received_at"),
                        "reply_message_id": payload.get("reply_message_id"),
                    }
                if status == "completed":
                    return None
            except Exception as exc:
                if cancel_event.is_set():
                    break
                if "timeout" not in str(exc).lower():
                    print(f"[SharedTelegramHub] wait_for_reply error: {exc}")
                cancel_event.wait(timeout=poll_interval)
        return None

    def complete_prompt(
        self,
        prompt_message_id: int,
        *,
        status: str,
        source: Optional[str] = None,
    ) -> None:
        """Tell the hub that a prompt lifecycle has ended and can be cleaned up."""
        try:
            requests.post(
                f"{self._descriptor.base_url}/complete_prompt",
                json={
                    "prompt_message_id": int(prompt_message_id),
                    "status": status,
                    "source": source,
                },
                timeout=3,
            )
        except Exception as exc:
            print(f"[SharedTelegramHub] complete_prompt error: {exc}")

    def health(self) -> dict[str, Any]:
        """Return hub health metadata for diagnostics and health checks."""
        response = requests.get(f"{self._descriptor.base_url}/health", timeout=2)
        response.raise_for_status()
        return response.json()


def ensure_shared_telegram_hub(
    *,
    credentials: Optional[TelegramCredentials] = None,
    launcher_script: Optional[str] = None,
    startup_timeout: float = 8.0,
) -> SharedTelegramHubClient:
    """Discover or auto-start the shared hub for the current bot/chat scope."""
    creds = credentials or load_telegram_credentials()
    if creds is None:
        raise SharedTelegramHubError(
            "Shared Telegram mode requires TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID "
            "or telegram_config.json credentials."
        )

    existing = find_running_hub_descriptor(creds)
    if existing is not None:
        return SharedTelegramHubClient(existing)

    paths = get_runtime_paths(creds)
    launch_command, launch_cwd = _resolve_hub_launch_command(launcher_script)

    acquired_lock = _acquire_startup_lock(paths.startup_lock_path)
    deadline = time.time() + max(startup_timeout, 1.0)

    if acquired_lock:
        try:
            port = _find_free_port()
            _launch_hub_subprocess(launch_command, launch_cwd, port, dict(os.environ))
            while time.time() < deadline:
                descriptor = find_running_hub_descriptor(creds)
                if descriptor is not None:
                    return SharedTelegramHubClient(descriptor)
                time.sleep(0.2)
        finally:
            _release_startup_lock(paths.startup_lock_path)

    while time.time() < deadline:
        descriptor = find_running_hub_descriptor(creds)
        if descriptor is not None:
            return SharedTelegramHubClient(descriptor)
        time.sleep(0.2)

    raise SharedTelegramHubError(
        "Shared Telegram hub was requested, but no healthy hub became available "
        f"within {startup_timeout:.1f} seconds."
    )


def describe_shared_hub_status(
    credentials: Optional[TelegramCredentials] = None,
) -> dict[str, Any]:
    """Return diagnostics describing the shared hub and runtime scope."""
    creds = credentials or load_telegram_credentials()
    paths = get_runtime_paths(creds)
    descriptor = find_running_hub_descriptor(creds, prune_stale=False)
    healthy = bool(descriptor and _is_descriptor_healthy(descriptor))
    return {
        "mode": get_shared_telegram_mode(),
        "enabled": is_shared_telegram_enabled(),
        "runtime_root": paths.root_dir,
        "runtime_scope": paths.scope_dir,
        "configured": creds is not None,
        "descriptor_present": descriptor is not None,
        "hub_healthy": healthy,
        "descriptor": asdict(descriptor) if descriptor is not None else None,
    }


class SharedTelegramHubService:
    """Host-local HTTP service that owns Telegram getUpdates for one scope."""

    def __init__(
        self,
        credentials: TelegramCredentials,
        *,
        port: int,
        runtime_paths: Optional[RuntimePaths] = None,
        start_poller: bool = True,
    ):
        if requests is None:
            raise ImportError(
                "The 'requests' library is required for shared Telegram hub mode."
            )
        self.credentials = credentials
        self.runtime_paths = runtime_paths or get_runtime_paths(credentials)
        self.host = _HUB_HOST
        self.port = int(port)
        self.start_poller = start_poller
        self.pid = os.getpid()
        self.started_at = _utc_now_iso()
        self._http_server: Optional[ThreadingHTTPServer] = None
        self._http_thread: Optional[threading.Thread] = None
        self._poller_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._update_offset: Optional[int] = None
        self._commands_registered = False
        self._mailboxes: dict[int, _PromptMailbox] = {}
        self._mailboxes_lock = threading.Lock()

    def _api(self, method: str) -> str:
        """Return the Telegram Bot API endpoint URL."""
        return f"https://api.telegram.org/bot{self.credentials.bot_token}/{method}"

    def _log(self, message: str) -> None:
        """Write a UTF-8 log line to the host-global hub log."""
        try:
            with open(self.runtime_paths.hub_log_path, "a", encoding="utf-8") as handle:
                handle.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}\n")
        except Exception:
            pass

    def descriptor(self) -> SharedHubDescriptor:
        """Return the current discovery descriptor for this running hub."""
        return SharedHubDescriptor(
            version=_HUB_VERSION,
            host=self.host,
            port=self.port,
            pid=self.pid,
            scope_key=self.credentials.scope_key,
            started_at=self.started_at,
            script_path=os.path.abspath(sys.argv[0]),
            runtime_scope_dir=self.runtime_paths.scope_dir,
        )

    def _write_descriptor(self) -> None:
        """Persist the discovery descriptor after the HTTP server is listening."""
        _write_descriptor(self.descriptor(), self.runtime_paths.descriptor_path)

    def _flush_updates(self) -> None:
        """Advance the Telegram update offset so the hub only sees fresh messages."""
        try:
            response = requests.get(self._api("getUpdates"), params={"timeout": 0}, timeout=5)
            if response.status_code != 200:
                return
            updates = response.json().get("result", [])
            if updates:
                self._update_offset = updates[-1]["update_id"] + 1
        except Exception as exc:
            self._log(f"flush_updates failed: {exc}")

    def _send_text(self, text: str) -> None:
        """Send a plain text chat message for bypass command responses."""
        try:
            requests.post(
                self._api("sendMessage"),
                json={"chat_id": self.credentials.chat_id, "text": text},
                timeout=10,
            )
        except Exception as exc:
            self._log(f"send_text failed: {exc}")

    def _react_to_message(self, message_id: int, emoji: str = "👍") -> bool:
        """Apply the shared-mode visual ack for an accepted direct reply."""
        try:
            response = requests.post(
                self._api("setMessageReaction"),
                json={
                    "chat_id": self.credentials.chat_id,
                    "message_id": int(message_id),
                    "reaction": [{"type": "emoji", "emoji": emoji}],
                    "is_big": False,
                },
                timeout=10,
            )
            return response.status_code == 200
        except Exception as exc:
            self._log(f"react_to_message failed: {exc}")
            return False

    def _set_my_commands(self) -> None:
        """Register the Telegram command menu from the sole polling owner."""
        if self._commands_registered:
            return
        try:
            response = requests.post(
                self._api("setMyCommands"),
                json={
                    "commands": [
                        {"command": "bypass", "description": "Show bypass mode status"},
                        {
                            "command": "bypass_on",
                            "description": "Activate bypass (auto-approve all). Usage: /bypass_on [minutes]",
                        },
                        {
                            "command": "bypass_off",
                            "description": "Deactivate bypass (require human approval)",
                        },
                    ]
                },
                timeout=10,
            )
            self._commands_registered = response.status_code == 200 and response.json().get("ok", False)
        except Exception as exc:
            self._log(f"setMyCommands failed: {exc}")

    def _handle_bypass_command(self, text: str) -> None:
        """Apply /bypass commands against the host-global bypass lock file."""
        normalized = text.replace("/bypass_on", "/bypass on").replace("/bypass_off", "/bypass off")
        parts = normalized.split()
        bypass_lock = self.runtime_paths.bypass_lock_path

        def _read_state() -> Optional[dict[str, Any]]:
            if not os.path.exists(bypass_lock):
                return None
            try:
                with open(bypass_lock, "r", encoding="utf-8") as handle:
                    return json.load(handle)
            except Exception:
                return None

        def _activate(duration_minutes: Optional[int]) -> None:
            now = datetime.now(timezone.utc)
            payload = {
                "activated_at": now.isoformat(),
                "expires_at": (
                    now + timedelta(minutes=duration_minutes)
                ).isoformat() if duration_minutes else None,
                "source": "telegram-hub",
            }
            with open(bypass_lock, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2)

        def _deactivate() -> None:
            _remove_file_safely(bypass_lock)

        if len(parts) >= 2 and parts[1] == "off":
            _deactivate()
            self._send_text("✅ Bypass mode deactivated. Human approval is now required.")
            return

        if len(parts) >= 2 and parts[1] == "on":
            duration: Optional[int] = None
            if len(parts) >= 3:
                try:
                    duration = int(parts[2])
                except ValueError:
                    self._send_text(f"⚠️ Invalid duration: {parts[2]}. Use /bypass on [minutes]")
                    return
            _activate(duration)
            if duration:
                self._send_text(f"⚡ Bypass mode activated for {duration} minutes.")
            else:
                self._send_text("⚡ Bypass mode activated with no expiry.")
            return

        if len(parts) == 1:
            state = _read_state()
            if state:
                self._send_text(
                    "ℹ️ Bypass is ACTIVE\n"
                    f"Activated: {state.get('activated_at', 'unknown')}\n"
                    f"Expires: {state.get('expires_at', 'no expiry')}\n"
                    f"Source: {state.get('source', 'unknown')}"
                )
            else:
                self._send_text("ℹ️ Bypass is INACTIVE. Send /bypass on to activate.")
            return

        self._send_text("Usage: /bypass on [minutes] | /bypass off | /bypass")

    def submit_reply(
        self,
        prompt_message_id: int,
        reply_text: str,
        reply_message_id: Optional[int] = None,
        *,
        source: str = "telegram_reply",
    ) -> bool:
        """Store the first accepted Telegram reply and wake any waiting clients."""
        with self._mailboxes_lock:
            mailbox = self._mailboxes.setdefault(prompt_message_id, _PromptMailbox(event=threading.Event()))
            if mailbox.completed:
                mailbox.updated_at = time.time()
                return False
            mailbox.reply_text = reply_text
            mailbox.reply_message_id = reply_message_id
            mailbox.reply_source = source
            mailbox.reply_received_at = time.monotonic_ns()
            mailbox.completed = True
            mailbox.completion_status = "reply"
            mailbox.updated_at = time.time()
            mailbox.event.set()
            return True

    def complete_prompt(self, prompt_message_id: int, status: str) -> None:
        """Mark a prompt as completed locally so late Telegram replies are ignored."""
        with self._mailboxes_lock:
            mailbox = self._mailboxes.setdefault(prompt_message_id, _PromptMailbox(event=threading.Event()))
            mailbox.completed = True
            mailbox.completion_status = status
            mailbox.updated_at = time.time()
            mailbox.event.set()

    def wait_for_reply(self, prompt_message_id: int, timeout_seconds: float) -> dict[str, Any]:
        """Block until a matching reply or prompt completion arrives."""
        with self._mailboxes_lock:
            mailbox = self._mailboxes.setdefault(prompt_message_id, _PromptMailbox(event=threading.Event()))

        if mailbox.event.wait(timeout=max(timeout_seconds, 0.1)):
            with self._mailboxes_lock:
                reply_text = mailbox.reply_text
                completion_status = mailbox.completion_status
                reply_message_id = mailbox.reply_message_id
                if mailbox.completed:
                    # Keep the mailbox for a short grace period so duplicate waiters
                    # observe the same terminal state, then allow periodic cleanup.
                    mailbox.updated_at = time.time()
            if reply_text is not None:
                return {
                    "status": "reply",
                    "reply_text": reply_text,
                    "reply_message_id": reply_message_id,
                    "source": mailbox.reply_source or "telegram_reply",
                    "received_at": mailbox.reply_received_at,
                }
            return {
                "status": "completed",
                "completion_status": completion_status,
            }

        return {"status": "timeout"}

    def _cleanup_mailboxes(self, max_age_seconds: float = 900.0) -> None:
        """Drop terminal prompt state after a grace period to cap memory growth."""
        cutoff = time.time() - max_age_seconds
        with self._mailboxes_lock:
            stale_ids = [
                prompt_id
                for prompt_id, mailbox in self._mailboxes.items()
                if mailbox.completed and mailbox.updated_at < cutoff
            ]
            for prompt_id in stale_ids:
                self._mailboxes.pop(prompt_id, None)

    def _process_update(self, update: dict[str, Any]) -> None:
        """Route incoming Telegram updates to bypass commands or prompt waiters."""
        message = update.get("message") or {}
        chat_id = str(message.get("chat", {}).get("id", ""))
        if chat_id != self.credentials.chat_id:
            return

        text = (message.get("text") or "").strip()
        if text.startswith("/bypass"):
            self._handle_bypass_command(text.lower())
            return

        reply_to = message.get("reply_to_message") or {}
        prompt_message_id = reply_to.get("message_id")
        if isinstance(prompt_message_id, int):
            accepted = self.submit_reply(
                prompt_message_id,
                text,
                reply_message_id=message.get("message_id"),
                source="telegram_reply",
            )
            if accepted:
                reply_message_id = message.get("message_id")
                if isinstance(reply_message_id, int):
                    self._react_to_message(reply_message_id, "👍")

    def _poll_loop(self) -> None:
        """Own the Telegram getUpdates loop for this host-local bot/chat scope."""
        self._set_my_commands()
        self._flush_updates()

        while not self._stop_event.is_set():
            try:
                params: dict[str, Any] = {
                    "timeout": 3,
                    "allowed_updates": ["message"],
                }
                if self._update_offset is not None:
                    params["offset"] = self._update_offset
                response = requests.get(self._api("getUpdates"), params=params, timeout=10)
                if response.status_code != 200:
                    self._stop_event.wait(timeout=1.0)
                    continue
                payload = response.json()
                for update in payload.get("result", []):
                    self._update_offset = update["update_id"] + 1
                    self._process_update(update)
                self._cleanup_mailboxes()
            except Exception as exc:
                self._log(f"poll_loop error: {exc}")
                self._stop_event.wait(timeout=1.0)

    def start(self) -> None:
        """Start the HTTP service and, optionally, the Telegram polling thread."""

        service = self

        class _Handler(BaseHTTPRequestHandler):
            """Minimal JSON API used by sibling worktrees to talk to the hub."""

            server_version = "SharedTelegramHub/1.0"

            def _read_json(self) -> dict[str, Any]:
                length = int(self.headers.get("Content-Length", "0") or "0")
                raw = self.rfile.read(length) if length else b"{}"
                return json.loads(raw.decode("utf-8") or "{}")

            def _write_json(self, status_code: int, payload: dict[str, Any]) -> None:
                data = json.dumps(payload).encode("utf-8")
                self.send_response(status_code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
                if self.path != "/health":
                    self._write_json(404, {"ok": False, "error": "not_found"})
                    return
                descriptor = service.descriptor()
                self._write_json(
                    200,
                    {
                        "ok": True,
                        "pid": descriptor.pid,
                        "scope_key": descriptor.scope_key,
                        "port": descriptor.port,
                        "runtime_scope_dir": descriptor.runtime_scope_dir,
                    },
                )

            def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
                try:
                    payload = self._read_json()
                    if self.path == "/wait_for_reply":
                        prompt_message_id = int(payload["prompt_message_id"])
                        timeout_seconds = float(payload.get("timeout_seconds", _DEFAULT_WAIT_TIMEOUT_SECONDS))
                        self._write_json(200, service.wait_for_reply(prompt_message_id, timeout_seconds))
                        return
                    if self.path == "/complete_prompt":
                        prompt_message_id = int(payload["prompt_message_id"])
                        status = str(payload.get("status") or "completed")
                        service.complete_prompt(prompt_message_id, status)
                        self._write_json(200, {"ok": True})
                        return
                except Exception as exc:
                    self._write_json(400, {"ok": False, "error": str(exc)})
                    return
                self._write_json(404, {"ok": False, "error": "not_found"})

            def log_message(self, format: str, *args: Any) -> None:
                # Suppress noisy BaseHTTPRequestHandler stderr logging.
                return

        self._http_server = ThreadingHTTPServer((self.host, self.port), _Handler)
        self.port = int(self._http_server.server_address[1])
        self._http_thread = threading.Thread(
            target=self._http_server.serve_forever,
            daemon=True,
            name="shared-telegram-hub-http",
        )
        self._http_thread.start()
        self._write_descriptor()

        if self.start_poller:
            self._poller_thread = threading.Thread(
                target=self._poll_loop,
                daemon=True,
                name="shared-telegram-hub-poller",
            )
            self._poller_thread.start()

    def stop(self) -> None:
        """Stop the HTTP server, polling thread, and descriptor publication."""
        self._stop_event.set()

        if self._http_server is not None:
            self._http_server.shutdown()
            self._http_server.server_close()
        if self._http_thread is not None and self._http_thread.is_alive():
            self._http_thread.join(timeout=5)
        if self._poller_thread is not None and self._poller_thread.is_alive():
            self._poller_thread.join(timeout=5)

        descriptor = _read_descriptor(self.runtime_paths.descriptor_path)
        if descriptor is not None and descriptor.pid == self.pid:
            _remove_file_safely(self.runtime_paths.descriptor_path)


def run_shared_hub_from_argv(argv: Optional[list[str]] = None) -> int:
    """CLI entry point used when the main server auto-starts the hub subprocess."""
    args = list(argv if argv is not None else sys.argv[1:])
    hub_mode = "--telegram-hub" in args
    if not hub_mode:
        return 1

    port = 0
    for item in args:
        if item.startswith("--hub-port="):
            port = int(item.split("=", 1)[1])

    credentials = load_telegram_credentials()
    if credentials is None:
        raise SharedTelegramHubError(
            "The shared Telegram hub cannot start without configured Telegram credentials."
        )

    service = SharedTelegramHubService(credentials, port=port or _find_free_port())
    service.start()
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        return 0
    finally:
        service.stop()