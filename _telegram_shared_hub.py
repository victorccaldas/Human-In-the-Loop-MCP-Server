"""Shared Telegram hub for safely multiplexing Bot API polling on one host.

This module centralizes Telegram ``getUpdates`` ownership behind a single
host-local HTTP service. Multiple HITL-MCP server instances can still send
messages directly, but inbound Telegram polling is routed through this hub so
only one process consumes updates for a given bot/chat pair on the host.
"""

from __future__ import annotations

import atexit
import contextlib
import hashlib
import importlib.util
import json
import os
import platform
import re
import signal
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

from _hitl_logs import get_shared_telegram_logs_dir

try:
    import requests
except ImportError:  # pragma: no cover - validated by the caller at runtime
    requests = None  # type: ignore[assignment]


_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_CONFIG = os.path.join(_SCRIPT_DIR, "telegram_config.json")
_DIALOG_CONFIG_FILE = os.path.join(_SCRIPT_DIR, "dialog_config.json")
_HUB_HOST = "127.0.0.1"
_HUB_VERSION = 1
_DEFAULT_WAIT_TIMEOUT_SECONDS = 5.0

# ── Lifecycle management constants ──────────────────────────────────────────
# Hub auto-terminates when no MCP clients are connected after a grace period.
_HEARTBEAT_INTERVAL_SECONDS = 30.0    # Client sends heartbeat every 30s
_HEARTBEAT_STALE_SECONDS = 60.0       # 2 missed heartbeats = stale
_IDLE_SHUTDOWN_SECONDS = 15.0         # Grace period after last client gone
_STARTUP_GRACE_SECONDS = 60.0         # Don't check idle within first 60s
_CLIENT_MONITOR_INTERVAL = 10.0       # Check client liveness every 10s

# ── Client-side heartbeat state ─────────────────────────────────────────────
# Tracks the background heartbeat thread that keeps this process registered
# with the shared hub so the hub's idle-shutdown monitor counts us as alive.
_heartbeat_thread: Optional[threading.Thread] = None
_heartbeat_stop: Optional[threading.Event] = None
_heartbeat_lock = threading.Lock()
_active_heartbeat_descriptor_url: Optional[str] = None

_SHARED_MODE_ENV = "HITL_SHARED_TELEGRAM_MODE"
_RUNTIME_DIR_ENV = "HITL_MCP_RUNTIME_DIR"
_AUTO_MESSAGES_CONFIG_ENV = "HITL_MCP_AUTO_MESSAGES_CONFIG"
_AUTO_MESSAGES_CONFIG_FILENAME = "vscode-auto-messages.local.json"
_AUTO_MESSAGES_SETTINGS_PATH_KEY = "vscode_settings_path"
_AUTO_APPROVAL_ALLOWED_KEY = "_chat.tools.eligibleForAutoApproval"
_AUTO_APPROVAL_BLOCKED_KEY = "chat.tools.eligibleForAutoApproval"
_BLOCK_AUTO_MESSAGES_COMMAND = "/bloquear_mensagens_automaticas"
_ALLOW_AUTO_MESSAGES_COMMAND = "/permitir_mensagens_automaticas"
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
    poll_lock_path: str


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


def get_auto_messages_local_config_path() -> str:
    """Return the host-local config file used by Telegram admin commands.

    This path intentionally lives outside the git worktree so each host/user can
    point the Telegram commands at a private VS Code ``settings.json`` location
    without committing machine-specific paths.
    """
    override = os.environ.get(_AUTO_MESSAGES_CONFIG_ENV, "").strip()
    if override:
        return os.path.abspath(os.path.expanduser(override))
    return os.path.join(get_host_runtime_root(), _AUTO_MESSAGES_CONFIG_FILENAME)


def get_telegram_admin_commands() -> list[dict[str, str]]:
    """Return the shared Telegram command menu for direct and hub modes."""
    return [
        {"command": "bypass", "description": "Show bypass mode status"},
        {
            "command": "bypass_on",
            "description": "Activate bypass (auto-approve all). Usage: /bypass_on [minutes]",
        },
        {
            "command": "bypass_off",
            "description": "Deactivate bypass (require human approval)",
        },
        {
            "command": "bloquear_mensagens_automaticas",
            "description": "Block automatic VS Code approval messages",
        },
        {
            "command": "permitir_mensagens_automaticas",
            "description": "Allow automatic VS Code approval messages",
        },
        {
            "command": "tkinter_sound_on",
            "description": "Enable tkinter beeps for new get_remote_input dialogs",
        },
        {
            "command": "tkinter_sound_off",
            "description": "Disable tkinter beeps for new get_remote_input dialogs",
        },
    ]


def _load_dialog_config() -> dict[str, Any]:
    if not os.path.isfile(_DIALOG_CONFIG_FILE):
        return {}
    try:
        with open(_DIALOG_CONFIG_FILE, "r", encoding="utf-8") as handle:
            loaded = json.load(handle)
        return loaded if isinstance(loaded, dict) else {}
    except Exception:
        return {}


def _save_dialog_config(config_data: dict[str, Any]) -> None:
    with open(_DIALOG_CONFIG_FILE, "w", encoding="utf-8") as handle:
        json.dump(config_data, handle, indent=2)


