#!/usr/bin/env python3
"""
Human-in-the-Loop MCP Server

This server provides tools for getting human input and choices through GUI dialogs.
It enables LLMs to pause and ask for human feedback, input, or decisions.
Now supports both Windows and macOS platforms.
"""

import asyncio
import json
import platform
import subprocess
import threading
import time
try:
    import tkinter as tk
    from tkinter import messagebox, simpledialog, ttk
    _TKINTER_AVAILABLE = True
except ImportError:
    _TKINTER_AVAILABLE = False
    tk = None           # type: ignore[assignment]
    messagebox = None   # type: ignore[assignment]
    simpledialog = None # type: ignore[assignment]
    ttk = None          # type: ignore[assignment]
    import sys as _sys_tk
    print(
        "[Startup] tkinter is not available \u2014 GUI dialog tools will be "
        "disabled. Only get_remote_input (Telegram) and health_check will "
        "function. Install python3-tk (Linux) or ensure tkinter is bundled "
        "with your Python installation to enable GUI tools.",
        file=_sys_tk.stderr,
    )
from typing import List, Dict, Any, Optional, Literal
import sys
import os
from pydantic import Field
from typing import Annotated
# Set required environment variable for FastMCP 2.8.1+
os.environ.setdefault('FASTMCP_LOG_LEVEL', 'INFO')
from fastmcp import FastMCP, Context

# Telegram bridge for remote input (optional — degrades gracefully if unconfigured)
try:
    from _telegram_bridge import TelegramBridge, is_telegram_configured
except ImportError:
    TelegramBridge = None  # type: ignore[misc, assignment]
    def is_telegram_configured() -> bool:  # type: ignore[misc]
        return False

# Mini App components (optional — degrade gracefully if cloudflared unavailable)
try:
    import secrets as _secrets
    from _miniapp_server import MiniAppHTTPServer
    from _tunnel_manager import CloudflareTunnel, TunnelNotAvailableError
    _MINIAPP_AVAILABLE = True
except Exception as _miniapp_exc:
    # Catch any import / syntax / runtime error so the MCP server
    # always starts cleanly even if the Mini App modules are broken.
    import sys as _sys
    print(
        f"[Startup] Mini App components unavailable — "
        f"{type(_miniapp_exc).__name__}: {_miniapp_exc}. "
        "The get_remote_input Mini App/tunnel feature will be disabled.",
        file=_sys.stderr,
    )
    _MINIAPP_AVAILABLE = False
    MiniAppHTTPServer = None  # type: ignore[misc, assignment]
    CloudflareTunnel = None   # type: ignore[misc, assignment]
    class TunnelNotAvailableError(RuntimeError): pass  # type: ignore[misc]

# Platform detection
CURRENT_PLATFORM = platform.system().lower()
IS_WINDOWS = CURRENT_PLATFORM == 'windows'
IS_MACOS = CURRENT_PLATFORM == 'darwin'
IS_LINUX = CURRENT_PLATFORM == 'linux'

# Initialize the MCP server
mcp = FastMCP("Human-in-the-Loop Server")

def _get_multiline_input_custom_prompts() -> list:
    """Return list of (active: bool, active_color: str, text: str) checkbox tuples.

    Reads from custom_prompts.csv next to this script (format: active,active_color,prompt).
    Falls back to --multiline_input_custom_prompts= CLI args (all active=True, active_color='0').
    The CSV is re-read on every dialog open, so edits take effect immediately.
    """
    import csv as _csv
    # Try CSV file first
    try:
        csv_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "custom_prompts.csv")
        if os.path.isfile(csv_path):
            prompts = []
            with open(csv_path, encoding="utf-8", newline="") as fh:
                reader = _csv.DictReader(fh)
                for row in reader:
                    text = (row.get("prompt") or "").strip()
                    if text:
                        active = str(row.get("active", "1")).strip() not in ("0", "false", "False", "no")
                        active_color = (row.get("active_color") or "0").strip()
                        prompts.append((active, active_color, text))
            if prompts:
                return prompts
    except Exception as exc:
        print(f"[CustomPrompts] Failed to read custom_prompts.csv: {exc} "
              "— falling back to CLI args.")

    # Fallback: CLI args (all pre-checked)
    prompts = []
    try:
        argv = sys.argv[1:]
        for i, a in enumerate(argv):
            if a.startswith("--multiline_input_custom_prompts=") or a.startswith("multiline_input_custom_prompts="):
                prompts.append((True, "0", a.split("=", 1)[1]))
            elif a in ("--multiline_input_custom_prompts", "multiline_input_custom_prompts"):
                if i + 1 < len(argv):
                    prompts.append((True, "0", argv[i + 1]))
    except Exception as exc:
        print(f"[CustomPrompts] Failed to parse CLI args for custom prompts: {exc}")
    return prompts


def _normalize_tk_color(value: str) -> Optional[str]:
    """Normalize custom color values for Tkinter.

    - Returns None for empty/disabled values (e.g. "0").
    - Converts hyphen/underscore names (e.g. "light-coral") to Tk-friendly
      space-separated names ("light coral").
    """
    color = str(value or "").strip()
    if not color or color == "0":
        return None
    return color.replace("-", " ").replace("_", " ")


def _get_tool_timeout() -> Optional[float]:
    """Return the tool dialog timeout in seconds read from CLI args.

    Expected CLI forms:
      --tool-timeout=<seconds>   (e.g. 99999999 for effectively no timeout)
      --tool_timeout=<seconds>
    If not present or the value is <= 0, returns None (no timeout).
    """
    try:
        argv = sys.argv[1:]
        for i, a in enumerate(argv):
            if a.startswith("--tool-timeout=") or a.startswith("--tool_timeout="):
                value = float(a.split("=", 1)[1])
                return value if value > 0 else None
            if a in ("--tool-timeout", "--tool_timeout"):
                if i + 1 < len(argv):
                    value = float(argv[i + 1])
                    return value if value > 0 else None
    except Exception as exc:
        print(f"[Config] Invalid --tool-timeout value: {exc} — using default 300 s.")

    return 300.0  # default: 5 minutes

# Global variable to ensure GUI is initialized properly
_gui_initialized = False
_gui_lock = threading.Lock()

# Persistent GUI thread, root, and request queue for concurrent dialog support
_persistent_root = None
_persistent_gui_thread = None
_dialog_request_queue = None  # thread-safe queue; worker threads post callables here

# Persistent config file (same directory as this script)
_CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dialog_config.json")

def _get_persisted_dialog_height() -> int:
    """Return the saved dialog height, or the platform default (first run)."""
    default = 530 if IS_WINDOWS else (510 if IS_MACOS else 480)
    try:
        if os.path.isfile(_CONFIG_FILE):
            data = json.loads(open(_CONFIG_FILE, encoding="utf-8").read())
            return int(data.get("dialog_height", default))
    except Exception as exc:
        print(f"[Config] Could not read dialog_config.json: {exc} "
              f"— using default height {default}.")
    return default

def _save_persisted_dialog_height(height: int):
    """Persist the dialog height to dialog_config.json."""
    try:
        data = {}
        if os.path.isfile(_CONFIG_FILE):
            try:
                data = json.loads(open(_CONFIG_FILE, encoding="utf-8").read())
            except Exception as exc:
                print(f"[Config] dialog_config.json is corrupt ({exc}); "
                      "existing content will be overwritten.")
                data = {}
        data["dialog_height"] = height
        with open(_CONFIG_FILE, "w", encoding="utf-8") as _f:
            json.dump(data, _f, indent=2)
    except Exception as exc:
        print(f"[Config] Failed to save dialog height to dialog_config.json: {exc}")


# ── Bypass Human Input Mode ──────────────────────────────────────────────────
# When active, all tool requests are auto-approved without human interaction.
# The lock file is the single source of truth: if it exists, bypass is ON.

import json as _json_bypass
from datetime import datetime, timezone, timedelta

_BYPASS_LOCK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bypass_active.lock")
_BYPASS_LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bypass_log.jsonl")

# Telegram command poller control
_command_poller_thread: Optional[threading.Thread] = None
_command_poller_stop: Optional[threading.Event] = None

_BYPASS_AUTO_APPROVAL_MESSAGE = (
    "[AUTO-APPROVED] The user has enabled the bypass-human-input flag.\n"
    "This interaction has been automatically approved — no human review was performed.\n"
    "Action: Proceed with the best judgment. Continue requesting human approval "
    "on subsequent steps as the user may disable the bypass at any time.\n"
    "STATUS=APPROVED"
)


def _is_bypass_active() -> bool:
    """Check if bypass mode is active by checking the lock file.

    The lock file is the single source of truth. If it exists and hasn't
    expired, bypass is active. If it has expired, the lock file is deleted.
    """
    if not os.path.exists(_BYPASS_LOCK_FILE):
        return False

    try:
        with open(_BYPASS_LOCK_FILE, "r", encoding="utf-8") as f:
            data = _json_bypass.load(f)

        expires_at = data.get("expires_at")
        if expires_at:
            expiry = datetime.fromisoformat(expires_at)
            if datetime.now(timezone.utc) >= expiry:
                # Expired — clean up
                _deactivate_bypass("auto_expiry")
                return False

        return True
    except Exception:
        # If lock file is corrupted, treat as active (err on side of bypass)
        return True


def _activate_bypass(source: str = "unknown", duration_minutes: Optional[int] = None) -> dict:
    """Activate bypass mode by creating the lock file."""
    now = datetime.now(timezone.utc)
    data = {
        "activated_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=duration_minutes)).isoformat() if duration_minutes else None,
        "source": source,
    }

    try:
        with open(_BYPASS_LOCK_FILE, "w", encoding="utf-8") as f:
            _json_bypass.dump(data, f, indent=2)
    except Exception as e:
        print(f"[Bypass] Error creating lock file: {e}")
        return {"success": False, "error": str(e)}

    # Start Telegram command poller if available
    _start_command_poller()

    print(f"[Bypass] Activated by {source}" + (f" for {duration_minutes} minutes" if duration_minutes else " (no expiry)"))
    return {"success": True, **data}


def _deactivate_bypass(source: str = "unknown") -> dict:
    """Deactivate bypass mode by deleting the lock file."""
    try:
        if os.path.exists(_BYPASS_LOCK_FILE):
            os.remove(_BYPASS_LOCK_FILE)
    except Exception as e:
        print(f"[Bypass] Error removing lock file: {e}")
        return {"success": False, "error": str(e)}

    # NOTE: The Telegram command poller is intentionally NOT stopped here.
    # It must keep running so it can receive future /bypass on commands.

    print(f"[Bypass] Deactivated by {source}")
    return {"success": True, "deactivated_by": source}


