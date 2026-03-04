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
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import List, Optional
from urllib.parse import urlparse, parse_qs

from _miniapp_template import MINIAPP_HTML


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
        """Queue that receives the user's submitted answer string."""
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

                # Consume token and deliver answer
                with server_ref._lock:
                    server_ref._token_used = True

                server_ref._answer_queue.put(answer)
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

        self._httpd = HTTPServer(("127.0.0.1", 0), _Handler)
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
        if self._thread is not None:
            self._thread.join(timeout=3.0)