def set_remote_input_notifications_enabled(enabled: bool) -> dict[str, Any]:
    data = _load_dialog_config()
    data["remote_input_notifications_enabled"] = bool(enabled)
    _save_dialog_config(data)
    return {
        "status": "success",
        "enabled": bool(enabled),
        "config_path": _DIALOG_CONFIG_FILE,
    }


def handle_remote_input_notification_telegram_command(text: str) -> Optional[str]:
    token = _get_telegram_command_token(text)
    if token not in {
        "/tkinter_sound_on",
        "/tkinter_sound_off",
        "/tkinter_sound",
    }:
        return None
    if token == "/tkinter_sound":
        enabled = bool(_load_dialog_config().get("remote_input_notifications_enabled", True))
        state = "ativadas" if enabled else "desativadas"
        return f"ℹ️ Notificações de get_remote_input estão {state}."
    enabled = token == "/tkinter_sound_on"
    result = set_remote_input_notifications_enabled(enabled)
    state = "ativadas" if result["enabled"] else "desativadas"
    return f"🔔 Notificações de get_remote_input {state}."


def _get_telegram_command_token(text: str) -> str:
    """Return the normalized Telegram command token, ignoring any bot mention."""
    stripped = (text or "").strip()
    if not stripped:
        return ""
    first_token = stripped.split(None, 1)[0]
    return first_token.split("@", 1)[0].lower()


def _count_json_property_key_occurrences(content: str, key: str) -> int:
    """Count exact JSON/JSONC property-key matches without reformatting the file."""
    pattern = re.compile(rf'"{re.escape(key)}"\s*:')
    return len(pattern.findall(content))


def _rename_json_property_key(content: str, source_key: str, target_key: str) -> tuple[str, int]:
    """Rename one exact JSON/JSONC property key while preserving surrounding formatting."""
    pattern = re.compile(rf'"{re.escape(source_key)}"(?P<separator>\s*:)')
    return pattern.subn(f'"{target_key}"\\g<separator>', content, count=1)


def toggle_vscode_auto_message_setting(
    *,
    block_automatic_messages: bool,
    local_config_path: Optional[str] = None,
) -> dict[str, Any]:
    """Toggle the VS Code auto-approval setting via a host-local config file.

    The implementation intentionally edits only the targeted key name in the
    settings file content so comments and formatting in VS Code's JSONC file are
    preserved verbatim.
    """
    config_path = os.path.abspath(
        os.path.expanduser(local_config_path or get_auto_messages_local_config_path())
    )
    source_key = (
        _AUTO_APPROVAL_ALLOWED_KEY
        if block_automatic_messages
        else _AUTO_APPROVAL_BLOCKED_KEY
    )
    target_key = (
        _AUTO_APPROVAL_BLOCKED_KEY
        if block_automatic_messages
        else _AUTO_APPROVAL_ALLOWED_KEY
    )
    desired_state = "blocked" if block_automatic_messages else "allowed"
    result: dict[str, Any] = {
        "status": "unknown",
        "desired_state": desired_state,
        "config_path": config_path,
        "settings_path": None,
        "source_key": source_key,
        "target_key": target_key,
    }

    if not os.path.isfile(config_path):
        result["status"] = "missing_local_config"
        return result

    try:
        with open(config_path, "r", encoding="utf-8") as handle:
            config_data = json.load(handle)
    except Exception as exc:
        result["status"] = "missing_local_config"
        result["error"] = str(exc)
        return result

    raw_settings_path = config_data.get(_AUTO_MESSAGES_SETTINGS_PATH_KEY)
    if not isinstance(raw_settings_path, str) or not raw_settings_path.strip():
        result["status"] = "missing_settings_path"
        return result

    settings_path = os.path.abspath(os.path.expanduser(raw_settings_path.strip()))
    result["settings_path"] = settings_path
    if not os.path.isfile(settings_path):
        result["status"] = "missing_settings_file"
        return result

    try:
        with open(settings_path, "r", encoding="utf-8", newline="") as handle:
            original_content = handle.read()
    except Exception as exc:
        result["status"] = "write_failure"
        result["error"] = str(exc)
        return result

    source_count = _count_json_property_key_occurrences(original_content, source_key)
    target_count = _count_json_property_key_occurrences(original_content, target_key)
    result["source_count"] = source_count
    result["target_count"] = target_count

    if source_count == 0 and target_count == 1:
        result["status"] = "already-in-target-state"
        return result

    if source_count != 1 or target_count != 0:
        result["status"] = "conflict"
        if source_count > 0 and target_count > 0:
            result["reason"] = "both_keys_present"
        elif source_count == 0 and target_count == 0:
            result["reason"] = "expected_key_not_found"
        elif source_count > 1:
            result["reason"] = "multiple_source_keys_present"
        else:
            result["reason"] = "multiple_target_keys_present"
        return result

    updated_content, replacements = _rename_json_property_key(
        original_content,
        source_key,
        target_key,
    )
    if replacements != 1:
        result["status"] = "conflict"
        result["reason"] = "rename_not_unique"
        return result

    temp_path = f"{settings_path}.tmp"
    try:
        with open(temp_path, "w", encoding="utf-8", newline="") as handle:
            handle.write(updated_content)
        os.replace(temp_path, settings_path)
        result["status"] = "success"
        return result
    except Exception as exc:
        result["status"] = "write_failure"
        result["error"] = str(exc)
        with contextlib.suppress(FileNotFoundError):
            os.remove(temp_path)
        return result


