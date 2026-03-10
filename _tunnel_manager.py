"""
Cloudflare Quick Tunnel manager for the Human-in-the-Loop MCP Server.

Spawns ``cloudflared tunnel --url localhost:<PORT>`` as a subprocess and
extracts the ephemeral ``https://*.trycloudflare.com`` public URL from
the process output.  No account or authentication is required for quick
tunnels.

Usage::

    from _tunnel_manager import CloudflareTunnel, TunnelNotAvailableError

    tunnel = CloudflareTunnel()
    try:
        public_url = tunnel.start(local_port=8742, timeout=15.0)
        print(public_url)  # https://xxxx.trycloudflare.com
    except TunnelNotAvailableError as exc:
        print(f"Tunnel unavailable: {exc}")
    finally:
        tunnel.stop()
"""

import re
import shutil
import subprocess
import sys
import threading
import time
import os
from typing import Callable, Optional


# Regex that matches the HTTPS trycloudflare URL emitted by cloudflared
_URL_PATTERN = re.compile(r'https://[a-z0-9-]+\.trycloudflare\.com')

# Common Windows install locations to probe when cloudflared is not on PATH
_WINDOWS_FALLBACK_PATHS = [
    r"C:\Program Files (x86)\cloudflared\cloudflared.exe",
    r"C:\Program Files\cloudflared\cloudflared.exe",
    r"C:\cloudflared\cloudflared.exe",
]


def _find_cloudflared() -> Optional[str]:
    """Return the path to the cloudflared binary, or None if not found."""
    # 1. Standard PATH lookup
    found = shutil.which("cloudflared")
    if found:
        return found

    # 2. Common Windows install locations (winget installs here but may not
    #    update the PATH of the already-running MCP server process)
    for candidate in _WINDOWS_FALLBACK_PATHS:
        if os.path.isfile(candidate):
            return candidate

    return None


class TunnelNotAvailableError(RuntimeError):
    """Raised when the Cloudflare tunnel cannot be established."""


