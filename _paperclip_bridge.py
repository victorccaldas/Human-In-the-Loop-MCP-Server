"""
Paperclip Bridge — Webhook receiver and API client for Paperclip heartbeat integration.

This module provides a third input channel for ``get_remote_input`` alongside
tkinter dialogs and Telegram.  When Paperclip fires a heartbeat for a registered
agent session, the webhook injects the task context as the answer to the pending
prompt, effectively "waking" the idle agent.

All functionality is optional and crash-isolated — failures here never affect the
core MCP server or existing dialog/Telegram paths.
"""

from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, Optional

import requests

from _hitl_logs import append_log_line

# ---------------------------------------------------------------------------
# Logging helper
# ---------------------------------------------------------------------------

_LOG_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "logs", "paperclip_bridge.log"
)


def _log(msg: str) -> None:
    append_log_line(_LOG_FILE, msg)
    try:
        safe = msg.encode("ascii", "replace").decode("ascii")
        print(f"[PaperclipBridge] {safe}")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_CONFIG_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "paperclip_config.json"
)


def load_paperclip_config() -> Optional[Dict[str, Any]]:
    """Load paperclip_config.json; returns *None* if absent or disabled."""
    if not os.path.isfile(_CONFIG_FILE):
        return None
    try:
        with open(_CONFIG_FILE, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
        if not cfg.get("enabled", False):
            return None
        return cfg
    except Exception as exc:
        _log(f"Failed to load config: {exc}")
        return None


def is_paperclip_configured() -> bool:
    return load_paperclip_config() is not None


# ---------------------------------------------------------------------------
# Pending-prompt registry (thread-safe)
# ---------------------------------------------------------------------------

_registry_lock = threading.Lock()

# Maps paperclip_agent_id → session info dict.
# Each entry contains the claim function and metadata for one active
# ``create_remote_input_dialog`` call.
_pending_sessions: Dict[str, Dict[str, Any]] = {}


def register_prompt(
    paperclip_agent_id: str,
    claim_fn: Callable[[str, Optional[str]], bool],
    master_done: threading.Event,
    cancel_events: Dict[str, threading.Event],
    meta: Optional[Dict[str, Any]] = None,
) -> None:
    """Register a pending prompt so Paperclip heartbeats can inject answers.

    Parameters
    ----------
    paperclip_agent_id:
        The Paperclip agent ID associated with this MCP session.
    claim_fn:
        The ``_claim_completion(source, text) -> bool`` closure from
        ``create_remote_input_dialog``.
    master_done:
        The ``master_done`` threading.Event from the dialog.
    cancel_events:
        Dict of events to set when Paperclip wins (e.g. ``tg_cancel``).
    meta:
        Optional dict with extra info (title, prompt, name_or_role, …).
    """
    with _registry_lock:
        _pending_sessions[paperclip_agent_id] = {
            "claim_fn": claim_fn,
            "master_done": master_done,
            "cancel_events": cancel_events,
            "meta": meta or {},
            "registered_at": time.time(),
        }
    _log(f"Prompt registered for agent {paperclip_agent_id}")


def unregister_prompt(paperclip_agent_id: str) -> Optional[Dict[str, Any]]:
    """Remove a prompt registration.  Returns the removed entry or None."""
    with _registry_lock:
        entry = _pending_sessions.pop(paperclip_agent_id, None)
    if entry:
        _log(f"Prompt unregistered for agent {paperclip_agent_id}")
    return entry


def get_pending_prompt(paperclip_agent_id: str) -> Optional[Dict[str, Any]]:
    """Look up a pending prompt by Paperclip agent ID (read-only)."""
    with _registry_lock:
        return _pending_sessions.get(paperclip_agent_id)


def list_pending_prompts() -> Dict[str, Dict[str, Any]]:
    """Return a snapshot of all pending prompts (for status endpoint)."""
    with _registry_lock:
        result = {}
        for agent_id, entry in _pending_sessions.items():
            result[agent_id] = {
                "registered_at": entry.get("registered_at"),
                "meta": entry.get("meta", {}),
                "is_done": entry["master_done"].is_set(),
            }
        return result


# ---------------------------------------------------------------------------
# Paperclip API Client
# ---------------------------------------------------------------------------

class PaperclipAPIClient:
    """Thin wrapper around the Paperclip REST API."""

    def __init__(self, api_url: str, api_key: str = ""):
        self._base = api_url.rstrip("/")
        self._api_key = api_key

    def _headers(self, run_id: str = "") -> Dict[str, str]:
        h: Dict[str, str] = {"Content-Type": "application/json"}
        if self._api_key:
            h["Authorization"] = f"Bearer {self._api_key}"
        if run_id:
            h["X-Paperclip-Run-Id"] = run_id
        return h

    # -- Agent ---
    def get_agent_me(self, api_key: str = "") -> Optional[Dict[str, Any]]:
        key = api_key or self._api_key
        try:
            r = requests.get(
                f"{self._base}/agents/me",
                headers={"Authorization": f"Bearer {key}"},
                timeout=10,
            )
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            _log(f"get_agent_me failed: {exc}")
            return None

    # -- Issues ---
    def get_assigned_issues(
        self, company_id: str, agent_id: str, statuses: str = "todo,in_progress,blocked"
    ) -> list:
        try:
            r = requests.get(
                f"{self._base}/companies/{company_id}/issues",
                params={"assigneeAgentId": agent_id, "status": statuses},
                headers=self._headers(),
                timeout=10,
            )
            r.raise_for_status()
            return r.json() if isinstance(r.json(), list) else r.json().get("items", [])
        except Exception as exc:
            _log(f"get_assigned_issues failed: {exc}")
            return []

    def get_issue(self, issue_id: str) -> Optional[Dict[str, Any]]:
        try:
            r = requests.get(
                f"{self._base}/issues/{issue_id}",
                headers=self._headers(),
                timeout=10,
            )
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            _log(f"get_issue({issue_id}) failed: {exc}")
            return None

    def get_issue_comments(self, issue_id: str) -> list:
        try:
            r = requests.get(
                f"{self._base}/issues/{issue_id}/comments",
                headers=self._headers(),
                timeout=10,
            )
            r.raise_for_status()
            data = r.json()
            return data if isinstance(data, list) else data.get("items", [])
        except Exception as exc:
            _log(f"get_issue_comments({issue_id}) failed: {exc}")
            return []

    def post_comment(self, issue_id: str, body: str, run_id: str = "") -> bool:
        try:
            r = requests.post(
                f"{self._base}/issues/{issue_id}/comments",
                json={"body": body},
                headers=self._headers(run_id),
                timeout=10,
            )
            r.raise_for_status()
            return True
        except Exception as exc:
            _log(f"post_comment({issue_id}) failed: {exc}")
            return False

    def update_issue(
        self, issue_id: str, status: str = "", comment: str = "", run_id: str = ""
    ) -> bool:
        payload: Dict[str, str] = {}
        if status:
            payload["status"] = status
        if comment:
            payload["comment"] = comment
        if not payload:
            return False
        try:
            r = requests.patch(
                f"{self._base}/issues/{issue_id}",
                json=payload,
                headers=self._headers(run_id),
                timeout=10,
            )
            r.raise_for_status()
            return True
        except Exception as exc:
            _log(f"update_issue({issue_id}) failed: {exc}")
            return False


# ---------------------------------------------------------------------------
# Heartbeat processing
# ---------------------------------------------------------------------------

def _format_heartbeat_response(
    heartbeat: Dict[str, Any],
    issues: list,
    issue_detail: Optional[Dict[str, Any]] = None,
    comments: Optional[list] = None,
) -> str:
    """Build the text that gets injected as the get_remote_input answer."""
    lines: list[str] = []
    wake = heartbeat.get("wakeReason", heartbeat.get("context", {}).get("wakeReason", "unknown"))
    run_id = heartbeat.get("runId", "?")
    agent_id = heartbeat.get("agentId", "?")
    task_id = heartbeat.get("context", {}).get("taskId") or heartbeat.get("taskId")
    comment_id = heartbeat.get("context", {}).get("commentId") or heartbeat.get("commentId")

    lines.append("=" * 60)
    lines.append("[PAPERCLIP HEARTBEAT — INJECTED INSTRUCTIONS]")
    lines.append("=" * 60)
    lines.append(f"Wake Reason : {wake}")
    lines.append(f"Agent ID    : {agent_id}")
    lines.append(f"Run ID      : {run_id}")
    if task_id:
        lines.append(f"Task ID     : {task_id}")
    if comment_id:
        lines.append(f"Comment ID  : {comment_id}")
    lines.append("-" * 60)

    if issue_detail:
        lines.append(f"Task        : {issue_detail.get('title', '(no title)')}")
        lines.append(f"Status      : {issue_detail.get('status', '?')}")
        lines.append(f"Priority    : {issue_detail.get('priority', '?')}")
        desc = issue_detail.get("description", "")
        if desc:
            lines.append(f"Description : {desc[:500]}")
        lines.append("-" * 60)

    if comments:
        lines.append("Recent comments:")
        for c in comments[-5:]:
            actor = c.get("actor", c.get("agentName", "?"))
            body = c.get("body", "")[:200]
            lines.append(f"  [{actor}]: {body}")
        lines.append("-" * 60)

    if issues and not issue_detail:
        lines.append("Assigned issues:")
        for iss in issues[:10]:
            lines.append(
                f"  [{iss.get('status','?')}] {iss.get('title','?')} "
                f"(priority={iss.get('priority','?')}, id={iss.get('id','?')})"
            )
        lines.append("-" * 60)

    lines.append(
        "The above data was injected by Paperclip. "
        "Execute the task as described, report back with results, "
        "and follow the heartbeat protocol."
    )
    lines.append("=" * 60)
    return "\n".join(lines)


def process_heartbeat(
    heartbeat: Dict[str, Any],
    api_client: PaperclipAPIClient,
    priority_mode: str = "first_wins",
) -> Dict[str, Any]:
    """Process an incoming Paperclip heartbeat.

    Returns a dict with ``{success, message, agent_id, ...}``.
    """
    agent_id = heartbeat.get("agentId", "")
    run_id = heartbeat.get("runId", "")
    company_id = heartbeat.get("companyId", "")
    context = heartbeat.get("context", {})
    task_id = context.get("taskId") or heartbeat.get("taskId", "")
    wake_reason = context.get("wakeReason") or heartbeat.get("wakeReason", "unknown")

    _log(
        f"Heartbeat received: agent={agent_id}, run={run_id}, "
        f"wake={wake_reason}, task={task_id}"
    )

    # Look up pending prompt for this agent
    entry = get_pending_prompt(agent_id)
    if entry is None:
        _log(f"No pending prompt for agent {agent_id}")
        return {
            "success": False,
            "message": f"No pending prompt for agent {agent_id}",
            "agent_id": agent_id,
        }

    if entry["master_done"].is_set():
        _log(f"Prompt for agent {agent_id} already completed")
        return {
            "success": False,
            "message": f"Prompt for agent {agent_id} already completed",
            "agent_id": agent_id,
        }

    # Fetch task context from Paperclip API
    issues: list = []
    issue_detail: Optional[Dict[str, Any]] = None
    comments: Optional[list] = None

    if task_id:
        issue_detail = api_client.get_issue(task_id)
        comments = api_client.get_issue_comments(task_id)
    elif company_id and agent_id:
        issues = api_client.get_assigned_issues(company_id, agent_id)

    # Build the injected response text
    response_text = _format_heartbeat_response(
        heartbeat, issues, issue_detail, comments
    )

    # Attempt to claim the pending prompt
    claim_fn = entry["claim_fn"]
    claimed = claim_fn("paperclip_heartbeat", response_text)

    if claimed:
        _log(f"Heartbeat successfully claimed prompt for agent {agent_id}")
        # Signal Telegram/other pollers to stop
        for evt in entry.get("cancel_events", {}).values():
            if isinstance(evt, threading.Event):
                evt.set()
        return {
            "success": True,
            "message": "Heartbeat injected into pending prompt",
            "agent_id": agent_id,
            "run_id": run_id,
            "wake_reason": wake_reason,
            "response_length": len(response_text),
        }
    else:
        _log(f"Failed to claim prompt for agent {agent_id} (already answered)")
        return {
            "success": False,
            "message": "Prompt was already answered by another channel",
            "agent_id": agent_id,
        }


# ---------------------------------------------------------------------------
# Reverse path: report human answers to Paperclip
# ---------------------------------------------------------------------------

def report_human_answer(
    paperclip_agent_id: str,
    human_text: str,
    source: str,
    api_client: PaperclipAPIClient,
    task_id: str = "",
    run_id: str = "",
) -> bool:
    """Post a comment to the active Paperclip issue indicating the human intervened."""
    if not task_id:
        _log(f"No task_id for reverse-path report (agent={paperclip_agent_id})")
        return False

    truncated = human_text[:1000] if human_text else "(empty)"
    body = (
        f"## Human-Intercepted Input\n\n"
        f"**Source:** {source}\n\n"
        f"**Response:**\n```\n{truncated}\n```\n\n"
        f"_This response was provided by a human operator via the "
        f"Human-in-the-Loop MCP Server ({source} channel)._"
    )
    return api_client.post_comment(task_id, body, run_id)


# ---------------------------------------------------------------------------
# Webhook HTTP handler
# ---------------------------------------------------------------------------

class _PaperclipWebhookHandler(BaseHTTPRequestHandler):
    """Handles POST /heartbeat and GET /status requests from Paperclip."""

    # Injected by PaperclipBridge at server creation time
    bridge: "PaperclipBridge"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        _log(f"HTTP {format % args}")

    def _send_json(self, code: int, data: Dict[str, Any]) -> None:
        body = json.dumps(data).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> Optional[Dict[str, Any]]:
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length <= 0:
            return None
        raw = self.rfile.read(content_length)
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None

    # --- POST ---
    def do_POST(self) -> None:  # noqa: N802
        path = self.path.rstrip("/")
        if path == "/heartbeat":
            self._handle_heartbeat()
        else:
            self._send_json(404, {"error": f"Unknown endpoint: {path}"})

    def _handle_heartbeat(self) -> None:
        body = self._read_json_body()
        if body is None:
            self._send_json(400, {"error": "Missing or invalid JSON body"})
            return

        result = process_heartbeat(
            body,
            self.bridge.api_client,
            self.bridge.priority_mode,
        )
        code = 200 if result.get("success") else 409
        self._send_json(code, result)

    # --- GET ---
    def do_GET(self) -> None:  # noqa: N802
        path = self.path.rstrip("/")
        if path == "/status":
            self._handle_status()
        elif path == "/health":
            self._send_json(200, {"status": "ok", "bridge": "paperclip"})
        else:
            self._send_json(404, {"error": f"Unknown endpoint: {path}"})

    def _handle_status(self) -> None:
        pending = list_pending_prompts()
        self._send_json(200, {
            "status": "ok",
            "pending_prompts": pending,
            "total_pending": len(pending),
            "webhook_port": self.bridge.port,
            "priority_mode": self.bridge.priority_mode,
            "reverse_path": self.bridge.reverse_path_enabled,
        })


# ---------------------------------------------------------------------------
# PaperclipBridge — main orchestrator
# ---------------------------------------------------------------------------

class PaperclipBridge:
    """Manages the Paperclip webhook server and API client.

    Start this from the MCP server on a daemon thread; it listens for incoming
    heartbeats and injects answers into pending ``get_remote_input`` prompts.
    """

    def __init__(self, config: Dict[str, Any]):
        self.port: int = config.get("webhook_port", 8765)
        self.priority_mode: str = config.get("priority_mode", "first_wins")
        self.reverse_path_enabled: bool = config.get("reverse_path", True)

        api_url = config.get("paperclip_api_url", "http://localhost:3100/api")
        api_key = config.get("agent_api_key", "")
        self.api_client = PaperclipAPIClient(api_url, api_key)

        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._started = threading.Event()

    def start(self) -> None:
        """Start the webhook HTTP server in a daemon thread."""
        if self._thread is not None and self._thread.is_alive():
            _log("Bridge already running")
            return

        def _run() -> None:
            try:
                handler = type(
                    "_Handler",
                    (_PaperclipWebhookHandler,),
                    {"bridge": self},
                )
                self._server = ThreadingHTTPServer(("127.0.0.1", self.port), handler)
                self._started.set()
                _log(f"Webhook server listening on 127.0.0.1:{self.port}")
                self._server.serve_forever()
            except Exception as exc:
                _log(f"Webhook server failed: {exc}")
                self._started.set()  # unblock waiters even on failure

        self._thread = threading.Thread(target=_run, daemon=True, name="paperclip-bridge")
        self._thread.start()
        self._started.wait(timeout=5)

    def stop(self) -> None:
        """Shut down the webhook server."""
        if self._server:
            try:
                self._server.shutdown()
            except Exception:
                pass
        self._server = None
        _log("Bridge stopped")

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_bridge_instance: Optional[PaperclipBridge] = None
_bridge_lock = threading.Lock()


def get_bridge() -> Optional[PaperclipBridge]:
    """Return the running PaperclipBridge singleton, or None."""
    return _bridge_instance


def ensure_paperclip_bridge() -> Optional[PaperclipBridge]:
    """Load config and start the bridge if configured.  Thread-safe singleton."""
    global _bridge_instance
    with _bridge_lock:
        if _bridge_instance is not None:
            return _bridge_instance
        cfg = load_paperclip_config()
        if cfg is None:
            return None
        bridge = PaperclipBridge(cfg)
        try:
            bridge.start()
            _bridge_instance = bridge
            _log(
                f"Paperclip bridge started (port={bridge.port}, "
                f"priority={bridge.priority_mode}, reverse={bridge.reverse_path_enabled})"
            )
            return bridge
        except Exception as exc:
            _log(f"Failed to start bridge: {exc}")
            return None