def format_vscode_auto_message_toggle_result(result: dict[str, Any]) -> str:
    """Convert a toggle result into a concise Telegram status message."""
    desired_state = str(result.get("desired_state") or "blocked")
    config_path = str(result.get("config_path") or get_auto_messages_local_config_path())
    settings_path = result.get("settings_path")
    source_key = str(result.get("source_key") or "")
    target_key = str(result.get("target_key") or "")
    action_label = "bloqueadas" if desired_state == "blocked" else "permitidas"
    status = str(result.get("status") or "unknown")

    if status == "success":
        return (
            f"✅ Mensagens automáticas {action_label} com sucesso.\n"
            f"Arquivo: {settings_path}\n"
            f"Chave alterada: \"{source_key}\" → \"{target_key}\""
        )

    if status == "already-in-target-state":
        return (
            f"ℹ️ Mensagens automáticas já estão {action_label}.\n"
            f"Arquivo: {settings_path}\n"
            f"Chave ativa: \"{target_key}\""
        )

    if status == "missing_local_config":
        details = ""
        if result.get("error"):
            details = f"\nDetalhe: {result['error']}"
        return (
            "⚠️ Configuração local ausente para controlar as mensagens automáticas.\n"
            f"Arquivo esperado: {config_path}{details}\n"
            "Crie um JSON local com a chave \"vscode_settings_path\" apontando para o settings.json do VS Code."
        )

    if status == "missing_settings_path":
        return (
            "⚠️ A configuração local existe, mas não define \"vscode_settings_path\".\n"
            f"Arquivo: {config_path}"
        )

    if status == "missing_settings_file":
        return (
            "⚠️ O settings.json configurado não foi encontrado.\n"
            f"Configuração: {config_path}\n"
            f"Caminho informado: {settings_path}"
        )

    if status == "conflict":
        return (
            "⚠️ Conflito ao atualizar o settings.json; nenhuma alteração foi aplicada.\n"
            f"Arquivo: {settings_path}\n"
            f"Estado detectado: source={result.get('source_count', 0)}, target={result.get('target_count', 0)}, reason={result.get('reason', 'unknown')}\n"
            f"Chaves esperadas: \"{source_key}\" / \"{target_key}\""
        )

    if status == "write_failure":
        return (
            "❌ Falha ao gravar o settings.json do VS Code.\n"
            f"Arquivo: {settings_path}\n"
            f"Erro: {result.get('error', 'unknown')}"
        )

    return (
        "❌ Não foi possível atualizar o controle de mensagens automáticas.\n"
        f"Status: {status}"
    )