def _log_bypass(tool_name: str, args_summary: dict) -> None:
    """Log a bypassed tool interaction to the JSONL audit log."""
    try:
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "tool": tool_name,
            "args_summary": args_summary,
        }
        with open(_BYPASS_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(_json_bypass.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[Bypass] Logging error: {e}")


def _check_bypass(tool_name: str, args_summary: dict) -> Optional[Dict[str, Any]]:
    """Check if bypass is active and return auto-approval response if so.

    Returns None if bypass is not active (tool should proceed normally).
    Returns a tool-specific auto-approval dict if bypass is active.
    """
    if not _is_bypass_active():
        return None

    # Log the bypassed interaction
    _log_bypass(tool_name, args_summary)

    # Build tool-specific response
    platform_name = "windows" if IS_WINDOWS else ("macos" if IS_MACOS else "linux")

    if tool_name in ("get_user_input", "get_multiline_input", "get_remote_input"):
        return {
            "success": True,
            "user_input": _BYPASS_AUTO_APPROVAL_MESSAGE,
            "character_count": len(_BYPASS_AUTO_APPROVAL_MESSAGE),
            "line_count": _BYPASS_AUTO_APPROVAL_MESSAGE.count("\n") + 1,
            "cancelled": False,
            "platform": platform_name,
            "bypassed": True,
        }
    elif tool_name == "get_user_choice":
        return {
            "success": True,
            "selected_choice": _BYPASS_AUTO_APPROVAL_MESSAGE,
            "cancelled": False,
            "platform": platform_name,
            "bypassed": True,
        }
    elif tool_name == "show_confirmation_dialog":
        return {
            "success": True,
            "confirmed": True,
            "response": "yes",
            "platform": platform_name,
            "bypassed": True,
        }
    elif tool_name == "show_info_message":
        return {
            "success": True,
            "acknowledged": True,
            "platform": platform_name,
            "bypassed": True,
        }

    return None


# ── Telegram Command Poller ──────────────────────────────────────────────────

def _start_command_poller():
    """Start the Telegram command poller thread if Telegram is configured."""
    global _command_poller_thread, _command_poller_stop

    if not is_telegram_configured() or TelegramBridge is None:
        return

    # Don't start if already running
    if _command_poller_thread is not None and _command_poller_thread.is_alive():
        return

    _command_poller_stop = threading.Event()
    _command_poller_thread = threading.Thread(
        target=_telegram_command_poller_loop,
        daemon=True,
        name="bypass-command-poller",
    )
    _command_poller_thread.start()
    print("[Bypass] Telegram command poller started")


def _stop_command_poller():
    """Stop the Telegram command poller thread."""
    global _command_poller_thread, _command_poller_stop

    if _command_poller_stop is not None:
        _command_poller_stop.set()

    if _command_poller_thread is not None and _command_poller_thread.is_alive():
        _command_poller_thread.join(timeout=5)

    _command_poller_thread = None
    _command_poller_stop = None
    print("[Bypass] Telegram command poller stopped")


def _telegram_command_poller_loop():
    """Background thread that listens for /bypass commands in Telegram."""
    try:
        import requests as _requests_poller

        tg = TelegramBridge()
        # Flush any pending updates so we only process new ones
        if hasattr(tg, "flush_updates"):
            tg.flush_updates()

        # Register bot command menu (shows "/" button in Telegram)
        tg.set_my_commands([
            {"command": "bypass", "description": "Show bypass mode status"},
            {"command": "bypass_on", "description": "Activate bypass (auto-approve all). Usage: /bypass_on [minutes]"},
            {"command": "bypass_off", "description": "Deactivate bypass (require human approval)"},
        ])

        _offset = None

        while not _command_poller_stop.is_set():
            try:
                # Long-poll for updates (3 second timeout on Telegram side)
                params = {"timeout": 3, "allowed_updates": ["message"]}
                if _offset is not None:
                    params["offset"] = _offset

                resp = _requests_poller.get(
                    f"https://api.telegram.org/bot{tg.bot_token}/getUpdates",
                    params=params,
                    timeout=10,
                )

                if resp.status_code != 200:
                    continue

                data = resp.json()
                if not data.get("ok"):
                    continue

                for update in data.get("result", []):
                    _offset = update["update_id"] + 1
                    msg = update.get("message", {})
                    text = (msg.get("text") or "").strip().lower()
                    chat_id = str(msg.get("chat", {}).get("id", ""))

                    # Only process messages from the configured chat
                    if chat_id != str(tg.chat_id):
                        continue

                    if text.startswith("/bypass"):
                        _handle_bypass_command(tg, text)

            except Exception as e:
                if not _command_poller_stop.is_set():
                    print(f"[Bypass] Command poller error: {e}")
                    _command_poller_stop.wait(timeout=2)
    except Exception as e:
        print(f"[Bypass] Command poller failed to start: {e}")


def _handle_bypass_command(tg, text: str):
    """Handle a /bypass command from Telegram."""
    # Normalize underscore variants from Telegram's command menu
    text = text.replace("/bypass_on", "/bypass on").replace("/bypass_off", "/bypass off")
    parts = text.split()

    if len(parts) >= 2 and parts[1] == "off":
        _deactivate_bypass("telegram")
        tg.send_text("✅ Bypass mode deactivated. Human approval is now required for all requests.")

    elif len(parts) >= 2 and parts[1] == "on":
        duration = None
        if len(parts) >= 3:
            try:
                duration = int(parts[2])
            except ValueError:
                tg.send_text(f"⚠️ Invalid duration: {parts[2]}. Use /bypass on [minutes]")
                return

        _activate_bypass("telegram", duration_minutes=duration)
        msg = "⚡ Bypass mode activated."
        if duration:
            msg += f" Auto-expires in {duration} minutes."
        else:
            msg += " No expiry set — send /bypass off to deactivate."
        tg.send_text(msg)

    elif len(parts) == 1:
        # Just "/bypass" — show status
        if _is_bypass_active():
            try:
                with open(_BYPASS_LOCK_FILE, "r", encoding="utf-8") as f:
                    data = _json_bypass.load(f)
                activated = data.get("activated_at", "unknown")
                expires = data.get("expires_at", "no expiry")
                source = data.get("source", "unknown")
                tg.send_text(f"ℹ️ Bypass is ACTIVE\nActivated: {activated}\nExpires: {expires}\nSource: {source}")
            except Exception:
                tg.send_text("ℹ️ Bypass is ACTIVE (details unavailable)")
        else:
            tg.send_text("ℹ️ Bypass is INACTIVE. Send /bypass on to activate.")

    else:
        tg.send_text("Usage: /bypass on [minutes] | /bypass off | /bypass (status)")


def get_system_font():
    """Get appropriate system font for the current platform"""
    if IS_MACOS:
        return ("SF Pro Display", 13)  # macOS system font
    elif IS_WINDOWS:
        return ("Segoe UI", 10)  # Windows system font
    else:
        return ("Ubuntu", 10)  # Linux/other systems

def get_title_font():
    """Get title font for dialogs"""
    if IS_MACOS:
        return ("SF Pro Display", 16, "bold")
    elif IS_WINDOWS:
        return ("Segoe UI", 14, "bold")
    else:
        return ("Ubuntu", 14, "bold")

def get_text_font():
    """Get text font for text widgets"""
    if IS_MACOS:
        return ("Monaco", 12)  # macOS monospace font
    elif IS_WINDOWS:
        return ("Consolas", 11)  # Windows monospace font
    else:
        return ("Ubuntu Mono", 10)  # Linux monospace font

def get_theme_colors():
    """Get modern theme colors based on platform"""
    if IS_WINDOWS:
        return {
            "bg_primary": "#FFFFFF",           # Pure white background
            "bg_secondary": "#F8F9FA",         # Light gray background
            "bg_accent": "#F1F3F4",            # Accent background
            "fg_primary": "#202124",           # Dark text
            "fg_secondary": "#5F6368",         # Secondary text
            "accent_color": "#0078D4",         # Windows blue
            "accent_hover": "#106EBE",         # Darker blue for hover
            "border_color": "#E8EAED",         # Light border
            "success_color": "#137333",        # Green for success
            "error_color": "#D93025",          # Red for errors
            "selection_bg": "#E3F2FD",         # Light blue selection
            "selection_fg": "#1565C0"          # Dark blue selection text
        }
    elif IS_MACOS:
        return {
            "bg_primary": "#FFFFFF",
            "bg_secondary": "#F5F5F7",
            "bg_accent": "#F2F2F7",
            "fg_primary": "#1D1D1F",
            "fg_secondary": "#86868B",
            "accent_color": "#007AFF",
            "accent_hover": "#0056CC",
            "border_color": "#D2D2D7",
            "success_color": "#30D158",
            "error_color": "#FF3B30",
            "selection_bg": "#E3F2FD",
            "selection_fg": "#1565C0"
        }
    else:  # Linux
        return {
            "bg_primary": "#FFFFFF",
            "bg_secondary": "#F8F9FA",
            "bg_accent": "#F1F3F4",
            "fg_primary": "#202124",
            "fg_secondary": "#5F6368",
            "accent_color": "#1976D2",
            "accent_hover": "#1565C0",
            "border_color": "#E8EAED",
            "success_color": "#388E3C",
            "error_color": "#D32F2F",
            "selection_bg": "#E3F2FD",
            "selection_fg": "#1565C0"
        }

def apply_modern_style(widget, widget_type="default", theme_colors=None):
    """Apply modern styling to tkinter widgets"""
    if theme_colors is None:
        theme_colors = get_theme_colors()
    
    try:
        if widget_type == "frame":
            widget.configure(
                bg=theme_colors["bg_primary"],
                relief="flat",
                borderwidth=0
            )
        elif widget_type == "label":
            widget.configure(
                bg=theme_colors["bg_primary"],
                fg=theme_colors["fg_primary"],
                font=get_system_font(),
                anchor="w"
            )
        elif widget_type == "title_label":
            widget.configure(
                bg=theme_colors["bg_primary"],
                fg=theme_colors["fg_primary"],
                font=get_title_font(),
                anchor="w"
            )
        elif widget_type == "listbox":
            widget.configure(
                bg=theme_colors["bg_primary"],
                fg=theme_colors["fg_primary"],
                selectbackground=theme_colors["selection_bg"],
                selectforeground=theme_colors["selection_fg"],
                relief="solid",
                borderwidth=1,
                highlightthickness=1,
                highlightcolor=theme_colors["accent_color"],
                highlightbackground=theme_colors["border_color"],
                font=get_system_font(),
                activestyle="none"
            )
        elif widget_type == "text":
            widget.configure(
                bg=theme_colors["bg_primary"],
                fg=theme_colors["fg_primary"],
                selectbackground=theme_colors["selection_bg"],
                selectforeground=theme_colors["selection_fg"],
                relief="solid",
                borderwidth=1,
                highlightthickness=1,
                highlightcolor=theme_colors["accent_color"],
                highlightbackground=theme_colors["border_color"],
                font=get_text_font(),
                wrap="word",
                padx=12,
                pady=8
            )
        elif widget_type == "scrollbar":
            widget.configure(
                bg=theme_colors["bg_secondary"],
                troughcolor=theme_colors["bg_accent"],
                activebackground=theme_colors["accent_hover"],
                relief="flat",
                borderwidth=0,
                highlightthickness=0
            )
    except Exception:
        pass  # Ignore styling errors on different platforms

def create_modern_button(parent, text, command, button_type="primary", theme_colors=None):
    """Create a modern styled button"""
    if theme_colors is None:
        theme_colors = get_theme_colors()
    
    if button_type == "primary":
        bg_color = theme_colors["accent_color"]
        fg_color = "#FFFFFF"
        hover_color = theme_colors["accent_hover"]
    else:  # secondary
        bg_color = theme_colors["bg_secondary"]
        fg_color = theme_colors["fg_primary"]
        hover_color = theme_colors["bg_accent"]
    
    button = tk.Button(
        parent,
        text=text,
        command=command,
        bg=bg_color,
        fg=fg_color,
        font=get_system_font(),
        relief="flat",
        borderwidth=0,
        padx=20,
        pady=8,
        cursor="hand2" if IS_WINDOWS else "pointinghand"
    )
    
    # Add hover effects
    def on_enter(e):
        button.configure(bg=hover_color)
    
    def on_leave(e):
        button.configure(bg=bg_color)
    
    button.bind("<Enter>", on_enter)
    button.bind("<Leave>", on_leave)
    
    return button

def configure_modern_window(window):
    """Apply modern window styling"""
    theme_colors = get_theme_colors()
    
    try:
        window.configure(bg=theme_colors["bg_primary"])
        
        if IS_WINDOWS:
            # Windows-specific modern styling
            try:
                # Try to remove window decorations for modern look (Windows 10/11)
                window.overrideredirect(False)  # Keep decorations for better UX
                window.attributes('-alpha', 0.98)  # Slight transparency
            except:
                pass
        
        # Apply platform-specific configurations
        configure_window_for_platform(window)
        
    except Exception:
        pass  # Fallback to basic styling

def configure_macos_app():
    """Configure macOS-specific application settings"""
    if IS_MACOS:
        try:
            # Try to bring Python to front on macOS
            subprocess.run([
                'osascript', '-e', 
                'tell application "System Events" to set frontmost of first process whose unix id is {} to true'.format(os.getpid())
            ], check=False, capture_output=True)
        except Exception:
            pass  # Ignore if osascript is not available

def ensure_gui_initialized():
    """Ensure GUI subsystem is properly initialized"""
    global _gui_initialized
    if not _TKINTER_AVAILABLE:
        return False
    with _gui_lock:
        if not _gui_initialized:
            try:
                test_root = tk.Tk()
                test_root.withdraw()
                
                # Platform-specific initialization
                if IS_MACOS:
                    # macOS-specific configuration
                    test_root.call('wm', 'attributes', '.', '-topmost', '1')
                    configure_macos_app()
                elif IS_WINDOWS:
                    # Windows-specific configuration (existing behavior)
                    test_root.attributes('-topmost', True)
                
                test_root.destroy()
                _gui_initialized = True
            except Exception as e:
                print(f"Warning: GUI initialization failed: {e}")
                _gui_initialized = False
        return _gui_initialized

def _ensure_persistent_root():
    """Ensure a single Tk root + polling loop run on a dedicated GUI thread.

    Worker threads post callables to _dialog_request_queue; the GUI thread's
    polling loop dequeues and executes them, making it safe to create/destroy
    Toplevel widgets from multiple concurrent worker threads.
    """
    global _persistent_root, _persistent_gui_thread, _dialog_request_queue
    if not _TKINTER_AVAILABLE:
        return None
    with _gui_lock:
        if _persistent_gui_thread is not None and _persistent_gui_thread.is_alive():
            return _persistent_root
        import queue as _queue_module
        _dialog_request_queue = _queue_module.Queue()
        ready = threading.Event()

        def _poll():
            """Drain the request queue and re-schedule (runs on GUI thread)."""
            try:
                while True:
                    fn = _dialog_request_queue.get_nowait()
                    try:
                        fn()
                    except Exception as exc:
                        print(f"[GUIThread] Unhandled exception executing queued dialog "
                              f"callable {fn!r}: {type(exc).__name__}: {exc}")
            except Exception:
                pass  # queue.Empty — nothing to do
            if _persistent_root and _persistent_root.winfo_exists():
                _persistent_root.after(30, _poll)

        def _gui_loop():
            global _persistent_root
            _persistent_root = tk.Tk()
            _persistent_root.withdraw()  # hidden root; all dialogs are Toplevels
            ready.set()
            _persistent_root.after(30, _poll)  # start the polling loop
            _persistent_root.mainloop()

        _persistent_gui_thread = threading.Thread(
            target=_gui_loop, daemon=True, name="tkinter-gui-thread"
        )
        _persistent_gui_thread.start()
        ready.wait(timeout=5)
    return _persistent_root

def configure_window_for_platform(window):
    """Apply platform-specific window configurations"""
    try:
        if IS_MACOS:
            # macOS-specific window configuration
            window.call('wm', 'attributes', '.', '-topmost', '1')
            window.lift()
            window.focus_force()
            # Try to activate the app on macOS
            configure_macos_app()
        elif IS_WINDOWS:
            # Windows-specific configuration (existing behavior)
            window.attributes('-topmost', True)
            window.lift()
            window.focus_force()
    except Exception as e:
        print(f"Warning: Platform-specific window configuration failed: {e}")

def create_input_dialog(title: str, prompt: str, default_value: str = "", input_type: str = "text"):
    """Create a modern input dialog window"""
    try:
        root = tk.Tk()
        root.withdraw()
        dialog = ModernInputDialog(root, title, prompt, default_value, input_type)
        result = dialog.result
        root.destroy()
        return result
    except Exception as e:
        print(f"[Dialog] create_input_dialog(title={title!r}) failed: "
              f"{type(e).__name__}: {e}")
        return None

def show_confirmation(title: str, message: str):
    """Show modern confirmation dialog"""
    try:
        root = tk.Tk()
        root.withdraw()
        dialog = ModernConfirmationDialog(root, title, message)
        result = dialog.result
        root.destroy()
        return result
    except Exception as e:
        print(f"Error in confirmation dialog: {e}")
        return False

def show_info(title: str, message: str):
    """Show modern info dialog"""
    try:
        root = tk.Tk()
        root.withdraw()
        dialog = ModernInfoDialog(root, title, message)
        result = dialog.result
        root.destroy()
        return result
    except Exception as e:
        print(f"Error in info dialog: {e}")
        return False

class ModernInputDialog:
    def __init__(self, parent, title, prompt, default_value="", input_type="text"):
        self.result = None
        self.input_type = input_type
        
        # Get theme colors
        self.theme_colors = get_theme_colors()
        
        # Create the dialog window
        self.dialog = tk.Toplevel(parent)
        self.dialog.title(title)
        self.dialog.grab_set()
        self.dialog.resizable(False, False)
        
        # Apply modern window styling
        configure_modern_window(self.dialog)
        
        # Set size based on platform (increased height by 40px)
        if IS_WINDOWS:
            self.dialog.geometry("420x320")
        else:
            self.dialog.geometry("400x300")
        
        self.center_window()
        
        # Create the main frame
        main_frame = tk.Frame(self.dialog, bg=self.theme_colors["bg_primary"])
        main_frame.pack(fill="both", expand=True, padx=24, pady=20)
        
        # Title label
        title_label = tk.Label(
            main_frame,
            text=title,
            bg=self.theme_colors["bg_primary"],
            fg=self.theme_colors["fg_primary"],
            font=get_title_font(),
            anchor="w"
        )
        title_label.pack(fill="x", pady=(0, 8))
        
        # Prompt area (fixed height + scrollable for long prompts) with visible scrollbar and border
        prompt_container = tk.Frame(main_frame, bg=self.theme_colors["bg_primary"],
                                    highlightbackground=self.theme_colors["border_color"],
                                    highlightthickness=1, bd=0)
        prompt_container.pack(fill="x", pady=(0, 20))

        # Inner frame using grid so text and scrollbar align exactly and scrollbar is always visible
        prompt_inner = tk.Frame(prompt_container, bg=self.theme_colors["bg_primary"])
        prompt_inner.pack(fill="both", expand=True)
        prompt_inner.columnconfigure(0, weight=1)
        prompt_inner.rowconfigure(0, weight=1)

        # Text widget for prompt (disabled so user can't edit)
        prompt_text = tk.Text(
            prompt_inner,
            height=6,  # reserve vertical space so input stays visible
            wrap="word",
            bg=self.theme_colors["bg_primary"],
            fg=self.theme_colors["fg_secondary"],
            font=get_system_font(),
            relief="flat",
            borderwidth=0,
            padx=8,
            pady=6,
            highlightthickness=0
        )
        prompt_text.insert("1.0", prompt)
        prompt_text.configure(state="disabled")
        prompt_text.grid(row=0, column=0, sticky="nsew")

        # Vertical scrollbar (styled and visible to indicate scrollability)
        prompt_scroll = tk.Scrollbar(prompt_inner, orient="vertical", command=prompt_text.yview, width=14)
        # Make scrollbar more visible with accent colors and thicker track
        prompt_scroll.configure(
            troughcolor=self.theme_colors["bg_accent"],
            bg=self.theme_colors["accent_color"],
            activebackground=self.theme_colors["accent_hover"],
            relief="flat",
            bd=0,
            highlightthickness=0
        )
        prompt_scroll.grid(row=0, column=1, sticky="ns", padx=(6, 0))
        prompt_text.configure(yscrollcommand=prompt_scroll.set)

        # Small arrow indicator to the right of the scrollbar so users notice it
        arrow_label = tk.Label(prompt_inner,
                               text="▼",
                               bg=self.theme_colors["bg_primary"],
                               fg=self.theme_colors["fg_secondary"],
                               font=(get_system_font()[0], max(get_system_font()[1]-1, 9), "bold"))
        arrow_label.grid(row=0, column=2, sticky="n", padx=(6,4), pady=(2,0))

        # Input field
        input_frame = tk.Frame(main_frame, bg=self.theme_colors["bg_primary"])
        input_frame.pack(fill="x", pady=(0, 24))
        
        self.entry = tk.Entry(
            input_frame,
            font=get_system_font(),
            bg=self.theme_colors["bg_primary"],
            fg=self.theme_colors["fg_primary"],
            relief="solid",
            borderwidth=1,
            highlightthickness=1,
            highlightcolor=self.theme_colors["accent_color"],
            highlightbackground=self.theme_colors["border_color"],
            insertbackground=self.theme_colors["accent_color"]
        )
        self.entry.pack(fill="x", ipady=8, ipadx=12)
        
        if default_value:
            self.entry.insert(0, default_value)
            self.entry.select_range(0, tk.END)
        
        # Button frame
        button_frame = tk.Frame(main_frame, bg=self.theme_colors["bg_primary"])
        button_frame.pack(fill="x")
        
        # Create modern buttons
        self.ok_button = create_modern_button(
            button_frame, "OK", self.ok_clicked, "primary", self.theme_colors
        )
        self.ok_button.pack(side=tk.RIGHT, padx=(8, 0))
        
        self.cancel_button = create_modern_button(
            button_frame, "Cancel", self.cancel_clicked, "secondary", self.theme_colors
        )
        self.cancel_button.pack(side=tk.RIGHT)
        
        # Handle window close and keyboard shortcuts
        self.dialog.protocol("WM_DELETE_WINDOW", self.cancel_clicked)
        self.dialog.bind('<Return>', lambda e: self.ok_clicked())
        self.dialog.bind('<Escape>', lambda e: self.cancel_clicked())
        
        # Focus on entry
        self.entry.focus_set()
        
        # Wait for dialog completion
        self.dialog.wait_window()
    
    def center_window(self):
        """Center the dialog window on screen"""
        self.dialog.update_idletasks()
        width = self.dialog.winfo_width()
        height = self.dialog.winfo_height()
        screen_width = self.dialog.winfo_screenwidth()
        screen_height = self.dialog.winfo_screenheight()
        x = (screen_width // 2) - (width // 2)
        y = (screen_height // 2) - (height // 2)
        
        if IS_MACOS:
            y = max(50, y - 50)
        elif IS_WINDOWS:
            y = max(30, y - 30)
            
        self.dialog.geometry(f"{width}x{height}+{x}+{y}")
    
    def ok_clicked(self):
        value = self.entry.get()
        if self.input_type == "integer":
            try:
                self.result = int(value) if value else None
            except ValueError:
                self.result = None
        elif self.input_type == "float":
            try:
                self.result = float(value) if value else None
            except ValueError:
                self.result = None
        else:
            self.result = value if value else None
        self.dialog.destroy()
    
    def cancel_clicked(self):
        self.result = None
        self.dialog.destroy()

class ModernConfirmationDialog:
    def __init__(self, parent, title, message):
        self.result = False
        
        # Get theme colors
        self.theme_colors = get_theme_colors()
        
        # Create the dialog window
        self.dialog = tk.Toplevel(parent)
        self.dialog.title(title)
        self.dialog.grab_set()
        self.dialog.resizable(False, False)
        
        # Apply modern window styling
        configure_modern_window(self.dialog)
        
        # Set size based on content
        if IS_WINDOWS:
            self.dialog.geometry("440x220")
        else:
            self.dialog.geometry("420x200")
        
        self.center_window()
        
        # Create the main frame
        main_frame = tk.Frame(self.dialog, bg=self.theme_colors["bg_primary"])
        main_frame.pack(fill="both", expand=True, padx=24, pady=20)
        
        # Title label
        title_label = tk.Label(
            main_frame,
            text=title,
            bg=self.theme_colors["bg_primary"],
            fg=self.theme_colors["fg_primary"],
            font=get_title_font(),
            anchor="w"
        )
        title_label.pack(fill="x", pady=(0, 12))
        
        # Message label
        message_label = tk.Label(
            main_frame,
            text=message,
            bg=self.theme_colors["bg_primary"],
            fg=self.theme_colors["fg_secondary"],
            font=get_system_font(),
            wraplength=370,
            justify="left",
            anchor="w"
        )
        message_label.pack(fill="x", pady=(0, 24))
        
        # Button frame
        button_frame = tk.Frame(main_frame, bg=self.theme_colors["bg_primary"])
        button_frame.pack(fill="x")
        
        # Create modern buttons
        self.yes_button = create_modern_button(
            button_frame, "Yes", self.yes_clicked, "primary", self.theme_colors
        )
        self.yes_button.pack(side=tk.RIGHT, padx=(8, 0))
        
        self.no_button = create_modern_button(
            button_frame, "No", self.no_clicked, "secondary", self.theme_colors
        )
        self.no_button.pack(side=tk.RIGHT)
        
        # Handle window close and keyboard shortcuts
        self.dialog.protocol("WM_DELETE_WINDOW", self.no_clicked)
        self.dialog.bind('<Return>', lambda e: self.yes_clicked())
        self.dialog.bind('<Escape>', lambda e: self.no_clicked())
        
        # Focus on No button by default (safer)
        self.no_button.focus_set()
        
        # Wait for dialog completion
        self.dialog.wait_window()
    
    def center_window(self):
        """Center the dialog window on screen"""
        self.dialog.update_idletasks()
        width = self.dialog.winfo_width()
        height = self.dialog.winfo_height()
        screen_width = self.dialog.winfo_screenwidth()
        screen_height = self.dialog.winfo_screenheight()
        x = (screen_width // 2) - (width // 2)
        y = (screen_height // 2) - (height // 2)
        
        if IS_MACOS:
            y = max(50, y - 50)
        elif IS_WINDOWS:
            y = max(30, y - 30)
            
        self.dialog.geometry(f"{width}x{height}+{x}+{y}")
    
    def yes_clicked(self):
        self.result = True
        self.dialog.destroy()
    
    def no_clicked(self):
        self.result = False
        self.dialog.destroy()

class ModernInfoDialog:
    def __init__(self, parent, title, message):
        self.result = True
        
        # Get theme colors
        self.theme_colors = get_theme_colors()
        
        # Create the dialog window
        self.dialog = tk.Toplevel(parent)
        self.dialog.title(title)
        self.dialog.grab_set()
        self.dialog.resizable(False, False)
        
        # Apply modern window styling
        configure_modern_window(self.dialog)
        
        # Set size based on content
        if IS_WINDOWS:
            self.dialog.geometry("420x200")
        else:
            self.dialog.geometry("400x180")
        
        self.center_window()
        
        # Create the main frame
        main_frame = tk.Frame(self.dialog, bg=self.theme_colors["bg_primary"])
        main_frame.pack(fill="both", expand=True, padx=24, pady=20)
        
        # Title label
        title_label = tk.Label(
            main_frame,
            text=title,
            bg=self.theme_colors["bg_primary"],
            fg=self.theme_colors["fg_primary"],
            font=get_title_font(),
            anchor="w"
        )
        title_label.pack(fill="x", pady=(0, 12))
        
        # Message label
        message_label = tk.Label(
            main_frame,
            text=message,
            bg=self.theme_colors["bg_primary"],
            fg=self.theme_colors["fg_secondary"],
            font=get_system_font(),
            wraplength=350,
            justify="left",
            anchor="w"
        )
        message_label.pack(fill="x", pady=(0, 24))
        
        # Button frame
        button_frame = tk.Frame(main_frame, bg=self.theme_colors["bg_primary"])
        button_frame.pack(fill="x")
        
        # Create modern OK button
        self.ok_button = create_modern_button(
            button_frame, "OK", self.ok_clicked, "primary", self.theme_colors
        )
        self.ok_button.pack(side=tk.RIGHT)
        
        # Handle window close and keyboard shortcuts
        self.dialog.protocol("WM_DELETE_WINDOW", self.ok_clicked)
        self.dialog.bind('<Return>', lambda e: self.ok_clicked())
        self.dialog.bind('<Escape>', lambda e: self.ok_clicked())
        
        # Focus on OK button
        self.ok_button.focus_set()
        
        # Wait for dialog completion
        self.dialog.wait_window()
    
    def center_window(self):
        """Center the dialog window on screen"""
        self.dialog.update_idletasks()
        width = self.dialog.winfo_width()
        height = self.dialog.winfo_height()
        screen_width = self.dialog.winfo_screenwidth()
        screen_height = self.dialog.winfo_screenheight()
        x = (screen_width // 2) - (width // 2)
        y = (screen_height // 2) - (height // 2)
        
        if IS_MACOS:
            y = max(50, y - 50)
        elif IS_WINDOWS:
            y = max(30, y - 30)
            
        self.dialog.geometry(f"{width}x{height}+{x}+{y}")
    
    def ok_clicked(self):
        self.result = True
        self.dialog.destroy()

def create_choice_dialog(title: str, prompt: str, choices: List[str], allow_multiple: bool = False):
    """Create a choice dialog window"""
    try:
        root = tk.Tk()
        root.withdraw()
        dialog = ChoiceDialog(root, title, prompt, choices, allow_multiple)
        result = dialog.result
        root.destroy()
        return result
    except Exception as e:
        print(f"Error in choice dialog: {e}")
        return None

def create_multiline_input_dialog(title: str, prompt: str, default_value: str = ""):
    """Create a multi-line text input dialog.

    Uses a persistent Tk root on a dedicated GUI thread so multiple
    calls can be active simultaneously without blocking each other.
    """
    try:
        root = _ensure_persistent_root()
        if root is None:
            # No display — try Telegram-only mode if configured
            if not is_telegram_configured() or TelegramBridge is None:
                return None
            try:
                _tg = TelegramBridge()
                _msg_id = _tg.send_prompt(title, prompt)
                if not _msg_id:
                    return None
                import threading as _threading
                _cancel = _threading.Event()
                _reply = _tg.poll_for_reply(_msg_id, cancel_event=_cancel)
                if _reply is not None:
                    _tg.edit_message(_msg_id, f"\u2705 Response received.")
                return _reply
            except Exception as _exc:
                print(f"[RemoteInput] Telegram-only mode error: {_exc}")
                return None
        done = threading.Event()
        dialog_holder = [None]

        def _create_on_gui_thread():
            try:
                dialog_holder[0] = MultilineInputDialog(
                    root, title, prompt, default_value, done_event=done
                )
                # Ensure the new window is visible and on top of others
                try:
                    dialog_holder[0].dialog.lift()
                except Exception:
                    pass
            except Exception as e:
                print(f"[GUIThread] Failed to create MultilineInputDialog "
                      f"(title={title!r}): {type(e).__name__}: {e}")
                done.set()  # unblock caller even on error

        # Post to the GUI thread via the thread-safe queue.
        # The polling loop (_poll) picks this up within ~30ms.
        _dialog_request_queue.put(_create_on_gui_thread)
        timeout = _get_tool_timeout()
        done.wait(timeout=timeout)

        if dialog_holder[0] is not None:
            return dialog_holder[0].result
        return None
    except Exception as e:
        print(f"[Dialog] create_multiline_input_dialog(title={title!r}) "
              f"failed: {type(e).__name__}: {e}")
        return None

def show_confirmation(title: str, message: str):
    """Show confirmation dialog"""
    try:
        root = tk.Tk()
        root.withdraw()
        configure_window_for_platform(root)
        result = messagebox.askyesno(title, message, parent=root)
        root.destroy()
        return result
    except Exception as e:
        print(f"[Dialog] show_confirmation(title={title!r}) failed: "
              f"{type(e).__name__}: {e}")
        return False

def show_info(title: str, message: str):
    """Show info dialog"""
    try:
        root = tk.Tk()
        root.withdraw()
        configure_window_for_platform(root)
        messagebox.showinfo(title, message, parent=root)
        root.destroy()
        return True
    except Exception as e:
        print(f"[Dialog] show_info(title={title!r}) failed: "
              f"{type(e).__name__}: {e}")
        return False

class ChoiceDialog:
    def __init__(self, parent, title, prompt, choices, allow_multiple=False):
        self.result = None
        
        # Get theme colors
        self.theme_colors = get_theme_colors()
        
        # Create the dialog window
        self.dialog = tk.Toplevel(parent)
        self.dialog.title(title)
        self.dialog.grab_set()
        self.dialog.resizable(True, True)
        
        # Apply modern window styling
        configure_modern_window(self.dialog)
        
        # Set size based on platform
        if IS_MACOS:
            self.dialog.geometry("480x400")
        elif IS_WINDOWS:
            self.dialog.geometry("500x420")
        else:
            self.dialog.geometry("450x350")
        
        self.center_window()
        
        # Create the main frame with modern styling
        main_frame = tk.Frame(self.dialog, bg=self.theme_colors["bg_primary"])
        main_frame.pack(fill="both", expand=True, padx=24, pady=20)
        
        # Configure grid weights
        main_frame.columnconfigure(0, weight=1)
        main_frame.rowconfigure(1, weight=1)
        
        # Add modern title label
        title_label = tk.Label(
            main_frame, 
            text=title,
            bg=self.theme_colors["bg_primary"],
            fg=self.theme_colors["fg_primary"],
            font=get_title_font(),
            anchor="w"
        )
        title_label.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        
        # Prompt area (fixed height + scrollable) so long prompts don't push content away
        prompt_container = tk.Frame(main_frame, bg=self.theme_colors["bg_primary"])
        prompt_container.grid(row=1, column=0, sticky="ew", pady=(0,20))
        prompt_container.columnconfigure(0, weight=1)

        # Inner frame for alignment
        prompt_inner = tk.Frame(prompt_container, bg=self.theme_colors["bg_primary"])
        prompt_inner.pack(fill="both", expand=True)
        prompt_inner.columnconfigure(0, weight=1)

        prompt_text = tk.Text(
            prompt_inner,
            height=4,
            wrap="word",
            bg=self.theme_colors["bg_primary"],
            fg=self.theme_colors["fg_secondary"],
            font=get_system_font(),
            relief="flat",
            borderwidth=0,
            padx=6,
            pady=4,
            highlightthickness=0
        )
        prompt_text.insert("1.0", prompt)
        prompt_text.configure(state="disabled")
        prompt_text.grid(row=0, column=0, sticky="nsew")

        prompt_scroll = tk.Scrollbar(prompt_inner, orient="vertical", command=prompt_text.yview, width=12)
        apply_modern_style(prompt_scroll, "scrollbar", self.theme_colors)
        prompt_scroll.grid(row=0, column=1, sticky="ns", padx=(6,0))
        prompt_text.configure(yscrollcommand=prompt_scroll.set)

        # Small arrow indicator to draw attention to scrollbar
        arrow_label = tk.Label(prompt_inner,
                               text="▼",
                               bg=self.theme_colors["bg_primary"],
                               fg=self.theme_colors["fg_secondary"],
                               font=(get_system_font()[0], max(get_system_font()[1]-1, 9), "bold"))
        arrow_label.grid(row=0, column=2, sticky="n", padx=(6,4), pady=(2,0))

        # Create choice selection widget with modern container
        list_container = tk.Frame(main_frame, bg=self.theme_colors["bg_primary"])
        list_container.grid(row=2, column=0, sticky="nsew", pady=(0, 24))
        list_container.columnconfigure(0, weight=1)
        list_container.rowconfigure(0, weight=1)
        
        # Modern listbox with styling
        if allow_multiple:
            self.listbox = tk.Listbox(list_container, selectmode=tk.MULTIPLE, height=8)
        else:
            self.listbox = tk.Listbox(list_container, selectmode=tk.SINGLE, height=8)
        
        apply_modern_style(self.listbox, "listbox", self.theme_colors)
        
        for choice in choices:
            self.listbox.insert(tk.END, choice)
        self.listbox.grid(row=0, column=0, sticky="nsew", padx=(0, 2))
        
        # Modern scrollbar
        scrollbar = tk.Scrollbar(list_container, orient="vertical", command=self.listbox.yview)
        apply_modern_style(scrollbar, "scrollbar", self.theme_colors)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.listbox.configure(yscrollcommand=scrollbar.set)
        
        # Modern button frame
        button_frame = tk.Frame(main_frame, bg=self.theme_colors["bg_primary"])
        button_frame.grid(row=3, column=0, sticky="ew")
        
        # Create modern buttons
        self.ok_button = create_modern_button(
            button_frame, "OK", self.ok_clicked, "primary", self.theme_colors
        )
        self.ok_button.pack(side=tk.RIGHT, padx=(8, 0))
        
        self.cancel_button = create_modern_button(
            button_frame, "Cancel", self.cancel_clicked, "secondary", self.theme_colors
        )
        self.cancel_button.pack(side=tk.RIGHT)
        
        # Handle window close
        self.dialog.protocol("WM_DELETE_WINDOW", self.cancel_clicked)
        
        # Focus on listbox
        self.listbox.focus_set()
        if choices:
            self.listbox.selection_set(0)  # Select first item by default
        
        # Platform-specific final setup
        if IS_MACOS:
            self.dialog.after(100, lambda: self.listbox.focus_set())
        
        # Add keyboard shortcuts
        self.dialog.bind('<Return>', lambda e: self.ok_clicked())
        self.dialog.bind('<Escape>', lambda e: self.cancel_clicked())
        
        # No wait_window() here — the caller blocks on self._done_event instead,
        # allowing multiple dialogs to be open simultaneously.
    
    def center_window(self):
        """Center the dialog window on screen"""
        self.dialog.update_idletasks()
        width = self.dialog.winfo_width()
        height = self.dialog.winfo_height()
        
        # Get screen dimensions
        screen_width = self.dialog.winfo_screenwidth()
        screen_height = self.dialog.winfo_screenheight()
        
        # Calculate center position
        x = (screen_width // 2) - (width // 2)
        y = (screen_height // 2) - (height // 2)
        
        # Platform-specific adjustments
        if IS_MACOS:
            y = max(50, y - 50)
        elif IS_WINDOWS:
            y = max(30, y - 30)
        
        self.dialog.geometry(f"{width}x{height}+{x}+{y}")
    
    def ok_clicked(self):
        selection = self.listbox.curselection()
        if selection:
            selected_items = [self.listbox.get(i) for i in selection]
            self.result = selected_items if len(selected_items) > 1 else selected_items[0]
        self.dialog.destroy()
    
    def cancel_clicked(self):
        self.result = None
        self.dialog.destroy()

class MultilineInputDialog:
    def __init__(self, parent, title, prompt, default_value="", done_event=None):
        self.result = None
        self._done_event = done_event  # set by ok/cancel to unblock the caller
        
        # Get theme colors
        self.theme_colors = get_theme_colors()
        self._base_bg_primary = self.theme_colors["bg_primary"]
        self._base_bg_secondary = self.theme_colors["bg_secondary"]
        
        # Create the dialog window
        self.dialog = tk.Toplevel(parent)
        self.dialog.title(title)
        # grab_set() intentionally omitted -- it prevents minimizing on Windows.
        self.dialog.resizable(True, True)

        # Apply modern window styling
        configure_modern_window(self.dialog)

        # Width is platform-specific; height uses the persisted value
        _dlg_width = 580 if IS_MACOS else (600 if IS_WINDOWS else 550)
        _dlg_height = _get_persisted_dialog_height()
        self.dialog.geometry(f"{_dlg_width}x{_dlg_height}")
        self.center_window()
        
        # Create the main frame with modern styling
        main_frame = tk.Frame(self.dialog, bg=self.theme_colors["bg_primary"])
        main_frame.pack(fill="both", expand=True, padx=24, pady=20)
        self.main_frame = main_frame
        
        # Configure grid weights
        main_frame.columnconfigure(0, weight=1)
        main_frame.rowconfigure(2, weight=1)
        
        # Add modern title label
        title_label = tk.Label(
            main_frame,
            text=title,
            bg=self.theme_colors["bg_primary"],
            fg=self.theme_colors["fg_primary"],
            font=get_title_font(),
            anchor="w"
        )
        title_label.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        self.title_label = title_label
        
        # Prompt area (fixed height + scrollable) so very long prompts remain readable
        prompt_container = tk.Frame(main_frame, bg=self.theme_colors["bg_primary"])
        prompt_container.grid(row=1, column=0, sticky="ew", pady=(0,20))
        prompt_container.columnconfigure(0, weight=1)
        self.prompt_container = prompt_container

        prompt_inner = tk.Frame(prompt_container, bg=self.theme_colors["bg_primary"])
        prompt_inner.pack(fill="both", expand=True)
        prompt_inner.columnconfigure(0, weight=1)
        self.prompt_inner = prompt_inner

        prompt_text = tk.Text(
            prompt_inner,
            height=8,
            wrap="word",
            bg=self.theme_colors["bg_primary"],
            fg=self.theme_colors["fg_secondary"],
            font=get_system_font(),
            relief="flat",
            borderwidth=0,
            padx=8,
            pady=6,
            highlightthickness=0
        )
        prompt_text.insert("1.0", prompt)
        prompt_text.configure(state="disabled")
        prompt_text.grid(row=0, column=0, sticky="nsew")
        self.prompt_text = prompt_text

        prompt_scroll = tk.Scrollbar(prompt_inner, orient="vertical", command=prompt_text.yview, width=14)
        apply_modern_style(prompt_scroll, "scrollbar", self.theme_colors)
        prompt_scroll.grid(row=0, column=1, sticky="ns", padx=(6,0))
        prompt_text.configure(yscrollcommand=prompt_scroll.set)

        arrow_label = tk.Label(prompt_inner,
                               text="▼",
                               bg=self.theme_colors["bg_primary"],
                               fg=self.theme_colors["fg_secondary"],
                               font=(get_system_font()[0], max(get_system_font()[1]-1, 9), "bold"))
        arrow_label.grid(row=0, column=2, sticky="n", padx=(6,4), pady=(2,0))
        self.arrow_label = arrow_label

        # Create text widget container with modern styling
        text_container = tk.Frame(main_frame, bg=self.theme_colors["bg_primary"])
        text_container.grid(row=2, column=0, sticky="nsew", pady=(0, 24))
        text_container.columnconfigure(0, weight=1)
        text_container.rowconfigure(0, weight=1)
        self.text_container = text_container
        
        # Modern text widget
        self.text_widget = tk.Text(text_container, height=12)
        apply_modern_style(self.text_widget, "text", self.theme_colors)
        self.text_widget.grid(row=0, column=0, sticky="nsew", padx=(0, 2))
        
        # Modern scrollbar for text widget
        text_scrollbar = tk.Scrollbar(text_container, orient="vertical", command=self.text_widget.yview)
        apply_modern_style(text_scrollbar, "scrollbar", self.theme_colors)
        text_scrollbar.grid(row=0, column=1, sticky="ns")
        self.text_widget.configure(yscrollcommand=text_scrollbar.set)
        
        # Set default value with better formatting
        if default_value:
            self.text_widget.insert("1.0", default_value)

        # Custom prompt checkboxes loaded from custom_prompts.csv
        self.prompt_vars = []
        self.prompt_meta = []
        custom_prompts = _get_multiline_input_custom_prompts()
        if custom_prompts:
            checkbox_frame = tk.Frame(main_frame, bg=self.theme_colors["bg_secondary"])
            checkbox_frame.grid(row=3, column=0, sticky="ew", pady=(0, 8))
            self.checkbox_frame = checkbox_frame
            for active, active_color, sentence in custom_prompts:
                var = tk.BooleanVar(value=active)

                row_frame = tk.Frame(checkbox_frame, bg=self.theme_colors["bg_secondary"])
                row_frame.pack(fill="x", padx=8, pady=2)

                cb = tk.Checkbutton(
                    row_frame,
                    text="",
                    variable=var,
                    command=self._on_prompt_selection_change,
                    bg=self.theme_colors["bg_secondary"],
                    fg=self.theme_colors["fg_secondary"],
                    selectcolor=self.theme_colors["bg_primary"],
                    activebackground=self.theme_colors["bg_secondary"],
                    font=get_system_font(),
                )
                cb.pack(side=tk.LEFT)

                text_height = max(1, min(6, (len(sentence) // 75) + 1))
                row_text = tk.Text(
                    row_frame,
                    height=text_height,
                    wrap="word",
                    bg=self.theme_colors["bg_secondary"],
                    fg=self.theme_colors["fg_secondary"],
                    font=get_system_font(),
                    relief="flat",
                    borderwidth=0,
                    padx=2,
                    pady=1,
                    highlightthickness=0,
                    cursor="xterm",
                )
                row_text.insert("1.0", sentence)
                row_text.configure(state="disabled")
                row_text.pack(side=tk.LEFT, fill="x", expand=True, padx=(6, 0))
                row_text.bind("<Button-1>", lambda e, w=row_text: w.focus_set())
                row_text.bind("<Control-c>", self._copy_selected_prompt_text)
                row_text.bind("<Control-C>", self._copy_selected_prompt_text)

                self.prompt_vars.append(var)
                self.prompt_meta.append({
                    "active_color": active_color,
                    "checkbutton": cb,
                    "text_widget": row_text,
                    "row_frame": row_frame,
                    "var": var,
                })
        else:
            self.checkbox_frame = None

        # Modern button frame
        button_frame = tk.Frame(main_frame, bg=self.theme_colors["bg_primary"])
        button_frame.grid(row=4, column=0, sticky="ew")
        self.button_frame = button_frame
        
                # Height configurator (left side of button row)
        height_ctrl_frame = tk.Frame(button_frame, bg=self.theme_colors["bg_primary"])
        height_ctrl_frame.pack(side=tk.LEFT, padx=(0, 0))
        self.height_ctrl_frame = height_ctrl_frame
        tk.Label(
            height_ctrl_frame,
            text="Window height:",
            bg=self.theme_colors["bg_primary"],
            fg=self.theme_colors["fg_secondary"],
            font=get_system_font(),
        ).pack(side=tk.LEFT)
        self._height_var = tk.StringVar(value=str(_dlg_height))
        height_spin = tk.Spinbox(
            height_ctrl_frame,
            from_=300, to=1500, increment=10,
            textvariable=self._height_var,
            width=6,
            bg=self.theme_colors["bg_secondary"],
            fg=self.theme_colors["fg_primary"],
            font=get_system_font(),
            relief="flat",
            buttonbackground=self.theme_colors["bg_secondary"],
        )
        height_spin.pack(side=tk.LEFT, padx=(6, 0))
        height_spin.bind("<Return>", self._on_height_change)
        height_spin.bind("<FocusOut>", self._on_height_change)
# Create modern buttons
        self.ok_button = create_modern_button(
            button_frame, "OK", self.ok_clicked, "primary", self.theme_colors
        )
        self.ok_button.pack(side=tk.RIGHT, padx=(8, 0))
        
        self.cancel_button = create_modern_button(
            button_frame, "Cancel", self.cancel_clicked, "secondary", self.theme_colors
        )
        self.cancel_button.pack(side=tk.RIGHT)
        
        # Handle window close
        self.dialog.protocol("WM_DELETE_WINDOW", self.cancel_clicked)
        
        # Add keyboard shortcuts
        self.dialog.bind('<Control-Return>', lambda e: self.ok_clicked())
        self.dialog.bind('<Escape>', lambda e: self.cancel_clicked())

        # Disable always-on-top so the window can be freely minimized.
        # Must come after configure_modern_window which sets topmost=True on Windows.
        self.dialog.attributes('-topmost', False)
        self.text_widget.focus_set()

        # Apply initial active-color state based on pre-checked prompts
        self._on_prompt_selection_change()

        # Resurface every 60 seconds to remind the user the dialog is still open
        self._reminder_id = None
        self._reminder_id = self.dialog.after(60000, self._reminder)

        # No wait_window() here — the caller thread blocks on self._done_event.wait()
        # instead, allowing multiple dialogs to be open and active simultaneously.
    
    def center_window(self):
        """Center the dialog window on screen"""
        self.dialog.update_idletasks()
        width = self.dialog.winfo_width()
        height = self.dialog.winfo_height()
        
        # Get screen dimensions
        screen_width = self.dialog.winfo_screenwidth()
        screen_height = self.dialog.winfo_screenheight()
        
        # Calculate center position
        x = (screen_width // 2) - (width // 2)
        y = (screen_height // 2) - (height // 2)
        
        # Platform-specific adjustments
        if IS_MACOS:
            y = max(50, y - 50)
        elif IS_WINDOWS:
            y = max(30, y - 30)
        
        self.dialog.geometry(f"{width}x{height}+{x}+{y}")
    
    def _reminder(self):
        """Resurface the dialog every 60 seconds ONLY if it is minimized."""
        try:
            if self.dialog.winfo_exists():
                # Only restore and lift when actually minimized; don't disturb an active window
                if self.dialog.wm_state() == 'iconic':
                    self.dialog.deiconify()
                    self.dialog.lift()
                # Schedule the next reminder
                self._reminder_id = self.dialog.after(60000, self._reminder)
        except Exception as exc:
            print(f"[Dialog] Reminder loop error: {type(exc).__name__}: {exc}")

    def _on_prompt_selection_change(self):
        """Apply active_color only to selected checkbox rows and text object.

        - active_color=0 is ignored.
        - The dialog/window and text input backgrounds are not changed.
        """
        try:  # noqa: SIM105
            default_row_bg = "light gray"
            try:
                self.dialog.winfo_rgb(default_row_bg)
            except Exception:
                default_row_bg = self._base_bg_secondary

            # Update each checkbox row independently.
            for meta in self.prompt_meta:
                cb = meta["checkbutton"]
                row_text = meta["text_widget"]
                row_frame = meta["row_frame"]
                row_color = None
                is_checked = meta["var"].get()
                if is_checked:
                    normalized = _normalize_tk_color(meta.get("active_color", "0"))
                    if normalized:
                        try:
                            self.dialog.winfo_rgb(normalized)
                            row_color = normalized
                        except Exception:
                            row_color = None

                if is_checked:
                    target_row_bg = row_color or default_row_bg
                else:
                    target_row_bg = self._base_bg_secondary
                row_frame.configure(bg=target_row_bg)
                cb.configure(
                    bg=target_row_bg,
                    activebackground=target_row_bg,
                    selectcolor=self._base_bg_primary,
                )
                row_text.configure(bg=target_row_bg)
        except Exception as exc:
            print(f"[Dialog] _on_prompt_selection_change error: "
                  f"{type(exc).__name__}: {exc}")

    def _copy_selected_prompt_text(self, event=None):
        """Copy selected text from a prompt row text widget using Ctrl+C."""
        try:
            widget = event.widget
            if widget.tag_ranges("sel"):
                selected = widget.get("sel.first", "sel.last")
                if selected:
                    self.dialog.clipboard_clear()
                    self.dialog.clipboard_append(selected)
        except Exception:
            pass
        return "break"

    def ok_clicked(self):
        # Cancel the periodic reminder before closing
        try:
            if self._reminder_id is not None:
                self.dialog.after_cancel(self._reminder_id)
        except Exception:
            pass
        base = self.text_widget.get("1.0", tk.END).strip()
        # Append checked custom prompts to the answer
        custom_prompts = _get_multiline_input_custom_prompts()
        checked = [text for (_active, _active_color, text), var in zip(custom_prompts, self.prompt_vars) if var.get()]
        if checked:
            separator = "\n\n" if base else ""
            base = base + separator + "\n\n".join(checked)
        self.result = base
        self.dialog.destroy()
        if self._done_event is not None:
            self._done_event.set()

    def cancel_clicked(self):
        # Cancel the periodic reminder before closing
        try:
            if self._reminder_id is not None:
                self.dialog.after_cancel(self._reminder_id)
        except Exception:
            pass
        self.result = None
        self.dialog.destroy()
        if self._done_event is not None:
            self._done_event.set()

    def _on_height_change(self, event=None):
        """Called when the user changes the height spinbox value.
        Resizes the dialog window and persists the new height to dialog_config.json."""
        try:
            new_height = int(self._height_var.get())
            new_height = max(300, min(1500, new_height))
            # Get current width and apply the new height
            current_geom = self.dialog.geometry()
            width_part = current_geom.split('x')[0]
            self.dialog.geometry(f"{width_part}x{new_height}")
            _save_persisted_dialog_height(new_height)
        except (ValueError, tk.TclError):
            pass  # Ignore invalid spinbox values

# MCP Tools

@mcp.tool()
async def get_user_input(
    title: Annotated[str, Field(description="Title of the input dialog window")],
    prompt: Annotated[str, Field(description="The prompt/question to show to the user")],
    default_value: Annotated[str, Field(description="Default value to pre-fill in the input field")] = "",
    input_type: Annotated[Literal["text", "integer", "float"], Field(description="Type of input expected")] = "text",
    ctx: Context = None
) -> Dict[str, Any]:
    """
    Create an input dialog window for the user to enter text, numbers, or other data.

    This tool opens a GUI dialog box where the user can input information that the LLM needs.
    Perfect for getting specific details, clarifications, or data from the user.
    """
    # Check bypass mode
    bypass_result = _check_bypass("get_user_input", {"title": title, "prompt": prompt})
    if bypass_result is not None:
        return bypass_result

    try:
        if ctx:
            await ctx.info(f"Requesting user input: {prompt}")

        # Ensure GUI is initialized
        if not ensure_gui_initialized():
            return {
                "success": False,
                "error": (
                    f"get_user_input: GUI subsystem unavailable on platform "
                    f"'{CURRENT_PLATFORM}'. Ensure a display server is running "
                    "or use get_remote_input with Telegram configured."
                ),
                "cancelled": False,
                "platform": CURRENT_PLATFORM
            }

        # Create the dialog in a separate thread to avoid blocking
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as executor:
            future = executor.submit(create_input_dialog, title, prompt, default_value, input_type)
            result = future.result(timeout=_get_tool_timeout())  # configurable via --tool-timeout arg

        if result is not None:
            if ctx:
                await ctx.info(f"User provided input: {result}")
            return {
                "success": True,
                "user_input": result,
                "input_type": input_type,
                "cancelled": False,
                "platform": CURRENT_PLATFORM
            }
        else:
            if ctx:
                await ctx.warning("User cancelled the input dialog")
            return {
                "success": False,
                "user_input": None,
                "input_type": input_type,
                "cancelled": True,
                "platform": CURRENT_PLATFORM
            }

    except Exception as e:
        if ctx:
            await ctx.error(f"Error creating input dialog: {str(e)}")
        return {
            "success": False,
            "error": str(e),
            "cancelled": False,
            "platform": CURRENT_PLATFORM
        }

@mcp.tool()
async def get_user_choice(
    title: Annotated[str, Field(description="Title of the choice dialog window")],
    prompt: Annotated[str, Field(description="The prompt/question to show to the user")],
    choices: Annotated[List[str], Field(description="List of choices to present to the user")],
    allow_multiple: Annotated[bool, Field(description="Whether user can select multiple choices")] = False,
    ctx: Context = None
) -> Dict[str, Any]:
    """
    Create a choice dialog window for the user to select from multiple options.
    
    This tool opens a GUI dialog box with a list of choices where the user can select
    one or multiple options. Perfect for getting decisions, preferences, or selections from the user.
    """
    # Check bypass mode
    bypass_result = _check_bypass("get_user_choice", {"title": title, "prompt": prompt})
    if bypass_result is not None:
        return bypass_result

    try:
        if ctx:
            await ctx.info(f"Requesting user choice: {prompt}")
            await ctx.debug(f"Available choices: {choices}")
        
        # Ensure GUI is initialized
        if not ensure_gui_initialized():
            return {
                "success": False,
                "error": (
                    f"get_user_choice: GUI subsystem unavailable on platform "
                    f"'{CURRENT_PLATFORM}'. Ensure a display server is running "
                    "or use get_remote_input with Telegram configured."
                ),
                "cancelled": False,
                "platform": CURRENT_PLATFORM
            }
        
        # Create the dialog in a separate thread
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as executor:
            future = executor.submit(create_choice_dialog, title, prompt, choices, allow_multiple)
            result = future.result(timeout=_get_tool_timeout())  # configurable via --tool-timeout arg
        
        if result is not None:
            if ctx:
                await ctx.info(f"User selected: {result}")
            return {
                "success": True,
                "selected_choice": result,
                "selected_choices": result if isinstance(result, list) else [result],
                "allow_multiple": allow_multiple,
                "cancelled": False,
                "platform": CURRENT_PLATFORM
            }
        else:
            if ctx:
                await ctx.warning("User cancelled the choice dialog")
            return {
                "success": False,
                "selected_choice": None,
                "selected_choices": [],
                "allow_multiple": allow_multiple,
                "cancelled": True,
                "platform": CURRENT_PLATFORM
            }
    
    except Exception as e:
        if ctx:
            await ctx.error(f"Error creating choice dialog: {str(e)}")
        return {
            "success": False,
            "error": str(e),
            "cancelled": False,
            "platform": CURRENT_PLATFORM
        }

@mcp.tool()
async def get_multiline_input(
    title: Annotated[str, Field(description="Title of the input dialog window")],
    prompt: Annotated[str, Field(description="The prompt/question to show to the user")],
    default_value: Annotated[str, Field(description="Default text to pre-fill in the text area")] = "",
    ctx: Context = None
) -> Dict[str, Any]:
    """
    Create a multi-line text input dialog for the user to enter longer text content.
    
    This tool opens a GUI dialog box with a large text area where the user can input
    multiple lines of text. Perfect for getting detailed descriptions, code, or long-form content.
    """
    # Check bypass mode
    bypass_result = _check_bypass("get_multiline_input", {"title": title, "prompt": prompt})
    if bypass_result is not None:
        return bypass_result

    try:
        if ctx:
            await ctx.info(f"Requesting multiline user input: {prompt}")
        
        # Ensure GUI is initialized
        if not ensure_gui_initialized():
            return {
                "success": False,
                "error": (
                    f"get_multiline_input: GUI subsystem unavailable on platform "
                    f"'{CURRENT_PLATFORM}'. Ensure a display server is running "
                    "or use get_remote_input with Telegram configured."
                ),
                "cancelled": False,
                "platform": CURRENT_PLATFORM
            }
        
        # Run the blocking dialog function in a thread pool without blocking
        # the asyncio event loop — this allows multiple concurrent calls.
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None, create_multiline_input_dialog, title, prompt, default_value
        )
        
        if result is not None:
            if ctx:
                await ctx.info(f"User provided multiline input ({len(result)} characters)")
            return {
                "success": True,
                "user_input": result,
                "character_count": len(result),
                "line_count": len(result.split('\n')),
                "cancelled": False,
                "platform": CURRENT_PLATFORM
            }
        else:
            if ctx:
                await ctx.warning("User cancelled the multiline input dialog")
            return {
                "success": False,
                "user_input": None,
                "cancelled": True,
                "platform": CURRENT_PLATFORM
            }
    
    except Exception as e:
        if ctx:
            await ctx.error(f"Error creating multiline input dialog: {str(e)}")
        return {
            "success": False,
            "error": str(e),
            "cancelled": False,
            "platform": CURRENT_PLATFORM
        }


# ---------------------------------------------------------------------------
# Remote Input — Entangled tkinter + Telegram channels
# ---------------------------------------------------------------------------

class MiniAppSession:
    """Container for an active Mini App HTTP server + Cloudflare tunnel pair."""

    def __init__(self, http_server, tunnel, webapp_url: str) -> None:
        self.http_server = http_server      # MiniAppHTTPServer
        self.tunnel = tunnel                # CloudflareTunnel
        self.webapp_url = webapp_url        # Full https URL with token query param

    def stop(self) -> None:
        """Shut down both the HTTP server and the tunnel subprocess."""
        try:
            self.http_server.stop()
        except Exception:
            pass
        try:
            self.tunnel.stop()
        except Exception:
            pass


def _build_miniapp_session(
    title: str,
    prompt: str,
    all_prompts: list,
    name_or_role: str = "",
) -> Optional["MiniAppSession"]:
    """Attempt to start a Mini App HTTP server + Cloudflare quick tunnel.

    Returns a :class:`MiniAppSession` on success, or ``None`` when the Mini
    App infrastructure is not available (cloudflared not installed, import
    error, tunnel timeout, etc.).  Failures are printed but never raised so
    that callers can degrade gracefully.
    """
    if not _MINIAPP_AVAILABLE:
        return None

    try:
        token = _secrets.token_hex(16)

        # Build prompts list: all CSV rows, active ones pre-checked
        # all_prompts = [(active: bool, color: str, text: str), ...]
        prompts_data = [
            {"text": p[2], "checked": bool(p[0])}
            for p in all_prompts
            if (p[2] or "").strip()
        ]

        # Start local HTTP server first (instant, OS assigns port)
        http_server = MiniAppHTTPServer(
            title=title,
            prompt=prompt,
            prompts=prompts_data,
            token=token,
            tunnel_base_url="https://placeholder.trycloudflare.com",  # updated after tunnel starts
            name_or_role=name_or_role,
        )
        port = http_server.start()

        # Start Cloudflare tunnel (blocks up to 20 s)
        tunnel = CloudflareTunnel()
        try:
            public_url = tunnel.start(local_port=port, timeout=20.0)
        except TunnelNotAvailableError as exc:
            print(f"[MiniApp] Tunnel unavailable: {exc}")
            http_server.stop()
            return None

        # Update the server's tunnel URL now that we know the real address
        http_server._tunnel_base_url = public_url.rstrip("/")

        # Small propagation wait: Cloudflare CDN needs ~2–3 s to register the
        # ephemeral tunnel in its DNS before external clients can resolve it.
        # The Telegram message is sent after this pause, so by the time the
        # user taps the inline button the URL is fully reachable.
        time.sleep(3)

        webapp_url = f"{public_url.rstrip('/')}/?t={token}"
        print(f"[MiniApp] Mini App ready at {webapp_url}")
        return MiniAppSession(http_server, tunnel, webapp_url)

    except Exception as exc:
        print(f"[MiniApp] Failed to build Mini App session: {exc}")
        return None


def create_remote_input_dialog(
    title: str,
    prompt: str,
    default_value: str = "",
    name_or_role: str = "",
):
    """Create a multiline input dialog + Telegram prompt.

    Opens the same tkinter MultilineInputDialog as ``get_multiline_input``, but
    also sends the prompt to Telegram.  The user can answer from **either**
    channel — the first response wins:

    * **Tkinter answered** → cancel Telegram polling, edit Telegram message
      to show "answered locally".
    * **Telegram answered** → close the tkinter dialog, show a brief
      "answered via Telegram" flash before closing.

    If Telegram is not configured, this falls back to a plain tkinter dialog
    (identical to ``create_multiline_input_dialog``).
    """
    try:
        root = _ensure_persistent_root()
        if root is None:
            # ── Headless (no display) — Telegram-only mode with MiniApp support ──
            # Mirrors the full tkinter+Telegram path's MiniApp logic so that
            # headless environments (CI, SSH, containers) also get the rich
            # Telegram Mini App experience when infrastructure is available.
            if not is_telegram_configured() or TelegramBridge is None:
                return None

            _miniapp_session: Optional[MiniAppSession] = None
            try:
                _tg = TelegramBridge()

                # Attempt to build a MiniApp session (HTTP server + Cloudflare tunnel)
                _all_prompts = _get_multiline_input_custom_prompts()
                _miniapp_session = _build_miniapp_session(title, prompt, _all_prompts, name_or_role)

                if _miniapp_session:
                    # MiniApp path: send prompt with InlineKeyboard "Open" button
                    _msg_id = _tg.send_prompt_with_miniapp(
                        title, prompt, _miniapp_session.webapp_url, name_or_role
                    )
                    if _msg_id:
                        print(f"[RemoteInput][Headless] Telegram Mini App prompt sent (msg_id={_msg_id})")
                    else:
                        # send_prompt_with_miniapp failed — tear down MiniApp, fall back to plain
                        print("[RemoteInput][Headless] send_prompt_with_miniapp failed — falling back to plain prompt")
                        _miniapp_session.stop()
                        _miniapp_session = None
                        _msg_id = _tg.send_prompt(title, prompt)
                else:
                    # No MiniApp available — use plain prompt (original behaviour)
                    _msg_id = _tg.send_prompt(title, prompt)

                if not _msg_id:
                    return None

                # Poll for the answer using poll_for_answer (supports MiniApp
                # answer_queue, web_app_data, and plain-text replies) when
                # available, otherwise fall back to legacy poll_for_reply.
                import threading as _threading
                _cancel = _threading.Event()

                _answer_queue = (
                    _miniapp_session.http_server.answer_queue
                    if _miniapp_session else None
                )

                if hasattr(_tg, "poll_for_answer"):
                    _reply = _tg.poll_for_answer(
                        _msg_id, _cancel, answer_queue=_answer_queue
                    )
                else:
                    # Fallback for older TelegramBridge without poll_for_answer
                    _reply = _tg.poll_for_reply(_msg_id, cancel_event=_cancel)

                # Edit the Telegram message with final status (mirrors the
                # cleanup logic from the full tkinter+Telegram path).
                if _reply is not None:
                    _esc = _tg._escape_html
                    _display = (_reply or "")[:2000]
                    _prompt_trunc = prompt[:3000]
                    if len(prompt) > 3000:
                        _prompt_trunc += "\n...(truncated)"
                    _edited_text = (
                        f"\U0001f5a5\ufe0f <b>{_esc(title)}</b>\n\n"
                        f"Original message:\n"
                        f"<blockquote expandable>{_esc(_prompt_trunc)}</blockquote>\n\n"
                        f"Response via Telegram:\n"
                        f"<blockquote expandable>{_esc(_display)}</blockquote>"
                    )
                    try:
                        _tg.edit_message(_msg_id, _edited_text, parse_mode="HTML")
                    except Exception:
                        # Best-effort: if HTML edit fails, fall back to simple status
                        try:
                            _tg.edit_message(_msg_id, "\u2705 Response received.")
                        except Exception:
                            pass
                else:
                    # No reply (timeout / cancellation)
                    try:
                        _tg.edit_message(_msg_id, "\u23f0 Timed out — no response received.")
                    except Exception:
                        pass

                return _reply

            except Exception as _exc:
                print(f"[RemoteInput] Telegram-only mode error: {_exc}")
                return None
            finally:
                # Always clean up MiniApp session (HTTP server + tunnel)
                if _miniapp_session is not None:
                    try:
                        _miniapp_session.stop()
                    except Exception:
                        pass

        # --- Shared synchronisation primitives ---
        master_done = threading.Event()   # fires when EITHER channel answers
        tg_cancel   = threading.Event()   # tells the TG poller to stop
        tkinter_done = threading.Event()  # dialog's internal done event

        result_container: Dict[str, Any] = {
            "text": None,
            "source": None,        # "tkinter" | "telegram" | None
            "dialog": None,
        }

        # --- 1. Initialise Telegram bridge (optional) ---
        tg_bridge: Optional[Any] = None
        tg_msg_id: Optional[int] = None
        miniapp_session: Optional[MiniAppSession] = None

        if is_telegram_configured() and TelegramBridge is not None:
            try:
                tg_bridge = TelegramBridge()

                # --- 1a. Attempt Mini App session (Cloudflare tunnel + HTTP server) ---
                all_prompts = _get_multiline_input_custom_prompts()
                miniapp_session = _build_miniapp_session(title, prompt, all_prompts, name_or_role)

                if miniapp_session:
                    # Mini App path: send prompt with InlineKeyboard Open button
                    tg_msg_id = tg_bridge.send_prompt_with_miniapp(
                        title, prompt, miniapp_session.webapp_url, name_or_role
                    )
                    if tg_msg_id:
                        print(f"[RemoteInput] Telegram Mini App prompt sent (msg_id={tg_msg_id})")
                    else:
                        print("[RemoteInput] send_prompt_with_miniapp failed — falling back to plain prompt")
                        miniapp_session.stop()
                        miniapp_session = None
                        tg_msg_id = tg_bridge.send_prompt(title, prompt)

                if not miniapp_session:
                    # Plain prompt path (no Mini App)
                    if tg_msg_id is None:
                        tg_msg_id = tg_bridge.send_prompt(title, prompt)
                    if tg_msg_id:
                        print(f"[RemoteInput] Telegram prompt sent (msg_id={tg_msg_id})")
                    else:
                        print("[RemoteInput] Failed to send Telegram prompt -- continuing with tkinter only")
                        tg_bridge = None

            except Exception as exc:
                print(f"[RemoteInput] Telegram init error: {exc} -- continuing with tkinter only")
                if miniapp_session:
                    miniapp_session.stop()
                    miniapp_session = None
                tg_bridge = None

        telegram_active = tg_bridge is not None and tg_msg_id is not None

        # --- 2. Create tkinter dialog on the GUI thread ---
        def _create_on_gui():
            try:
                dlg = MultilineInputDialog(
                    root, title, prompt, default_value, done_event=tkinter_done
                )
                result_container["dialog"] = dlg

                # Inject agent role label ABOVE the title if provided.
                # We shift every existing content row down by 1 so the badge
                # occupies row=0, the title moves to row=1, and so on.
                _row_offset = 0
                if name_or_role:
                    try:
                        # Shift existing rows down by 1
                        for widget, orig_row in [
                            (dlg.title_label,      0),
                            (dlg.prompt_container, 1),
                            (dlg.text_container,   2),
                        ]:
                            info = widget.grid_info()
                            widget.grid(**{**info, "row": orig_row + 1})
                        if hasattr(dlg, "checkbox_frame") and dlg.checkbox_frame is not None:
                            info = dlg.checkbox_frame.grid_info()
                            dlg.checkbox_frame.grid(**{**info, "row": 4})
                        info = dlg.button_frame.grid_info()
                        dlg.button_frame.grid(**{**info, "row": 5})
                        # Move the expanding-row weight to the new index
                        dlg.main_frame.rowconfigure(2, weight=0)
                        dlg.main_frame.rowconfigure(3, weight=1)
                        _row_offset = 1

                        role_label = tk.Label(
                            dlg.main_frame,
                            text=f"\U0001f916  {name_or_role}",
                            bg=dlg.theme_colors["bg_secondary"],
                            fg=dlg.theme_colors["fg_secondary"],
                            font=get_system_font(),
                            anchor="w",
                            padx=8,
                            pady=2,
                        )
                        role_label.grid(row=0, column=0, sticky="ew", pady=(0, 4))
                    except Exception:
                        pass

                # Inject a Telegram status indicator into the dialog
                if telegram_active:
                    try:
                        if miniapp_session:
                            tg_label_text = "\U0001f4f2  Telegram Mini App active — tap the button in the chat to respond"
                        else:
                            tg_label_text = "\U0001f4e1  Telegram link active — you can also reply from your phone"
                        tg_label = tk.Label(
                            dlg.main_frame,
                            text=tg_label_text,
                            bg=dlg.theme_colors["bg_secondary"],
                            fg=dlg.theme_colors["accent_color"],
                            font=get_system_font(),
                            anchor="w",
                            padx=8,
                            pady=4,
                        )
                        tg_label.grid(row=5 + _row_offset, column=0, sticky="ew", pady=(4, 0))
                        dlg._tg_label = tg_label
                    except Exception:
                        pass

                try:
                    dlg.dialog.lift()
                except Exception:
                    pass
            except Exception as e:
                print(f"[RemoteInput] Error creating dialog: {e}")
                tkinter_done.set()

        _dialog_request_queue.put(_create_on_gui)

        # --- 3. Telegram poller thread ---
        def _telegram_poller():
            if not tg_bridge or not tg_msg_id:
                return
            # Use poll_for_answer which handles Mini App queue, web_app_data,
            # and plain-text replies in a unified way.
            _answer_queue = miniapp_session.http_server.answer_queue if miniapp_session else None
            if hasattr(tg_bridge, "poll_for_answer"):
                reply = tg_bridge.poll_for_answer(tg_msg_id, tg_cancel, answer_queue=_answer_queue)
            else:
                # Fallback for older TelegramBridge instances without poll_for_answer
                reply = tg_bridge.poll_for_reply(tg_msg_id, tg_cancel)
            if reply is not None and not master_done.is_set():
                result_container["text"] = reply
                result_container["source"] = "telegram"
                master_done.set()

                # Close the tkinter dialog from the GUI thread
                def _close_tkinter():
                    dlg = result_container.get("dialog")
                    if dlg is None:
                        return
                    try:
                        # Cancel the periodic reminder
                        if hasattr(dlg, '_reminder_id') and dlg._reminder_id is not None:
                            dlg.dialog.after_cancel(dlg._reminder_id)
                    except Exception:
                        pass
                    try:
                        # Update Telegram indicator to show remote answer
                        if hasattr(dlg, '_tg_label'):
                            dlg._tg_label.configure(
                                text="\u2705  User answered via Telegram \u2014 closing\u2026",
                                fg=dlg.theme_colors.get("success_color", "#137333"),
                            )
                        dlg.result = reply
                        # Brief delay so user sees the status change, then close
                        dlg.dialog.after(1200, dlg.dialog.destroy)
                    except Exception as _exc:
                        print(f"[RemoteInput] Error updating dialog after Telegram reply: "
                              f"{type(_exc).__name__}: {_exc}")
                        try:
                            dlg.dialog.destroy()
                        except Exception as _exc2:
                            print(f"[RemoteInput] Could not destroy dialog: {_exc2}")
                    # Signal the tkinter done event to unblock the monitor
                    if dlg._done_event:
                        dlg._done_event.set()

                _dialog_request_queue.put(_close_tkinter)

        tg_thread = threading.Thread(
            target=_telegram_poller, daemon=True, name="tg-remote-input-poller"
        )
        tg_thread.start()

        # --- 4. Monitor thread for tkinter completion ---
        def _tkinter_monitor():
            tkinter_done.wait()
            dlg = result_container.get("dialog")
            if dlg and not master_done.is_set():
                result_container["text"] = dlg.result
                result_container["source"] = "tkinter"
                master_done.set()
                tg_cancel.set()   # stop telegram poller

        tk_monitor = threading.Thread(
            target=_tkinter_monitor, daemon=True, name="tkinter-done-monitor"
        )
        tk_monitor.start()

        # --- 5. Wait for either channel to deliver a result ---
        timeout = _get_tool_timeout()
        master_done.wait(timeout=timeout)

        # --- 6. Cleanup: update the Telegram message with final status ---
        if tg_cancel is not None:
            tg_cancel.set()  # ensure poller stops

        # Stop Mini App session (HTTP server + tunnel) if one was active
        if miniapp_session is not None:
            try:
                miniapp_session.stop()
            except Exception:
                pass

        # File-based diagnostic logger (charmap-safe: writes UTF-8 to file,
        # ASCII-only to stdout to avoid Windows encoding errors).
        _diag_log = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_remote_input_diag.log")
        def _diag(msg):
            try:
                with open(_diag_log, "a", encoding="utf-8") as f:
                    f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
            except Exception:
                pass
            try:
                safe = msg.encode("ascii", "replace").decode("ascii")
                print(f"[RemoteInput] {safe}")
            except Exception:
                pass

        _diag(f"Cleanup reached. tg_bridge={tg_bridge is not None}, tg_msg_id={tg_msg_id}")

        if tg_bridge and tg_msg_id:
            source = result_container.get("source")
            text   = result_container.get("text")
            _diag(f"source={source}, text_len={len(text) if text else 0}")

            # Small delay when the reply came from Telegram — gives the API
            # time to fully process the reply before we edit the prompt message.
            if source == "telegram":
                time.sleep(0.5)

            # Truncate the original prompt and escape for HTML
            _esc = tg_bridge._escape_html
            original_prompt_truncated = prompt[:3000]
            if len(prompt) > 3000:
                original_prompt_truncated += "\n...(truncated)"

            # Build the edited message using HTML with expandable blockquotes so
            # both the original prompt and the user reply are collapsible.
            if source == "telegram":
                display_text = (text or "")[:2000]
                status_label = "Response via Telegram"
                status_footer = (
                    f"{status_label}:\n"
                    f"<blockquote expandable>{_esc(display_text)}</blockquote>"
                )
            elif source == "tkinter" and text is not None:
                display_text = (text or "")[:2000]
                status_label = "User answered via local dialog"
                status_footer = (
                    f"\u2705 {status_label}:\n"
                    f"<blockquote expandable>{_esc(display_text)}</blockquote>"
                )
            elif source == "tkinter" and text is None:
                status_label = "Cancelled locally"
                status_footer = f"\u274c {status_label}"
            else:
                status_label = "Timed out"
                status_footer = f"\u23f0 {status_label}"

            edited_text = (
                f"\U0001f5a5\ufe0f <b>{_esc(title)}</b>\n\n"
                f"Original message:\n"
                f"<blockquote expandable>{_esc(original_prompt_truncated)}</blockquote>\n\n"
                f"{status_footer}"
            )

            _diag(f"Editing Telegram message (msg_id={tg_msg_id}, status={status_label})")

            # Edit the prompt message (sent without reply_markup, so editable)
            ok = False
            try:
                ok = tg_bridge.edit_message(tg_msg_id, edited_text, parse_mode="HTML")
                _diag(f"edit_message returned: {ok}")
            except Exception as exc:
                _diag(f"edit_message exception: {exc}")

            # Fallback: if edit failed, send a follow-up reply instead
            if not ok:
                try:
                    ok = tg_bridge.send_status(tg_msg_id, status_footer)
                    _diag(f"send_status fallback returned: {ok}")
                except Exception as exc:
                    _diag(f"send_status fallback exception: {exc}")

            _diag(f"Final result: {'OK' if ok else 'FAILED'}")
        else:
            _diag(f"Skipped Telegram cleanup (bridge or msg_id not set)")

        return result_container.get("text")

    except Exception as e:
        import traceback as _tb
        print(f"[RemoteInput] Unexpected error in create_remote_input_dialog "
              f"(title={title!r}): {type(e).__name__}: {e}")
        _tb.print_exc()
        # Best-effort cleanup of Mini App session on unexpected errors
        try:
            if "miniapp_session" in dir() and miniapp_session is not None:
                miniapp_session.stop()
        except Exception:
            pass
        return None


@mcp.tool()
async def get_remote_input(
    title: Annotated[str, Field(description="Title of the input dialog window")],
    prompt: Annotated[str, Field(description="The prompt/question to show to the user")],
    default_value: Annotated[str, Field(description="Default text to pre-fill in the text area")] = "",
    name_or_role: Annotated[str, Field(description="Name or role of the AI agent making the request (e.g. 'Orchestrator', 'Code Reviewer'). Shown as a badge in the Telegram Mini App and dialog.")] = "",
    ctx: Context = None
) -> Dict[str, Any]:
    """
    Create a multi-line text input dialog **with Telegram remote answering**.

    Opens the same tkinter dialog as ``get_multiline_input`` and simultaneously
    sends the prompt to a configured Telegram chat.  The user can respond from
    **either** the local tkinter window or from Telegram on their phone:

    * Answering from **tkinter** closes the Telegram prompt.
    * Answering from **Telegram** closes the tkinter window.

    If Telegram is not configured (no ``telegram_config.json`` and no
    ``TELEGRAM_BOT_TOKEN`` / ``TELEGRAM_CHAT_ID`` env vars), this tool
    behaves identically to ``get_multiline_input`` — tkinter only.
    """
    # Check bypass mode
    bypass_result = _check_bypass("get_remote_input", {"title": title, "prompt": prompt})
    if bypass_result is not None:
        return bypass_result

    try:
        if ctx:
            tg_status = "active" if is_telegram_configured() else "not configured (tkinter only)"
            await ctx.info(
                f"Requesting remote input: {prompt[:80]}… | Telegram: {tg_status}"
            )

        # Ensure GUI is initialised
        if not ensure_gui_initialized() and not is_telegram_configured():
            return {
                "success": False,
                "error": (
                    f"get_remote_input: neither GUI nor Telegram is available on platform "
                    f"'{CURRENT_PLATFORM}'. Ensure a display server is running or "
                    "configure Telegram (telegram_config.json or "
                    "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID env vars)."
                ),
                "cancelled": False,
                "platform": CURRENT_PLATFORM
            }

        # Run the blocking orchestration function in a thread pool
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None, create_remote_input_dialog, title, prompt, default_value, name_or_role
        )

        if result is not None:
            if ctx:
                await ctx.info(f"User provided remote input ({len(result)} characters)")
            return {
                "success": True,
                "user_input": result,
                "character_count": len(result),
                "line_count": len(result.split('\n')),
                "cancelled": False,
                "platform": CURRENT_PLATFORM
            }
        else:
            if ctx:
                await ctx.warning("User cancelled the remote input dialog")
            return {
                "success": False,
                "user_input": None,
                "cancelled": True,
                "platform": CURRENT_PLATFORM
            }

    except Exception as e:
        if ctx:
            await ctx.error(f"Error creating remote input dialog: {str(e)}")
        return {
            "success": False,
            "error": str(e),
            "cancelled": False,
            "platform": CURRENT_PLATFORM
        }


@mcp.tool()
async def show_confirmation_dialog(
    title: Annotated[str, Field(description="Title of the confirmation dialog")],
    message: Annotated[str, Field(description="The message to show to the user")],
    ctx: Context = None
) -> Dict[str, Any]:
    """
    Show a confirmation dialog with Yes/No buttons.
    
    This tool displays a message to the user and asks for confirmation.
    Perfect for getting approval before proceeding with an action.
    """
    # Check bypass mode
    bypass_result = _check_bypass("show_confirmation_dialog", {"title": title, "message": message})
    if bypass_result is not None:
        return bypass_result

    try:
        if ctx:
            await ctx.info(f"Requesting user confirmation: {message}")
        
        # Ensure GUI is initialized
        if not ensure_gui_initialized():
            return {
                "success": False,
                "error": (
                    f"show_confirmation_dialog: GUI subsystem unavailable on platform "
                    f"'{CURRENT_PLATFORM}'. Ensure a display server is running."
                ),
                "confirmed": False,
                "platform": CURRENT_PLATFORM
            }
        
        # Create the dialog in a separate thread
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as executor:
            future = executor.submit(show_confirmation, title, message)
            result = future.result(timeout=_get_tool_timeout())  # configurable via --tool-timeout arg
        
        if ctx:
            await ctx.info(f"User confirmation result: {'Yes' if result else 'No'}")
        
        return {
            "success": True,
            "confirmed": result,
            "response": "yes" if result else "no",
            "platform": CURRENT_PLATFORM
        }
    
    except Exception as e:
        if ctx:
            await ctx.error(f"Error showing confirmation dialog: {str(e)}")
        return {
            "success": False,
            "error": str(e),
            "confirmed": False,
            "platform": CURRENT_PLATFORM
        }

@mcp.tool()
async def show_info_message(
    title: Annotated[str, Field(description="Title of the information dialog")],
    message: Annotated[str, Field(description="The information message to show to the user")],
    ctx: Context = None
) -> Dict[str, Any]:
    """
    Show an information message to the user.
    
    This tool displays an informational message dialog to notify the user about something.
    The user just needs to click OK to acknowledge the message.
    """
    # Check bypass mode
    bypass_result = _check_bypass("show_info_message", {"title": title, "message": message})
    if bypass_result is not None:
        return bypass_result

    try:
        if ctx:
            await ctx.info(f"Showing info message to user: {message}")
        
        # Ensure GUI is initialized
        if not ensure_gui_initialized():
            return {
                "success": False,
                "error": (
                    f"show_info_message: GUI subsystem unavailable on platform "
                    f"'{CURRENT_PLATFORM}'. Ensure a display server is running."
                ),
                "platform": CURRENT_PLATFORM
            }
        
        # Create the dialog in a separate thread
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as executor:
            future = executor.submit(show_info, title, message)
            result = future.result(timeout=_get_tool_timeout())  # configurable via --tool-timeout arg
        
        if ctx:
            await ctx.info("Info message acknowledged by user")
        
        return {
            "success": True,
            "acknowledged": result,
            "platform": CURRENT_PLATFORM
        }
    
    except Exception as e:
        if ctx:
            await ctx.error(f"Error showing info message: {str(e)}")
        return {
            "success": False,
            "error": str(e),
            "platform": CURRENT_PLATFORM
        }

# Add a prompt to get prompting guidance for LLMs
@mcp.prompt()
async def get_human_loop_prompt() -> Dict[str, str]:
    """
    Get prompting guidance for LLMs on when and how to use human-in-the-loop tools.
    
    This tool returns comprehensive guidance that helps LLMs understand when to pause
    and ask for human input, decisions, or feedback during task execution.
    """
    guidance = {
        "main_prompt": """
You have access to Human-in-the-Loop tools that allow you to interact directly with users through GUI dialogs. Use these tools strategically to enhance task completion and user experience.

**WHEN TO USE HUMAN-IN-THE-LOOP TOOLS:**

1. Always call this at the end of your response so that user can comment on your response or add valuable feedback
2. **Ambiguous Requirements** - When user instructions are unclear or could have multiple interpretations
3. **Decision Points** - When you need user preference between valid alternatives
4. **Creative Input** - For subjective choices like design, content style, or personal preferences
5. **Sensitive Operations** - Before executing potentially destructive or irreversible actions
6. **Missing Information** - When you need specific details not provided in the original request
7. **Quality Feedback** - To get user validation on intermediate results before proceeding
8. **Error Handling** - When encountering issues that require user guidance to resolve

**AVAILABLE TOOLS:**
- `get_user_input` - Single-line text/number input (names, values, paths, etc.)
- `get_user_choice` - Multiple choice selection (pick from options)
- `get_multiline_input` - Long-form text (descriptions, code, documents)
- `get_remote_input` - Long-form text with Telegram remote answering (same as get_multiline_input, plus the user can reply from Telegram)
- `show_confirmation_dialog` - Yes/No decisions (confirmations, approvals)
- `show_info_message` - Status updates and notifications

**BEST PRACTICES:**
- Ask specific, clear questions with context
- Provide helpful default values when possible
- Use confirmation dialogs before destructive actions
- Give status updates for long-running processes
- Offer meaningful choices rather than overwhelming options
- Be concise but informative in dialog prompts
- Use \n for line breaks (no escaping needed) to format messages clearly
""",
        
        "usage_examples": """
**EXAMPLE SCENARIOS:**

1. **File Operations:**
   - "I'm about to delete 15 files. Should I proceed?" (confirmation)
   - "Enter the target directory path:" (input)
   - "Choose backup format: Full, Incremental, Differential" (choice)

2. **Content Creation:**
   - "What tone should I use: Professional, Casual, Friendly?" (choice)
   - "Please provide any specific requirements:" (multiline input)
   - "Content generated successfully!" (info message)

3. **Code Development:**
   - "Enter the API endpoint URL:" (input)
   - "Select framework: React, Vue, Angular, Vanilla JS" (choice)
   - "Review the generated code and provide feedback:" (multiline input)

4. **Data Processing:**
   - "Found 3 data formats. Which should I use?" (choice)
   - "Enter the date range (YYYY-MM-DD to YYYY-MM-DD):" (input)
   - "Processing complete. 1,250 records updated." (info message)""",
        
        "decision_framework": """
**DECISION FRAMEWORK FOR HUMAN-IN-THE-LOOP:**

ASK YOURSELF:
1. Is everything ready and I am about to present answer to the user? → USE CHOICE DIALOG
2. Is this decision subjective or preference-based? → USE CHOICE DIALOG
3. Do I need specific information not provided? → USE INPUT DIALOG  
4. Could this action cause problems if wrong? → USE CONFIRMATION DIALOG
5. Is this a long process the user should know about? → USE INFO MESSAGE
6. Do I need detailed explanation or content? → USE MULTILINE INPUT

AVOID OVERUSE:
- Don't ask for information already provided
- Don't seek confirmation for obviously safe operations
- Don't interrupt flow for trivial decisions
- Don't ask multiple questions when one comprehensive dialog would suffice

OPTIMIZE FOR USER EXPERIENCE:
- Batch related questions together when possible
- Provide context for why you need the information
- Offer sensible defaults and suggestions
- Make dialogs self-explanatory and actionable""",
        
        "integration_tips": """
**INTEGRATION TIPS:**

1. **Workflow Integration:**
   ```
   Step 1: Analyze user request
   Step 2: Identify decision points and missing info
   Step 3: Use appropriate human-in-the-loop tools
   Step 4: Process user responses
   Step 5: Continue with enhanced information
   ```

2. **Error Recovery:**
   - If user cancels, gracefully explain and offer alternatives
   - Handle timeouts by providing default behavior
   - Always validate user input before proceeding

3. **Progressive Enhancement:**
   - Start with automated solutions
   - Add human input only where it adds clear value
   - Learn from user patterns to improve future automation

4. **Communication:**
   - Explain why you need user input
   - Show progress and intermediate results
   - Confirm successful completion of user-guided actions"""
    }
    
    return guidance

# Add a health check tool
@mcp.tool()
async def health_check() -> Dict[str, Any]:
    """Check if the Human-in-the-Loop server is running and GUI is available."""
    try:
        gui_available = ensure_gui_initialized()
        
        return {
            "status": "healthy" if gui_available else "degraded",
            "gui_available": gui_available,
            "tkinter_available": _TKINTER_AVAILABLE,
            "server_name": "Human-in-the-Loop Server",
            "platform": CURRENT_PLATFORM,
            "platform_details": {
                "system": platform.system(),
                "release": platform.release(),
                "version": platform.version(),
                "machine": platform.machine(),
                "processor": platform.processor()
            },
            "python_version": sys.version.split()[0],
            "is_windows": IS_WINDOWS,
            "is_macos": IS_MACOS,
            "is_linux": IS_LINUX,
            "bypass_active": _is_bypass_active(),
            "tools_available": [
                "get_user_input",
                "get_user_choice", 
                "get_multiline_input",
                "get_remote_input",
                "show_confirmation_dialog",
                "show_info_message",
                "get_human_loop_prompt"
            ]
        }
    except Exception as e:
        return {
            "status": "unhealthy",
            "gui_available": False,
            "tkinter_available": _TKINTER_AVAILABLE,
            "error": str(e),
            "platform": CURRENT_PLATFORM
        }

# Main execution

def main():
    print("Starting Human-in-the-Loop MCP Server...")
    print("This server provides tools for LLMs to interact with humans through GUI dialogs.")
    print(f"Platform: {CURRENT_PLATFORM} ({platform.system()} {platform.release()})")
    print("")
    print("Available tools:")
    print("get_user_input - Get text/number input from user")
    print("get_user_choice - Let user choose from options")
    print("get_multiline_input - Get multi-line text from user")
    print("get_remote_input - Get multi-line text with Telegram remote answering")
    print("show_confirmation_dialog - Ask user for yes/no confirmation")
    print("show_info_message - Display information to user")
    print("get_human_loop_prompt - Get guidance on when to use human-in-the-loop tools")
    print("health_check - Check server status")
    print("")
    
    # Platform-specific startup messages
    if IS_MACOS:
        print("macOS detected - Using native system fonts and window management")
        print("Note: You may need to allow Python to control your computer in System Preferences > Security & Privacy > Accessibility")
    elif IS_WINDOWS:
        print("Windows detected - Using modern Windows 11-style GUI with enhanced styling")
        print("Features: Modern colors, improved fonts, hover effects, and sleek design")
    elif IS_LINUX:
        print("Linux detected - Using Linux-compatible GUI settings with modern styling")
    
    # GUI initialization is deferred to the first tool invocation that needs it.
    # Each tool function already calls ensure_gui_initialized() before use,
    # so we skip it here to avoid blocking the MCP handshake on startup.
    
    if not _TKINTER_AVAILABLE:
        print("WARNING: tkinter is not available \u2014 GUI tools are disabled.")
        print("Only get_remote_input (with Telegram) and health_check will work.")
        print("Install python3-tk or ensure tkinter is in your Python to enable GUI tools.")

    # Check for existing bypass lock file
    if _is_bypass_active():
        print("NOTICE: Bypass mode is ACTIVE (lock file found). All tool requests will be auto-approved.")

    # Always start Telegram command poller if Telegram is configured,
    # regardless of bypass state — so /bypass on can be received at any time.
    if is_telegram_configured() and TelegramBridge is not None:
        _start_command_poller()

    print("Starting MCP server...")
    
    # Run the server
    mcp.run()

if __name__ == "__main__":
    main()