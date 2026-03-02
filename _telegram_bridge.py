"""
Telegram Bridge — lightweight Bot API wrapper for the remote input feature.

Provides methods to:
  - Send a prompt message to a personal Telegram chat
  - Long-poll for a reply to that specific message
  - Edit the message after the response arrives (status update)

Used by ``get_remote_input`` in ``human_loop_server.py`` to enable answering
Human-in-the-Loop prompts remotely via Telegram.

Configuration lives in ``telegram_config.json`` (same directory as this file):
  { "bot_token": "<BotFather token>", "chat_id": "<personal chat id>" }
"""

import json
import os
import time
import threading
from typing import Optional

try:
    import requests
except ImportError:
    requests = None  # Will raise when TelegramBridge is instantiated

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_CONFIG = os.path.join(_SCRIPT_DIR, "telegram_config.json")


class TelegramBridge:
    """Lightweight Telegram Bot API client for prompting and polling replies."""

    def __init__(self, config_path: Optional[str] = None):
        if requests is None:
            raise ImportError(
                "The 'requests' library is required for Telegram integration. "
                "Install it with: pip install requests"
            )

        config_path = config_path or _DEFAULT_CONFIG
        if not os.path.isfile(config_path):
            raise FileNotFoundError(
                f"Telegram config not found at '{config_path}'. "
                "Create telegram_config.json with keys: bot_token, chat_id"
            )

        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        self.bot_token: str = data["bot_token"]
        self.chat_id: str = str(data["chat_id"])
        self._update_offset: Optional[int] = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _api(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self.bot_token}/{method}"

    @staticmethod
    def _escape_md(text: str) -> str:
        """Escape MarkdownV2 special characters."""
        specials = r'_*[]()~`>#+-=|{}.!'
        return ''.join(('\\' + ch if ch in specials else ch) for ch in text)

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------

    def send_prompt(self, title: str, prompt: str) -> Optional[int]:
        """Send the prompt to the personal chat as a single editable message.

        The message is sent **without** ``reply_markup`` so that it can be
        edited later via ``editMessageText`` (Telegram only allows editing
        messages that have no markup or an InlineKeyboard).

        In a 1-on-1 private chat the user can simply swipe/long-press the
        message to reply; ``ForceReply`` is unnecessary.

        Returns the sent ``message_id``, or ``None`` on failure.
        """
        # Truncate prompt to Telegram's 4096 char limit (with room for header)
        max_prompt_len = 3800
        truncated = prompt[:max_prompt_len]
        if len(prompt) > max_prompt_len:
            truncated += "\n\\.\\.\\.(truncated)"

        text = (
            f"\U0001f5a5\ufe0f *{self._escape_md(title)}*\n\n"
            f"{self._escape_md(truncated)}\n\n"
            f"\u23f3 _Waiting for response\\.\\.\\._\n"
            f"_Reply to this message to respond\\._"
        )
        try:
            resp = requests.post(
                self._api("sendMessage"),
                json={
                    "chat_id": self.chat_id,
                    "text": text,
                    "parse_mode": "MarkdownV2",
                },
                timeout=15,
            )
            if resp.status_code == 200:
                return resp.json()["result"]["message_id"]
            else:
                try:
                    print(f"[TelegramBridge] send_prompt failed: {resp.status_code}")
                except UnicodeEncodeError:
                    pass
        except Exception as exc:
            try:
                print(f"[TelegramBridge] send_prompt error: {exc}")
            except UnicodeEncodeError:
                pass
        return None

    def edit_message(self, message_id: int, new_text: str, max_retries: int = 3) -> bool:
        """Edit a previously sent message (plain text, no parse_mode).

        Retries up to *max_retries* times on transient failures (network errors,
        rate-limits, non-200 responses).  Returns True if the edit succeeded,
        False otherwise.  Diagnostic details are written to _remote_input_diag.log.

        Only works on messages sent WITHOUT reply_markup or with InlineKeyboard.
        Messages sent with ForceReply cannot be edited (Telegram API limitation).
        """
        diag_path = os.path.join(_SCRIPT_DIR, "_remote_input_diag.log")
        def _log(msg):
            try:
                with open(diag_path, "a", encoding="utf-8") as f:
                    f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [edit_message] {msg}\n")
            except Exception:
                pass

        for attempt in range(1, max_retries + 1):
            try:
                resp = requests.post(
                    self._api("editMessageText"),
                    json={
                        "chat_id": self.chat_id,
                        "message_id": message_id,
                        "text": new_text,
                    },
                    timeout=10,
                )
                if resp.status_code == 200:
                    _log(f"Attempt {attempt}: SUCCESS (HTTP 200)")
                    return True
                # Log full response for diagnosis
                _log(f"Attempt {attempt}: FAILED HTTP {resp.status_code} - {resp.text}")
            except Exception as exc:
                _log(f"Attempt {attempt}: EXCEPTION - {exc}")
            # Short back-off before next retry (skip sleep after last attempt)
            if attempt < max_retries:
                time.sleep(1)
        _log("All retries exhausted.")
        return False

    def send_status(self, prompt_message_id: int, status_text: str) -> bool:
        """Send a follow-up status message as a reply to the original prompt.

        Used as a fallback when edit_message is not available (e.g. if the
        status message was never created).

        Returns True if the status message was sent successfully, False otherwise.
        """
        diag_path = os.path.join(_SCRIPT_DIR, "_remote_input_diag.log")
        def _log(msg):
            try:
                with open(diag_path, "a", encoding="utf-8") as f:
                    f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [send_status] {msg}\n")
            except Exception:
                pass

        try:
            resp = requests.post(
                self._api("sendMessage"),
                json={
                    "chat_id": self.chat_id,
                    "text": status_text,
                    "reply_to_message_id": prompt_message_id,
                    "allow_sending_without_reply": True,
                },
                timeout=10,
            )
            if resp.status_code == 200:
                _log(f"Status sent OK (replying to msg {prompt_message_id})")
                return True
            _log(f"Status send FAILED: HTTP {resp.status_code} - {resp.text}")
        except Exception as exc:
            _log(f"Status send EXCEPTION: {exc}")
        return False

    def delete_message(self, message_id: int) -> bool:
        """Delete a message from the chat.  Returns True on success."""
        try:
            resp = requests.post(
                self._api("deleteMessage"),
                json={"chat_id": self.chat_id, "message_id": message_id},
                timeout=10,
            )
            return resp.status_code == 200
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Polling
    # ------------------------------------------------------------------

    def flush_updates(self) -> None:
        """Consume all pending updates so we only see new messages."""
        try:
            resp = requests.get(
                self._api("getUpdates"),
                params={"timeout": 0},
                timeout=5,
            )
            if resp.status_code == 200:
                updates = resp.json().get("result", [])
                if updates:
                    self._update_offset = updates[-1]["update_id"] + 1
        except Exception:
            pass

    def poll_for_reply(
        self,
        prompt_message_id: int,
        cancel_event: threading.Event,
        poll_interval: float = 1.5,
    ) -> Optional[str]:
        """Block until a reply to *prompt_message_id* arrives or *cancel_event* is set.

        Uses Telegram long-polling (3 s server-side timeout per request).
        Returns the reply text, or ``None`` if cancelled / timed out.
        """
        self.flush_updates()

        while not cancel_event.is_set():
            try:
                params = {
                    "timeout": 3,                # Telegram server-side long-poll
                    "allowed_updates": ["message"],
                }
                if self._update_offset is not None:
                    params["offset"] = self._update_offset

                resp = requests.get(
                    self._api("getUpdates"), params=params, timeout=8
                )
                if resp.status_code != 200:
                    time.sleep(poll_interval)
                    continue

                for update in resp.json().get("result", []):
                    self._update_offset = update["update_id"] + 1
                    msg = update.get("message")
                    if not msg:
                        continue
                    # Must be from the configured personal chat
                    if str(msg.get("chat", {}).get("id")) != self.chat_id:
                        continue
                    # Must be a reply to our prompt message
                    reply_to = msg.get("reply_to_message")
                    if reply_to and reply_to.get("message_id") == prompt_message_id:
                        return msg.get("text", "")

            except Exception as exc:
                # requests.Timeout is expected - just loop
                if "timeout" not in str(exc).lower():
                    try:
                        print(f"[TelegramBridge] poll error: {exc}")
                    except UnicodeEncodeError:
                        pass
                time.sleep(poll_interval)

        return None


# ------------------------------------------------------------------
# Module-level convenience
# ------------------------------------------------------------------

def is_telegram_configured() -> bool:
    """Return True when telegram_config.json exists and contains valid keys."""
    try:
        if not os.path.isfile(_DEFAULT_CONFIG):
            return False
        with open(_DEFAULT_CONFIG, "r", encoding="utf-8") as f:
            data = json.load(f)
        return bool(data.get("bot_token")) and bool(data.get("chat_id"))
    except Exception:
        return False