def handle_vscode_auto_message_telegram_command(text: str) -> Optional[str]:
    """Handle Telegram commands that toggle the local VS Code auto-message key."""
    command = _get_telegram_command_token(text)
    if command == _BLOCK_AUTO_MESSAGES_COMMAND:
        return format_vscode_auto_message_toggle_result(
            toggle_vscode_auto_message_setting(block_automatic_messages=True)
        )
    if command == _ALLOW_AUTO_MESSAGES_COMMAND:
        return format_vscode_auto_message_toggle_result(
            toggle_vscode_auto_message_setting(block_automatic_messages=False)
        )
    return None


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
    repo_logs_dir = get_shared_telegram_logs_dir(scope_key)
    return RuntimePaths(
        root_dir=root_dir,
        scope_dir=scope_dir,
        descriptor_path=os.path.join(scope_dir, "hub-descriptor.json"),
        startup_lock_path=os.path.join(scope_dir, "hub-start.lock"),
        bypass_lock_path=os.path.join(scope_dir, "bypass_active.lock"),
        bypass_log_path=os.path.join(repo_logs_dir, "bypass_log.jsonl"),
        hub_log_path=os.path.join(repo_logs_dir, "hub.log"),
        poll_lock_path=os.path.join(scope_dir, "poll-owner.lock"),
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


def _probe_descriptor_health(
    descriptor: SharedHubDescriptor,
    timeout: float = 1.5,
) -> dict[str, Any]:
    """Probe the HTTP health endpoint and report readiness details.

    The hub descriptor can exist before Telegram polling finishes bootstrapping,
    so callers need to distinguish a reachable hub that is still starting from
    a stale descriptor that points nowhere.
    """
    state: dict[str, Any] = {
        "reachable": False,
        "status_code": None,
        "scope_matches": False,
        "ok": False,
        "ready": False,
        "payload": None,
        "error": None,
    }
    if requests is None:
        state["error"] = "requests_unavailable"
        return state
    try:
        response = requests.get(f"{descriptor.base_url}/health", timeout=timeout)
        state["status_code"] = response.status_code
        if response.status_code != 200:
            return state
        data = response.json()
        state["reachable"] = True
        state["payload"] = data
        state["ok"] = bool(data.get("ok"))
        state["scope_matches"] = data.get("scope_key") == descriptor.scope_key
        # Readiness is stricter than reachability: the hub must have completed
        # poller bootstrap before sibling worktrees consider it usable.
        state["ready"] = bool(data.get("ready", data.get("ok"))) and bool(
            state["scope_matches"]
        )
        return state
    except Exception as exc:
        state["error"] = str(exc)
        return state


def _is_descriptor_healthy(descriptor: SharedHubDescriptor, timeout: float = 1.5) -> bool:
    """Return True only when the descriptor points at a ready shared hub."""
    return bool(_probe_descriptor_health(descriptor, timeout=timeout).get("ready"))


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
    probe = _probe_descriptor_health(descriptor)
    if probe.get("ready"):
        return descriptor
    # Reachable hubs can legitimately report not-ready while the Telegram
    # poller is still bootstrapping; keep the descriptor so peers can wait.
    if probe.get("reachable"):
        return None
    # If startup orchestration is still in progress, avoid racing the launcher
    # by pruning the descriptor before the new hub can finish bootstrapping.
    if os.path.exists(paths.startup_lock_path):
        return None
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
    for attempt in range(3):
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(lock_path) > stale_after_seconds:
                    _remove_file_safely(lock_path)
                    time.sleep(0.05 * (attempt + 1))
                    continue
                return False
            except FileNotFoundError:
                time.sleep(0.05 * (attempt + 1))
                continue
    else:
        return False

    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(f"{os.getpid()}\n")
    return True


def _release_startup_lock(lock_path: str) -> None:
    """Release the file lock created by _acquire_startup_lock()."""
    _remove_file_safely(lock_path)


def _is_pid_alive_standalone(pid: int) -> bool:
    """Check if a process with given PID is still running (module-level).

    On Windows, os.kill(pid, 0) is unreliable from DETACHED_PROCESS because
    signal 0 maps to CTRL_C_EVENT which requires a console. We use
    OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION) via ctypes instead.
    """
    if os.name == "nt":
        import ctypes
        _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            _PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    # Unix: signal 0 correctly checks process existence
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # Process exists but we lack permission
    except OSError:
        return False


def _get_process_commandline(pid: int) -> str:
    """Return the best-effort command line for *pid*, or an empty string."""
    try:
        if platform.system() == "Windows":
            result = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    (
                        "(Get-CimInstance Win32_Process -Filter \"ProcessId = "
                        f"{pid}\").CommandLine"
                    ),
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                return result.stdout.strip()
            return ""
        result = subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        return ""
    return ""


def _process_looks_like_shared_hub(pid: int) -> bool:
    """Return True only when *pid* appears to be a detached hub subprocess."""
    command_line = _get_process_commandline(pid)
    if not command_line:
        return False
    lowered = command_line.lower()
    return "--telegram-hub" in lowered and _SERVER_MODULE_NAME in lowered


def _kill_process(pid: int, log_fn=print) -> bool:
    """Attempt to terminate a process by PID. Returns True if process is dead after attempt."""
    if not _process_looks_like_shared_hub(pid):
        log_fn(
            f"Refusing to kill PID {pid}: process does not look like a shared Telegram hub"
        )
        return False
    log_fn(f"Attempting to kill competing process PID {pid}")
    try:
        if platform.system() == "Windows":
            result = subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)],
                capture_output=True, text=True, timeout=5,
            )
            log_fn(f"taskkill result: returncode={result.returncode}, stdout={result.stdout.strip()}")
        else:
            os.kill(pid, signal.SIGTERM)
            log_fn(f"Sent SIGTERM to PID {pid}")
    except Exception as exc:
        log_fn(f"Failed to kill PID {pid}: {exc}")
        return False
    # Wait up to 3 seconds for process to die
    for _ in range(30):
        if not _is_pid_alive_standalone(pid):
            log_fn(f"Process PID {pid} confirmed dead")
            return True
        time.sleep(0.1)
    log_fn(f"Process PID {pid} still alive after kill attempt")
    return False


def _acquire_poll_lock(lock_path: str, log_fn=print) -> bool:
    """Acquire an exclusive polling lock. Kills stale owners if necessary."""
    my_pid = os.getpid()
    if os.path.isfile(lock_path):
        try:
            with open(lock_path, "r", encoding="utf-8") as f:
                existing_pid = int(f.read().strip())
        except (ValueError, OSError):
            existing_pid = None
        if existing_pid is not None and existing_pid != my_pid:
            if _is_pid_alive_standalone(existing_pid):
                log_fn(f"Poll lock held by live process PID {existing_pid} \u2014 attempting to kill")
                killed = _kill_process(existing_pid, log_fn)
                if not killed:
                    log_fn(f"FATAL: Could not kill competing process PID {existing_pid}")
                    return False
            else:
                log_fn(f"Poll lock held by dead process PID {existing_pid} \u2014 stealing lock")
    # Write our PID
    try:
        with open(lock_path, "w", encoding="utf-8") as f:
            f.write(f"{my_pid}\n")
        log_fn(f"Poll lock acquired (PID {my_pid})")
        return True
    except OSError as exc:
        log_fn(f"Failed to write poll lock: {exc}")
        return False


