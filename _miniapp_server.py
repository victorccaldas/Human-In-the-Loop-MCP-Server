"""
Mini App HTTP server for the Human-in-the-Loop MCP Server.

Serves the Telegram Mini App HTML page on localhost and receives the user's
submitted answer via a POST /submit endpoint.

The server binds to ``127.0.0.1:0`` (OS-assigned port) and runs in a
background daemon thread so it doesn't block the MCP server loop.

Usage::

    from _miniapp_server import MiniAppHTTPServer

    server = MiniAppHTTPServer(
        title="Review needed",
        prompt="Please review this code and share feedback.",
        active_prompts=["LGTM", "Needs changes"],
        token="deadbeef1234",
        tunnel_base_url="https://xxxx.trycloudflare.com",
    )
    port = server.start()
    # ...
    answer = server.answer_queue.get()   # blocks until user submits
    server.stop()
"""

import json
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from typing import List, Optional
from urllib.parse import urlparse, parse_qs

from _miniapp_template import MINIAPP_HTML


class _ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """HTTPServer that handles each request in a new daemon thread."""
    daemon_threads = True


class MiniAppHTTPServer:
    """Threaded HTTP server that serves the Mini App page and collects answers."""

    def __init__(
        self,
        title: str,
        prompt: str,
        prompts: List[dict],
        token: str,
        tunnel_base_url: str,
        name_or_role: str = "",
    ) -> None:
        """
        Parameters
        ----------
        title:
            Dialog title shown at the top of the Mini App.
        prompt:
            Full prompt text displayed in the read-only block.
        prompts:
            List of ``{"text": str, "checked": bool}`` dicts — all custom
            prompts from the CSV; active ones are pre-checked by default.
        token:
            Single-use HMAC-less random hex token; validated on GET and POST.
        tunnel_base_url:
            Public HTTPS base URL (e.g. ``https://xxxx.trycloudflare.com``).
            Used to build the ``submitUrl`` injected into the Mini App JS.
        name_or_role:
            Optional agent identifier shown as a subtitle in the Mini App.
        """
        self._title = title
        self._prompt = prompt
        self._prompts = prompts
        self._token = token
        self._tunnel_base_url = tunnel_base_url.rstrip("/")
        self._name_or_role = name_or_role

        self._answer_queue: queue.SimpleQueue = queue.SimpleQueue()
        self._token_used = False
        self._lock = threading.Lock()

        self._httpd: Optional[HTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._port: int = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def answer_queue(self) -> queue.SimpleQueue:
        """Queue that receives the user's submitted answer payload."""
        return self._answer_queue

    @property
    def port(self) -> int:
        """The local TCP port the server is bound to (0 until started)."""
        return self._port

    def start(self) -> int:
        """Start the HTTP server on a free OS-assigned port.

        Returns the bound port number.
        """
        server_ref = self  # closure reference

        class _Handler(BaseHTTPRequestHandler):
            """Request handler — one instance per request."""

            def do_GET(self):
                try:
                    self._do_GET_impl()
                except Exception as exc:
                    print(f"[MiniAppServer] Unhandled error in GET {self.path}: "
                          f"{type(exc).__name__}: {exc}")
                    try:
                        self._respond(500, "text/plain", b"Internal server error")
                    except Exception:
                        pass

            def _do_GET_impl(self):
                parsed = urlparse(self.path)
                if parsed.path != "/":
                    self._respond(404, "text/plain", b"Not found")
                    return

                # Validate token
                params = parse_qs(parsed.query)
                request_token = params.get("t", [""])[0]
                if request_token != server_ref._token:
                    self._respond(403, "text/plain", b"Forbidden")
                    return

                with server_ref._lock:
                    if server_ref._token_used:
                        self._respond(
                            410,
                            "text/plain",
                            b"This dialog has already been answered.",
                        )
                        return

                # Build the SESSION JSON to inject into the template
                submit_url = f"{server_ref._tunnel_base_url}/submit"
                session_data = {
                    "token": server_ref._token,
                    "title": server_ref._title,
                    "prompt": server_ref._prompt,
                    "prompts": server_ref._prompts,
                    "agentRole": server_ref._name_or_role,
                    "submitUrl": submit_url,
                }
                session_json = json.dumps(session_data, ensure_ascii=False)
                html = MINIAPP_HTML.replace("__SESSION_JSON__", session_json)
                body = html.encode("utf-8")
                self._respond(200, "text/html; charset=utf-8", body)

            def do_POST(self):
                try:
                    self._do_POST_impl()
                except Exception as exc:
                    print(f"[MiniAppServer] Unhandled error in POST {self.path}: "
                          f"{type(exc).__name__}: {exc}")
                    try:
                        self._respond(500, "text/plain", b"Internal server error")
                    except Exception:
                        pass

            def _do_POST_impl(self):
                parsed = urlparse(self.path)
                if parsed.path != "/submit":
                    self._respond(404, "text/plain", b"Not found")
                    return

                # CORS pre-flight handled by do_OPTIONS
                request_token = self.headers.get("X-Token", "")
                if request_token != server_ref._token:
                    self._respond(403, "text/plain", b"Forbidden")
                    return

                with server_ref._lock:
                    if server_ref._token_used:
                        self._respond(
                            409,
                            "application/json",
                            b'{"ok":false,"error":"already answered"}',
                        )
                        return

                # Read body
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length) if length else b""
                try:
                    data = json.loads(raw)
                    answer = data["answer"]
                    if not isinstance(answer, str):
                        raise ValueError("answer must be a string")
                except (json.JSONDecodeError, KeyError, ValueError) as exc:
                    self._respond(
                        400,
                        "application/json",
                        json.dumps({"ok": False, "error": str(exc)}).encode(),
                    )
                    return

                # Consume the token before enqueueing the answer so duplicate
                # Mini App submits cannot overtake the first accepted payload.
                with server_ref._lock:
                    server_ref._token_used = True

                server_ref._answer_queue.put(
                    {
                        "text": answer,
                        "source": "telegram_miniapp",
                        "received_at": time.monotonic_ns(),
                    }
                )
                self._respond(200, "application/json", b'{"ok":true}')

            def do_OPTIONS(self):
                """CORS pre-flight for the Mini App fetch from Telegram WebView."""
                self.send_response(204)
                self._add_cors_headers()
                self.end_headers()

            # ── Helpers ──────────────────────────────────────────────

            def _respond(self, code: int, content_type: str, body: bytes) -> None:
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self._add_cors_headers()
                self.end_headers()
                self.wfile.write(body)

            def _add_cors_headers(self) -> None:
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header(
                    "Access-Control-Allow-Headers", "Content-Type, X-Token"
                )

            def log_message(self, format, *args):  # noqa: A002
                pass  # suppress default stdout logging

        self._httpd = _ThreadedHTTPServer(("127.0.0.1", 0), _Handler)
        self._port = self._httpd.server_address[1]

        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            daemon=True,
            name="miniapp-http-server",
        )
        self._thread.start()
        return self._port

    def stop(self) -> None:
        """Shut down the HTTP server."""
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
            except Exception as exc:
                print(f"[MiniAppServer] Warning: HTTP server shutdown error "
                      f"(port {self._port}): {exc}")
            finally:
                try:
                    self._httpd.server_close()
                except Exception as exc:
                    print(f"[MiniAppServer] Warning: HTTP server close error "
                          f"(port {self._port}): {exc}")
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            if self._thread.is_alive():
                print(f"[MiniAppServer] Warning: HTTP server thread did not stop "
                      f"cleanly on port {self._port}.")
        self._httpd = None
        self._thread = None


