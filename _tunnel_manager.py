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
import threading
import time
import os
from typing import Optional


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
        self._stopped = False

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

        return self._url

    def stop(self) -> None:
        """Terminate the cloudflared subprocess and clean up."""
        if self._stopped:
            return
        self._stopped = True
        self._terminate_proc()
        if self._reader_thread is not None:
            self._reader_thread.join(timeout=3.0)

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
                match = _URL_PATTERN.search(line)
                if match and self._url is None:
                    self._url = match.group(0)
                    self._ready_event.set()
        except (OSError, ValueError):
            pass  # Process closed
        finally:
            self._ready_event.set()  # unblock any waiting caller even on error

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
        except OSError:
            pass