def _release_poll_lock(lock_path: str) -> None:
    """Release the polling lock only if we own it."""
    try:
        if os.path.isfile(lock_path):
            with open(lock_path, "r", encoding="utf-8") as f:
                owner_pid = int(f.read().strip())
            if owner_pid == os.getpid():
                _remove_file_safely(lock_path)
    except (ValueError, OSError):
        _remove_file_safely(lock_path)  # Corrupt lock file \u2014 clean up


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
                    from _hitl_logs import append_log_line, get_remote_input_diag_log_path
                    append_log_line(
                        get_remote_input_diag_log_path(),
                        f"[hub_client] wait_for_reply exception (prompt_msg_id={prompt_message_id}): {type(exc).__name__}: {exc}"
                    )
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
            from _hitl_logs import append_log_line, get_remote_input_diag_log_path
            append_log_line(
                get_remote_input_diag_log_path(),
                f"[hub_client] complete_prompt exception (prompt_msg_id={prompt_message_id}, status={status}): {type(exc).__name__}: {exc}"
            )
            print(f"[SharedTelegramHub] complete_prompt error: {exc}")

    def health(self) -> dict[str, Any]:
        """Return hub health metadata for diagnostics and health checks."""
        response = requests.get(f"{self._descriptor.base_url}/health", timeout=2)
        response.raise_for_status()
        return response.json()


# ── Client-side heartbeat & deregistration ──────────────────────────────────
# These module-level functions run in the MCP client process (not the hub) and
# keep the hub informed that this process is still alive.  The hub uses the
# heartbeat signal to avoid idle shutdown while at least one client is active.

def _client_heartbeat_loop(base_url: str, stop_event: threading.Event) -> None:
    """Send periodic heartbeats to the shared hub."""
    from _hitl_logs import append_log_line, get_remote_input_diag_log_path
    while not stop_event.is_set():
        try:
            requests.post(
                f"{base_url}/heartbeat",
                json={"pid": os.getpid()},
                timeout=5,
            )
        except Exception as exc:
            append_log_line(
                get_remote_input_diag_log_path(),
                f"[heartbeat] POST {base_url}/heartbeat failed: {type(exc).__name__}: {exc}"
            )
        stop_event.wait(timeout=_HEARTBEAT_INTERVAL_SECONDS)


def _start_client_heartbeat(base_url: str) -> None:
    """Start the heartbeat daemon thread (idempotent, URL-aware)."""
    from _hitl_logs import append_log_line, get_remote_input_diag_log_path
    global _heartbeat_thread, _heartbeat_stop, _active_heartbeat_descriptor_url
    previous_thread: Optional[threading.Thread] = None
    with _heartbeat_lock:
        if _heartbeat_thread is not None and _heartbeat_thread.is_alive():
            if _active_heartbeat_descriptor_url == base_url:
                return  # Same hub, already running
            # Hub changed (e.g., restarted on different port) — stop old heartbeat
            if _heartbeat_stop is not None:
                _heartbeat_stop.set()
            previous_thread = _heartbeat_thread

    if previous_thread is not None and previous_thread.is_alive():
        previous_thread.join(timeout=6.0)
        if previous_thread.is_alive():
            append_log_line(
                get_remote_input_diag_log_path(),
                f"[heartbeat] Previous heartbeat thread did not stop within 6s join timeout (url={_active_heartbeat_descriptor_url})"
            )

    with _heartbeat_lock:
        if _heartbeat_thread is not None and _heartbeat_thread.is_alive():
            if _active_heartbeat_descriptor_url == base_url:
                return
        _heartbeat_stop = threading.Event()
        _active_heartbeat_descriptor_url = base_url
        _heartbeat_thread = threading.Thread(
            target=_client_heartbeat_loop,
            args=(base_url, _heartbeat_stop),
            daemon=True,
            name="hub-client-heartbeat",
        )
        _heartbeat_thread.start()
        append_log_line(
            get_remote_input_diag_log_path(),
            f"[heartbeat] Started client heartbeat for hub {base_url}"
        )


def _stop_client_heartbeat() -> None:
    """Stop the heartbeat daemon thread."""
    from _hitl_logs import append_log_line, get_remote_input_diag_log_path
    global _heartbeat_thread, _heartbeat_stop, _active_heartbeat_descriptor_url
    previous_thread: Optional[threading.Thread] = None
    stopped_url = _active_heartbeat_descriptor_url
    with _heartbeat_lock:
        if _heartbeat_stop is not None:
            _heartbeat_stop.set()
        previous_thread = _heartbeat_thread
        _heartbeat_thread = None
        _heartbeat_stop = None
        _active_heartbeat_descriptor_url = None
    if previous_thread is not None and previous_thread.is_alive():
        previous_thread.join(timeout=6.0)
        if previous_thread.is_alive():
            append_log_line(
                get_remote_input_diag_log_path(),
                f"[heartbeat] Heartbeat thread did not stop within 6s join timeout (url={stopped_url})"
            )
        else:
            append_log_line(
                get_remote_input_diag_log_path(),
                f"[heartbeat] Stopped client heartbeat for hub {stopped_url}"
            )


def _atexit_deregister() -> None:
    """Best-effort deregistration on process exit."""
    url = _active_heartbeat_descriptor_url
    if url:
        try:
            requests.post(
                f"{url}/deregister",
                json={"pid": os.getpid()},
                timeout=3,
            )
        except Exception:
            pass
    _stop_client_heartbeat()


atexit.register(_atexit_deregister)