class PersistentMiniAppServer:
    """Multi-session HTTP server that handles concurrent MiniApp sessions.

    Unlike :class:`MiniAppHTTPServer` (one session per server), this server
    supports multiple concurrent sessions identified by unique tokens.  A
    single instance can be reused across multiple ``get_remote_input``
    invocations, eliminating the need to create a new Cloudflare tunnel
    for each call.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, dict] = {}  # token -> session data
        self._answer_queues: dict[str, "queue.SimpleQueue"] = {}
        self._tokens_used: dict[str, bool] = {}
        self._tunnel_base_url: str = ""
        self._lock = threading.Lock()
        self._httpd: Optional[HTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._port: int = 0

    @property
    def port(self) -> int:
        return self._port

    def register_session(
        self,
        token: str,
        title: str,
        prompt: str,
        prompts: list,
        name_or_role: str = "",
    ) -> "queue.SimpleQueue":
        """Register a new session and return its answer queue."""
        q: queue.SimpleQueue = queue.SimpleQueue()
        with self._lock:
            self._sessions[token] = {
                "title": title,
                "prompt": prompt,
                "prompts": prompts,
                "name_or_role": name_or_role,
            }
            self._answer_queues[token] = q
            self._tokens_used[token] = False
        return q

    def unregister_session(self, token: str) -> None:
        """Remove a session (cleanup after dialog closes)."""
        with self._lock:
            self._sessions.pop(token, None)
            self._answer_queues.pop(token, None)
            self._tokens_used.pop(token, None)

    def update_tunnel_url(self, new_url: str) -> None:
        """Update the tunnel base URL (called after tunnel reconnection)."""
        with self._lock:
            self._tunnel_base_url = new_url.rstrip("/")

    def start(self, tunnel_base_url: str = "") -> int:
        """Start the HTTP server. Returns the bound port number."""
        if self._httpd is not None:
            return self._port

        self._tunnel_base_url = tunnel_base_url.rstrip("/")
        server_ref = self

        class _Handler(BaseHTTPRequestHandler):

            def do_GET(self):
                try:
                    self._do_GET_impl()
                except Exception as exc:
                    print(f"[PersistentMiniApp] Unhandled GET error: {type(exc).__name__}: {exc}")
                    try:
                        self._respond(500, "text/plain", b"Internal server error")
                    except Exception:
                        pass

            def _do_GET_impl(self):
                parsed = urlparse(self.path)
                if parsed.path != "/":
                    self._respond(404, "text/plain", b"Not found")
                    return

                params = parse_qs(parsed.query)
                request_token = params.get("t", [""])[0]

                with server_ref._lock:
                    session = server_ref._sessions.get(request_token)
                    if session is None:
                        self._respond(403, "text/plain", b"Forbidden")
                        return
                    if server_ref._tokens_used.get(request_token, False):
                        self._respond(410, "text/plain", b"This dialog has already been answered.")
                        return
                    tunnel_url = server_ref._tunnel_base_url

                submit_url = f"{tunnel_url}/submit"
                session_data = {
                    "token": request_token,
                    "title": session["title"],
                    "prompt": session["prompt"],
                    "prompts": session["prompts"],
                    "agentRole": session["name_or_role"],
                    "submitUrl": submit_url,
                }
                session_json = json.dumps(session_data, ensure_ascii=False)
                html = MINIAPP_HTML.replace("__SESSION_JSON__", session_json)
                body = html.encode("utf-8")
                self._respond(200, "text/html; charset=utf-8", body)

            def do_POST(self):
                try:
                    self._do_POST_impl()
                except Exception as exc:
                    print(f"[PersistentMiniApp] Unhandled POST error: {type(exc).__name__}: {exc}")
                    try:
                        self._respond(500, "text/plain", b"Internal server error")
                    except Exception:
                        pass

            def _do_POST_impl(self):
                parsed = urlparse(self.path)
                if parsed.path != "/submit":
                    self._respond(404, "text/plain", b"Not found")
                    return

                request_token = self.headers.get("X-Token", "")

                with server_ref._lock:
                    if request_token not in server_ref._sessions:
                        self._respond(403, "text/plain", b"Forbidden")
                        return
                    if server_ref._tokens_used.get(request_token, False):
                        self._respond(409, "application/json", b'{"ok":false,"error":"already answered"}')
                        return

                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length) if length else b""
                try:
                    data = json.loads(raw)
                    answer = data["answer"]
                    if not isinstance(answer, str):
                        raise ValueError("answer must be a string")
                except (json.JSONDecodeError, KeyError, ValueError) as exc:
                    self._respond(400, "application/json", json.dumps({"ok": False, "error": str(exc)}).encode())
                    return

                with server_ref._lock:
                    server_ref._tokens_used[request_token] = True
                    q = server_ref._answer_queues.get(request_token)

                if q is not None:
                    q.put({
                        "text": answer,
                        "source": "telegram_miniapp",
                        "received_at": time.monotonic_ns(),
                    })
                self._respond(200, "application/json", b'{"ok":true}')

            def do_OPTIONS(self):
                self.send_response(204)
                self._add_cors_headers()
                self.end_headers()

            def _respond(self, code: int, content_type: str, body: bytes) -> None:
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self._add_cors_headers()
                self.end_headers()
                self.wfile.write(body)

            def _add_cors_headers(self) -> None:
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Token")

            def log_message(self, format, *args):  # noqa: A002
                pass

        self._httpd = _ThreadedHTTPServer(("127.0.0.1", 0), _Handler)
        self._port = self._httpd.server_address[1]
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            daemon=True,
            name="persistent-miniapp-http-server",
        )
        self._thread.start()
        return self._port

    def stop(self) -> None:
        """Shut down the persistent HTTP server."""
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
            except Exception:
                pass
            finally:
                try:
                    self._httpd.server_close()
                except Exception:
                    pass
        if self._thread is not None:
            self._thread.join(timeout=3.0)
        self._httpd = None
        self._thread = None