class CloudflareTunnel:
    """Manages a single ``cloudflared`` quick-tunnel subprocess."""

    def __init__(self) -> None:
        self._proc: Optional[subprocess.Popen] = None
        self._url: Optional[str] = None
        self._ready_event = threading.Event()
        self._reader_thread: Optional[threading.Thread] = None
        self._monitor_thread: Optional[threading.Thread] = None
        self._health_thread: Optional[threading.Thread] = None
        self._stopped = False
        self._process_died_event = threading.Event()
        self._exit_code: Optional[int] = None
        self._on_tunnel_died: Optional[Callable[[int, Optional[int]], None]] = None
        self._local_port: Optional[int] = None
        self._log_callback: Optional[Callable[[str], None]] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self, local_port: int, timeout: float = 20.0) -> str:
        """Start the tunnel and return the public HTTPS URL.

        Parameters
        ----------
        local_port:
            The local TCP port the Mini App HTTP server is listening on.
        timeout:
            Maximum seconds to wait for the tunnel URL to appear in output.

        Returns
        -------
        str
            The public HTTPS URL, e.g. ``https://xxxx.trycloudflare.com``.

        Raises
        ------
        TunnelNotAvailableError
            If ``cloudflared`` is not on PATH, the process exits early, or the
            URL is not detected within *timeout* seconds.
        """
        binary = _find_cloudflared()
        if binary is None:
            raise TunnelNotAvailableError(
                "'cloudflared' executable not found on PATH or in common install locations. "
                "Install it with: winget install Cloudflare.cloudflared  "
                "(Windows) or download from https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/"
            )

        cmd = [binary, "tunnel", "--url", f"http://localhost:{local_port}"]
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,   # merge stderr → stdout
                text=True,
                bufsize=1,                  # line-buffered
            )
        except OSError as exc:
            raise TunnelNotAvailableError(f"Failed to spawn cloudflared: {exc}") from exc

        # Background reader thread parses the merged output stream
        self._reader_thread = threading.Thread(
            target=self._read_output,
            daemon=True,
            name="cloudflare-tunnel-reader",
        )
        self._reader_thread.start()

        # Wait for the URL to be found (or the process to die)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._ready_event.wait(timeout=0.25):
                break
            if self._proc.poll() is not None:
                raise TunnelNotAvailableError(
                    f"cloudflared exited prematurely (code={self._proc.returncode}) "
                    "before a tunnel URL was found."
                )

        if not self._url:
            self._terminate_proc()
            raise TunnelNotAvailableError(
                f"cloudflared did not produce a tunnel URL within {timeout:.0f} s. "
                "Check that you have internet access and that the cloudflared binary is "
                "up to date."
            )

        self._local_port = local_port
        self._log(
            f"[CloudflareTunnel] Tunnel established: {self._url} "
            f"-> localhost:{local_port} (PID {self._proc.pid})"
        )

        # Start a background monitor that detects cloudflared crashing after
        # the tunnel URL has been obtained.
        self._monitor_thread = threading.Thread(
            target=self._monitor_process,
            daemon=True,
            name="cloudflare-tunnel-monitor",
        )
        self._monitor_thread.start()

        # Start a periodic health check that verifies the public URL is
        # still reachable (catches silent tunnel disconnections).
        self._health_thread = threading.Thread(
            target=self._health_check_loop,
            daemon=True,
            name="cloudflare-tunnel-health",
        )
        self._health_thread.start()

        return self._url

    def reconnect(self, timeout: float = 20.0) -> str:
        """Tear down the current tunnel and start a fresh one on the same port.

        Returns the **new** public HTTPS URL.  The caller is responsible for
        updating any references that used the old URL (e.g. Telegram button,
        ``MiniAppHTTPServer._tunnel_base_url``).

        Raises ``TunnelNotAvailableError`` if the new tunnel cannot be
        established.
        """
        if self._local_port is None:
            raise TunnelNotAvailableError(
                "Cannot reconnect -- tunnel was never started (local_port is unknown)."
            )

        local_port = self._local_port
        old_url = self._url
        self._log(f"[CloudflareTunnel] Reconnecting tunnel (old URL: {old_url})...")

        # ------ clean up the old process ------
        # Temporarily mark as stopped so the monitor thread doesn't fire
        # the death callback during the intentional teardown.
        self._stopped = True
        self._terminate_proc()
        if self._reader_thread is not None:
            self._reader_thread.join(timeout=3.0)
            self._reader_thread = None
        if self._monitor_thread is not None:
            self._monitor_thread.join(timeout=2.0)
            self._monitor_thread = None
        if self._health_thread is not None:
            self._health_thread.join(timeout=2.0)
            self._health_thread = None

        # ------ reset internal state ------
        self._proc = None
        self._url = None
        self._ready_event = threading.Event()
        self._process_died_event = threading.Event()
        self._exit_code = None
        self._stopped = False

        # ------ start a new cloudflared process ------
        binary = _find_cloudflared()
        if binary is None:
            raise TunnelNotAvailableError(
                "'cloudflared' executable not found on PATH or in common install locations."
            )

        cmd = [binary, "tunnel", "--url", f"http://localhost:{local_port}"]
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            raise TunnelNotAvailableError(f"Failed to spawn cloudflared: {exc}") from exc

        self._reader_thread = threading.Thread(
            target=self._read_output,
            daemon=True,
            name="cloudflare-tunnel-reader",
        )
        self._reader_thread.start()

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._ready_event.wait(timeout=0.25):
                break
            if self._proc.poll() is not None:
                raise TunnelNotAvailableError(
                    f"cloudflared exited prematurely (code={self._proc.returncode}) "
                    "during reconnect."
                )

        if not self._url:
            self._terminate_proc()
            raise TunnelNotAvailableError(
                f"cloudflared reconnect did not produce a URL within {timeout:.0f} s."
            )

        self._local_port = local_port
        self._log(
            f"[CloudflareTunnel] Tunnel reconnected: {self._url} "
            f"-> localhost:{local_port} (PID {self._proc.pid})"
        )

        self._monitor_thread = threading.Thread(
            target=self._monitor_process,
            daemon=True,
            name="cloudflare-tunnel-monitor",
        )
        self._monitor_thread.start()

        self._health_thread = threading.Thread(
            target=self._health_check_loop,
            daemon=True,
            name="cloudflare-tunnel-health",
        )
        self._health_thread.start()

        return self._url

    @property
    def is_alive(self) -> bool:
        """True when the cloudflared process is still running."""
        if self._proc is None:
            return False
        return self._proc.poll() is None

    @property
    def process_died(self) -> threading.Event:
        """Event that is set when the cloudflared process exits unexpectedly."""
        return self._process_died_event

    @property
    def exit_code(self) -> Optional[int]:
        """The cloudflared process exit code, or None if still running."""
        return self._exit_code

    def set_on_tunnel_died(self, callback: Optional[Callable[[int, Optional[int]], None]]) -> None:
        """Register a callback invoked when cloudflared exits unexpectedly.

        The callback receives ``(pid, exit_code)``.
        """
        self._on_tunnel_died = callback

    def set_log_callback(self, callback: Optional[Callable[[str], None]]) -> None:
        """Register a callback for log messages.

        When set, all diagnostic messages (including forwarded cloudflared
        output) are routed through this callback instead of being printed.
        This allows callers to write them to a log file.
        """
        self._log_callback = callback

    def _log(self, msg: str) -> None:
        """Route a diagnostic message to the log callback (if set)."""
        cb = self._log_callback
        if cb is not None:
            try:
                cb(msg)
            except Exception:
                pass

    def _log_critical(self, msg: str) -> None:
        """Log a critical message to both the callback and stderr."""
        self._log(msg)
        try:
            print(msg, file=sys.stderr)
        except Exception:
            pass

    def stop(self) -> None:
        """Terminate the cloudflared subprocess and clean up."""
        if self._stopped:
            return
        self._stopped = True
        self._terminate_proc()
        if self._reader_thread is not None:
            self._reader_thread.join(timeout=3.0)
        if self._monitor_thread is not None:
            self._monitor_thread.join(timeout=2.0)
        if self._health_thread is not None:
            self._health_thread.join(timeout=2.0)

    @property
    def url(self) -> Optional[str]:
        """The public tunnel URL, or ``None`` if not yet established."""
        return self._url

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _read_output(self) -> None:
        """Read cloudflared stdout/stderr lines and extract the tunnel URL."""
        if self._proc is None or self._proc.stdout is None:
            return
        try:
            for line in self._proc.stdout:
                if self._stopped:
                    break
                line_stripped = line.rstrip()
                if line_stripped:
                    self._log(f"[CloudflareTunnel] {line_stripped}")
                match = _URL_PATTERN.search(line)
                if match and self._url is None:
                    self._url = match.group(0)
                    self._ready_event.set()
        except (OSError, ValueError):
            if not self._stopped:
                self._log("[CloudflareTunnel] Output stream closed unexpectedly.")
        finally:
            self._ready_event.set()  # unblock any waiting caller even on error

    def _monitor_process(self) -> None:
        """Background thread that detects when cloudflared exits unexpectedly."""
        if self._proc is None:
            return
        try:
            exit_code = self._proc.wait()  # blocks until process exits
        except Exception:
            exit_code = None

        if self._stopped:
            # Intentional shutdown — don't fire the death callback.
            return

        self._exit_code = exit_code
        self._process_died_event.set()
        pid = self._proc.pid if self._proc else 0
        self._log_critical(
            f"[CloudflareTunnel] cloudflared process (PID {pid}) exited "
            f"unexpectedly with code {exit_code}."
        )
        cb = self._on_tunnel_died
        if cb is not None:
            try:
                cb(pid, exit_code)
            except Exception as exc:
                self._log(f"[CloudflareTunnel] on_tunnel_died callback error: {exc}")

    def _health_check_loop(self) -> None:
        """Periodically verify that the tunnel URL is reachable."""
        try:
            import requests as _req
        except ImportError:
            self._log("[CloudflareTunnel] 'requests' not available -- tunnel health checks disabled.")
            return

        # Wait a short period after startup before beginning health checks.
        _initial_delay = 10.0
        _check_interval = 30.0

        start = time.monotonic()
        while not self._stopped:
            elapsed = time.monotonic() - start
            if elapsed < _initial_delay:
                # Sleep in small increments so we can exit quickly on stop.
                if self._process_died_event.wait(timeout=1.0):
                    break
                continue

            if self._process_died_event.is_set() or self._stopped:
                break

            url = self._url
            if not url:
                break

            try:
                resp = _req.get(url, timeout=10, allow_redirects=False)
                if resp.status_code in (502, 504):
                    self._log(
                        f"[CloudflareTunnel] Health check WARN: "
                        f"GET {url} returned HTTP {resp.status_code}."
                    )
                elif resp.status_code == 404 and "Not Found" in resp.text and len(resp.text) < 50:
                    # Cloudflare's plain "Not Found" for dead quick tunnels.
                    self._log_critical(
                        f"[CloudflareTunnel] Health check FAIL: "
                        f"GET {url} returned Cloudflare 'Not Found' -- "
                        f"tunnel may be disconnected."
                    )
            except _req.ConnectionError:
                self._log(
                    f"[CloudflareTunnel] Health check FAIL: "
                    f"Connection error reaching {url}."
                )
            except _req.Timeout:
                self._log(
                    f"[CloudflareTunnel] Health check WARN: "
                    f"GET {url} timed out (10 s)."
                )
            except Exception as exc:
                self._log(
                    f"[CloudflareTunnel] Health check error: "
                    f"{type(exc).__name__}: {exc}"
                )

            # Wait for next check interval (interruptible).
            if self._process_died_event.wait(timeout=_check_interval):
                break

    def _terminate_proc(self) -> None:
        """Terminate the cloudflared process gracefully, then forcefully."""
        if self._proc is None:
            return
        try:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait(timeout=2.0)
        except OSError as exc:
            # ESRCH / errno 3 = "No such process" — process already exited; ignore.
            import errno
            if exc.errno not in (errno.ESRCH, None):
                self._log(f"[CloudflareTunnel] Non-fatal error terminating cloudflared process: {exc}")