def ensure_shared_telegram_hub(
    *,
    credentials: Optional[TelegramCredentials] = None,
    launcher_script: Optional[str] = None,
    startup_timeout: float = 60.0,
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
        client = SharedTelegramHubClient(existing)
        _start_client_heartbeat(client._descriptor.base_url)
        return client

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
                    client = SharedTelegramHubClient(descriptor)
                    _start_client_heartbeat(client._descriptor.base_url)
                    return client
                time.sleep(0.2)
        finally:
            _release_startup_lock(paths.startup_lock_path)

    while time.time() < deadline:
        descriptor = find_running_hub_descriptor(creds)
        if descriptor is not None:
            client = SharedTelegramHubClient(descriptor)
            _start_client_heartbeat(client._descriptor.base_url)
            return client
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
    descriptor = _read_descriptor(paths.descriptor_path)
    if descriptor is not None:
        expected_scope = creds.scope_key if creds else descriptor.scope_key
        if descriptor.version != _HUB_VERSION or descriptor.scope_key != expected_scope:
            descriptor = None
    probe = _probe_descriptor_health(descriptor) if descriptor is not None else None
    ready = bool(probe and probe.get("ready"))
    return {
        "mode": get_shared_telegram_mode(),
        "enabled": is_shared_telegram_enabled(),
        "runtime_root": paths.root_dir,
        "runtime_scope": paths.scope_dir,
        "configured": creds is not None,
        "descriptor_present": descriptor is not None,
        "hub_reachable": bool(probe and probe.get("reachable")),
        "hub_healthy": ready,
        "hub_ready": ready,
        "health": probe.get("payload") if probe else None,
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
        # Track readiness explicitly so startup waits for Telegram polling to
        # become usable before the descriptor/health surface turns green.
        self._http_ready = threading.Event()
        self._poller_ready = threading.Event()
        self._update_offset: Optional[int] = None
        self._commands_registered = False
        self._poller_bootstrap_error: Optional[str] = None
        self._last_poll_error: Optional[str] = None
        self._last_poll_success_at: Optional[str] = None
        self._mailboxes: dict[int, _PromptMailbox] = {}
        self._mailboxes_lock = threading.Lock()

        # ── Client lifecycle tracking ──────────────────────────────────────
        # Tracks connected MCP clients via heartbeats + PID monitoring.
        # Hub self-terminates when no active clients remain after a grace period.
        self._registered_clients: dict[int, float] = {}  # pid -> last_heartbeat_time
        self._registered_clients_lock = threading.Lock()
        self._client_monitor_thread: Optional[threading.Thread] = None
        self._idle_since: Optional[float] = None
        self._startup_time: float = time.time()

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

    # ── Client lifecycle methods ───────────────────────────────────────────

    @staticmethod
    def _is_pid_alive(pid: int) -> bool:
        """Check if a process with given PID is still running."""
        return _is_pid_alive_standalone(pid)

    def _register_heartbeat(self, pid: int) -> None:
        """Register or refresh a client heartbeat."""
        with self._registered_clients_lock:
            is_new = pid not in self._registered_clients
            self._registered_clients[pid] = time.time()
            self._idle_since = None
        if is_new:
            self._log(f"Client registered: PID {pid} (total: {len(self._registered_clients)})")

    def _deregister_client(self, pid: int) -> None:
        """Remove a client from the registry."""
        with self._registered_clients_lock:
            removed = self._registered_clients.pop(pid, None)
        if removed is not None:
            self._log(f"Client deregistered: PID {pid} (remaining: {len(self._registered_clients)})")

    def _client_monitor_loop(self) -> None:
        """Background thread: prune dead clients and trigger idle shutdown."""
        while not self._stop_event.is_set():
            self._stop_event.wait(timeout=_CLIENT_MONITOR_INTERVAL)
            if self._stop_event.is_set():
                break
            now = time.time()
            removed = []
            with self._registered_clients_lock:
                for pid, last_hb in list(self._registered_clients.items()):
                    if not self._is_pid_alive(pid):
                        removed.append((pid, "dead"))
                        del self._registered_clients[pid]
                    elif now - last_hb > _HEARTBEAT_STALE_SECONDS:
                        removed.append((pid, "stale"))
                        del self._registered_clients[pid]
                active_count = len(self._registered_clients)
            for pid, reason in removed:
                self._log(f"Client removed ({reason}): PID {pid}")
            # Idle shutdown logic — only activate after startup grace period
            if active_count == 0 and (now - self._startup_time) >= _STARTUP_GRACE_SECONDS:
                if self._idle_since is None:
                    self._idle_since = now
                    self._log(f"No active clients. Idle shutdown in {_IDLE_SHUTDOWN_SECONDS}s...")
                elif now - self._idle_since >= _IDLE_SHUTDOWN_SECONDS:
                    self._log("Idle shutdown triggered \u2014 no active clients.")
                    self._stop_event.set()
            else:
                if self._idle_since is not None:
                    self._idle_since = None
                    self._log("Idle shutdown cancelled \u2014 client activity detected.")

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

    def _record_poll_failure(self, message: str, *, bootstrap: bool = False) -> None:
        """Capture polling failures without falsely advertising readiness."""
        self._last_poll_error = message
        if bootstrap:
            self._poller_bootstrap_error = message
            self._poller_ready.clear()
        self._log(message)

    def _mark_poll_success(self) -> None:
        """Remember the last successful Telegram polling round-trip."""
        self._last_poll_error = None
        self._last_poll_success_at = _utc_now_iso()

    def _resolve_409_conflict(self) -> None:
        """Attempt to kill the process that is competing for getUpdates."""
        killed_any = False
        # Check the hub descriptor for the old hub's PID
        descriptor = _read_descriptor(self.runtime_paths.descriptor_path)
        if descriptor is not None and descriptor.pid != self.pid:
            if _is_pid_alive_standalone(descriptor.pid):
                self._log(f"Found competing hub in descriptor: PID {descriptor.pid}")
                if _kill_process(descriptor.pid, self._log):
                    killed_any = True
        # Check the poll lock file for another PID
        poll_lock = self.runtime_paths.poll_lock_path
        if os.path.isfile(poll_lock):
            try:
                with open(poll_lock, "r", encoding="utf-8") as f:
                    lock_pid = int(f.read().strip())
                if lock_pid != self.pid and _is_pid_alive_standalone(lock_pid):
                    self._log(f"Found competing process in poll lock: PID {lock_pid}")
                    if _kill_process(lock_pid, self._log):
                        killed_any = True
            except (ValueError, OSError):
                pass
        if not killed_any:
            self._log("No competing process found via descriptor or poll lock \u2014 409 may be from an external source")

    def _bootstrap_poller(self) -> bool:
        """Claim Telegram polling ownership before the hub is considered ready.

        Phase 1: deleteWebhook to ensure clean polling mode.
        Phase 2: getUpdates with 409-specific conflict resolution.
        Phase 3: Normal success flow \u2014 set offset and mark ready.
        """
        self._set_my_commands()
        # Phase 1: Ensure webhook is cleared so getUpdates can work
        try:
            wh_resp = requests.post(self._api("deleteWebhook"), timeout=5)
            if wh_resp.status_code == 200:
                self._log("deleteWebhook succeeded (clean polling mode)")
            else:
                self._log(f"deleteWebhook returned HTTP {wh_resp.status_code} (non-fatal)")
        except Exception as exc:
            self._log(f"deleteWebhook failed (non-fatal): {exc}")
        # Phase 2: Attempt getUpdates with 409 conflict handling
        try:
            response = requests.get(self._api("getUpdates"), params={"timeout": 0}, timeout=5)
            if response.status_code == 409:
                self._log("Bootstrap getUpdates got 409 Conflict \u2014 resolving competing process")
                self._resolve_409_conflict()
                time.sleep(2)
                # Retry after killing competitor
                response = requests.get(self._api("getUpdates"), params={"timeout": 0}, timeout=5)
                if response.status_code == 409:
                    self._record_poll_failure(
                        "bootstrap getUpdates still 409 after conflict resolution",
                        bootstrap=True,
                    )
                    return False
            if response.status_code != 200:
                self._record_poll_failure(
                    f"bootstrap getUpdates returned HTTP {response.status_code}",
                    bootstrap=True,
                )
                return False
            # Phase 3: Success \u2014 consume buffered updates and mark ready
            updates = response.json().get("result", [])
            if updates:
                self._update_offset = updates[-1]["update_id"] + 1
            self._poller_bootstrap_error = None
            self._poller_ready.set()
            self._mark_poll_success()
            return True
        except Exception as exc:
            self._record_poll_failure(f"bootstrap getUpdates failed: {exc}", bootstrap=True)
            return False

    def readiness_snapshot(self) -> dict[str, Any]:
        """Return the hub's current readiness state for health reporting."""
        poller_expected = self.start_poller
        poller_thread_alive = bool(self._poller_thread and self._poller_thread.is_alive())
        poller_ready = self._poller_ready.is_set() if poller_expected else True
        ready = self._http_ready.is_set() and poller_ready and (
            poller_thread_alive if poller_expected else True
        )
        return {
            "ready": ready,
            "http_ready": self._http_ready.is_set(),
            "poller_expected": poller_expected,
            "poller_ready": poller_ready,
            "poller_thread_alive": poller_thread_alive,
            "commands_registered": self._commands_registered,
            "poller_bootstrap_error": self._poller_bootstrap_error,
            "last_poll_error": self._last_poll_error,
            "last_poll_success_at": self._last_poll_success_at,
            "active_clients": len(self._registered_clients),
            "client_pids": list(self._registered_clients.keys()),
        }

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
            if response.status_code == 200:
                return True
            # Log non-200 responses for diagnostics (mirrors _telegram_bridge.py pattern)
            self._log(f"react_to_message failed for msg_id={message_id}: "
                       f"HTTP {response.status_code} - {response.text[:300]}")
            return False
        except Exception as exc:
            self._log(f"react_to_message failed: {exc}")
            return False

    def _delete_message(self, message_id: int) -> bool:
        """Delete a message from the chat (best-effort, used to suppress
        pin-service notifications)."""
        try:
            response = requests.post(
                self._api("deleteMessage"),
                json={"chat_id": self.credentials.chat_id, "message_id": message_id},
                timeout=5,
            )
            if response.status_code == 200:
                return True
            self._log(f"delete_message failed for msg_id={message_id}: "
                       f"HTTP {response.status_code} - {response.text[:300]}")
            return False
        except Exception as exc:
            self._log(f"delete_message exception for msg_id={message_id}: {exc}")
            return False

    def _set_my_commands(self) -> None:
        """Register the Telegram command menu from the sole polling owner."""
        if self._commands_registered:
            return
        try:
            response = requests.post(
                self._api("setMyCommands"),
                json={"commands": get_telegram_admin_commands()},
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

        # Suppress pin-service notification messages (cosmetic cleanup)
        if message.get("pinned_message"):
            service_msg_id = message.get("message_id")
            if isinstance(service_msg_id, int):
                self._delete_message(service_msg_id)
            return

        text = (message.get("text") or "").strip()
        if text.startswith("/bypass"):
            self._handle_bypass_command(text.lower())
            return
        notification_reply = handle_remote_input_notification_telegram_command(text)
        if notification_reply is not None:
            self._send_text(notification_reply)
            return
        auto_message_reply = handle_vscode_auto_message_telegram_command(text)
        if auto_message_reply is not None:
            self._send_text(auto_message_reply)
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
        while not self._stop_event.is_set():
            if not self._poller_ready.is_set():
                if not self._bootstrap_poller():
                    self._stop_event.wait(timeout=1.0)
                    continue

            try:
                params: dict[str, Any] = {
                    "timeout": 3,
                    "allowed_updates": ["message"],
                }
                if self._update_offset is not None:
                    params["offset"] = self._update_offset
                response = requests.get(self._api("getUpdates"), params=params, timeout=10)
                if response.status_code == 409:
                    self._record_poll_failure(
                        "poll_loop getUpdates got 409 Conflict \u2014 will re-bootstrap"
                    )
                    self._poller_ready.clear()
                    self._stop_event.wait(timeout=1.0)
                    continue
                if response.status_code != 200:
                    self._record_poll_failure(
                        f"poll_loop getUpdates returned HTTP {response.status_code}"
                    )
                    self._stop_event.wait(timeout=1.0)
                    continue
                payload = response.json()
                self._mark_poll_success()
                for update in payload.get("result", []):
                    self._update_offset = update["update_id"] + 1
                    self._process_update(update)
                self._cleanup_mailboxes()
            except Exception as exc:
                self._record_poll_failure(f"poll_loop error: {exc}")
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
                readiness = service.readiness_snapshot()
                self._write_json(
                    200,
                    {
                        "ok": readiness["ready"],
                        **readiness,
                        "pid": descriptor.pid,
                        "scope_key": descriptor.scope_key,
                        "port": descriptor.port,
                        "runtime_scope_dir": descriptor.runtime_scope_dir,
                    },
                )

            def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
                try:
                    payload = self._read_json()
                    if self.path == "/heartbeat":
                        # Client lifecycle: register or refresh a heartbeat
                        pid = int(payload["pid"])
                        service._register_heartbeat(pid)
                        self._write_json(200, {"ok": True})
                        return
                    if self.path == "/deregister":
                        # Client lifecycle: remove a client from the registry
                        pid = int(payload["pid"])
                        service._deregister_client(pid)
                        self._write_json(200, {"ok": True})
                        return
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
        self._http_ready.set()
        if not self.start_poller:
            # Local HTTP-only mode is ready as soon as the endpoint is serving.
            self._poller_ready.set()
        self._write_descriptor()

        if self.start_poller:
            # Acquire exclusive polling lock before starting the poller thread
            acquired = _acquire_poll_lock(self.runtime_paths.poll_lock_path, self._log)
            if not acquired:
                self._log("FATAL: Could not acquire polling lock \u2014 another hub owns this bot token")
                self._stop_event.set()
                return
            self._poller_thread = threading.Thread(
                target=self._poll_loop,
                daemon=True,
                name="shared-telegram-hub-poller",
            )
            self._poller_thread.start()

        # Launch the client lifecycle monitor that prunes dead/stale clients
        # and triggers idle shutdown when no MCP clients remain connected.
        self._client_monitor_thread = threading.Thread(
            target=self._client_monitor_loop, daemon=True,
            name="hub-client-monitor",
        )
        self._client_monitor_thread.start()

    def stop(self) -> None:
        """Stop the HTTP server, polling thread, and descriptor publication.

        Shutdown order: remove descriptor first (prevent new clients from
        discovering us), stop event, join poller, shutdown HTTP, join threads,
        release poll lock.
        """
        # 1. Remove descriptor FIRST so no new clients discover this hub
        descriptor = _read_descriptor(self.runtime_paths.descriptor_path)
        if descriptor is not None and descriptor.pid == self.pid:
            _remove_file_safely(self.runtime_paths.descriptor_path)
        # 2. Signal all threads to stop
        self._stop_event.set()
        # 3. Join poller thread
        if self._poller_thread is not None and self._poller_thread.is_alive():
            self._poller_thread.join(timeout=5)
        # 4. Shutdown HTTP server
        if self._http_server is not None:
            self._http_server.shutdown()
            self._http_server.server_close()
        # 5. Join HTTP thread
        if self._http_thread is not None and self._http_thread.is_alive():
            self._http_thread.join(timeout=5)
        # 6. Join client monitor thread
        if self._client_monitor_thread and self._client_monitor_thread.is_alive():
            self._client_monitor_thread.join(timeout=5)
        # 7. Release poll lock
        _release_poll_lock(self.runtime_paths.poll_lock_path)


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
        # Wait until the stop event fires (idle shutdown or external signal).
        # Replaces the old infinite sleep loop so the process can exit cleanly
        # when the client monitor triggers an idle shutdown.
        while not service._stop_event.is_set():
            service._stop_event.wait(timeout=1.0)
    except KeyboardInterrupt:
        return 0
    finally:
        service.stop()
        _release_poll_lock(service.runtime_paths.poll_lock_path)