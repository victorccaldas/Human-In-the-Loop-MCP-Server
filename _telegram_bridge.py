"""
Telegram Bridge — lightweight Bot API wrapper for the remote input feature.

Provides methods to:
  - Send a prompt message to a personal Telegram chat
  - Long-poll for a reply to that specific message
  - Edit the message after the response arrives (status update)

Used by ``get_remote_input`` in ``human_loop_server.py`` to enable answering
Human-in-the-Loop prompts remotely via Telegram.

Configuration is resolved from two sources (checked in order):

1. **Config file** — ``telegram_config.json`` (same directory as this file):
   ``{ "bot_token": "<BotFather token>", "chat_id": "<personal chat id>" }``

2. **Environment variables** (fallback when the config file is absent):
   - ``TELEGRAM_BOT_TOKEN``
   - ``TELEGRAM_CHAT_ID``

The environment-variable path is especially useful when the server is
installed via ``uvx`` / ``pip`` and there is no writable script directory
for a config file.
"""

import json
import os
import threading
import time
from typing import Any, Optional

from _telegram_shared_hub import (
    SharedTelegramHubClient,
    detect_unsafe_direct_polling,
    ensure_shared_telegram_hub,
    is_shared_telegram_enabled,
    is_telegram_configured as shared_is_telegram_configured,
    load_telegram_credentials,
)

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

        # Resolve credentials through the shared helper so the bridge and hub
        # always agree on the same bot/chat scope and runtime paths.
        credentials = load_telegram_credentials(config_path=config_path or _DEFAULT_CONFIG)
        if credentials is None:
            raise FileNotFoundError(
                "Telegram credentials not found. Provide either:\n"
                "  1. telegram_config.json with keys: bot_token, chat_id\n"
                "  2. Environment variables: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID"
            )

        self.bot_token: str = credentials.bot_token
        self.chat_id: str = str(credentials.chat_id)
        self._update_offset: Optional[int] = None
        self._shared_hub_client: Optional[SharedTelegramHubClient] = None

        # Shared mode must never fall back to local getUpdates polling, because
        # the hub is the sole safe owner for a same-host bot/chat scope.
        if is_shared_telegram_enabled():
            self._shared_hub_client = ensure_shared_telegram_hub(credentials=credentials)
        else:
            detect_unsafe_direct_polling(credentials)

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

    @staticmethod
    def _escape_html(text: str) -> str:
        """Escape HTML special characters for parse_mode='HTML'."""
        return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

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
                    print(
                        f"[TelegramBridge] send_prompt failed: "
                        f"HTTP {resp.status_code} \u2014 {resp.text[:300]}"
                    )
                except UnicodeEncodeError:
                    pass
        except Exception as exc:
            try:
                print(f"[TelegramBridge] send_prompt error: {exc}")
            except UnicodeEncodeError:
                pass
        return None

    def send_prompt_with_miniapp(
        self,
        title: str,
        prompt: str,
        webapp_url: str,
        name_or_role: str = "",
    ) -> Optional[int]:
        """Send the prompt with an InlineKeyboard button that opens the Mini App.

        The button carries ``web_app={"url": webapp_url}`` so Telegram opens
        the page inside its native WebView.  Messages with InlineKeyboard can
        still be edited via ``editMessageText``, so the existing post-answer
        edit flow remains intact.

        Returns the sent ``message_id``, or ``None`` on failure.
        """
        max_prompt_len = 3800
        truncated = prompt[:max_prompt_len]
        if len(prompt) > max_prompt_len:
            truncated += "\n\\.\\.\\.(truncated)"

        role_line = (
            f"\U0001f916 _{self._escape_md(name_or_role)}_\n"
            if name_or_role else ""
        )
        text = (
            f"{role_line}"
            f"\U0001f5a5\ufe0f *{self._escape_md(title)}*\n\n"
            f"{self._escape_md(truncated)}\n\n"
            f"\u23f3 _Waiting for response\\.\\.\\._\n"
            f"_Tap the button below to open the response dialog\\._"
        )
        try:
            resp = requests.post(
                self._api("sendMessage"),
                json={
                    "chat_id": self.chat_id,
                    "text": text,
                    "parse_mode": "MarkdownV2",
                    "reply_markup": {
                        "inline_keyboard": [
                            [
                                {
                                    "text": "\U0001f4f2  Open Response Dialog",
                                    "web_app": {"url": webapp_url},
                                }
                            ]
                        ]
                    },
                },
                timeout=15,
            )
            if resp.status_code == 200:
                return resp.json()["result"]["message_id"]
            else:
                try:
                    print(
                        f"[TelegramBridge] send_prompt_with_miniapp failed: "
                        f"{resp.status_code} {resp.text[:200]}"
                    )
                except UnicodeEncodeError:
                    pass
        except Exception as exc:
            try:
                print(f"[TelegramBridge] send_prompt_with_miniapp error: {exc}")
            except UnicodeEncodeError:
                pass
        return None

    def poll_for_answer(
        self,
        prompt_message_id: int,
        cancel_event: threading.Event,
        answer_queue=None,
        poll_interval: float = 1.5,
    ) -> Optional[str]:
        """Compatibility wrapper that returns only the accepted answer text."""
        details = self.poll_for_answer_details(
            prompt_message_id,
            cancel_event,
            answer_queue=answer_queue,
            poll_interval=poll_interval,
        )
        if details is None:
            return None
        return str(details.get("text") or "")

    @staticmethod
    def _normalize_answer_payload(
        payload: Any,
        *,
        default_source: str,
    ) -> Optional[dict[str, Any]]:
        """Normalize Mini App and Telegram answers into one comparable shape."""
        if payload is None:
            return None

        if isinstance(payload, dict):
            text = payload.get("text", payload.get("answer"))
            if text is None:
                return None
            received_at = payload.get("received_at")
            try:
                received_at_value = int(received_at)
            except (TypeError, ValueError):
                received_at_value = time.monotonic_ns()
            return {
                "text": str(text),
                "source": str(payload.get("source") or default_source),
                "received_at": received_at_value,
            }

        return {
            "text": str(payload),
            "source": default_source,
            "received_at": time.monotonic_ns(),
        }

    @classmethod
    def _pick_first_answer(
        cls,
        first_candidate: Optional[dict[str, Any]],
        second_candidate: Optional[dict[str, Any]],
    ) -> Optional[dict[str, Any]]:
        """Choose the earliest accepted answer, preferring Mini App on ties."""
        if first_candidate is None:
            return second_candidate
        if second_candidate is None:
            return first_candidate

        first_received = int(first_candidate.get("received_at") or 0)
        second_received = int(second_candidate.get("received_at") or 0)
        if first_received <= second_received:
            return first_candidate
        return second_candidate

    def poll_for_answer_details(
        self,
        prompt_message_id: int,
        cancel_event: threading.Event,
        answer_queue=None,
        poll_interval: float = 1.5,
    ) -> Optional[dict[str, Any]]:
        """Block until an answer arrives from any channel, or *cancel_event* is set.

        Checks three sources in priority order on each loop iteration:

        1. *answer_queue* — a ``queue.SimpleQueue`` populated by the Mini App
           HTTP server when the user submits via the web form (fastest path).
        2. ``web_app_data`` Telegram update — sent by ``Telegram.WebApp.sendData()``
           inside the Mini App as a belt-and-suspenders path.
        3. Plain-text reply to *prompt_message_id* — backwards-compatible with the
           existing Telegram reply workflow (no Mini App required).

        Parameters
        ----------
        prompt_message_id:
            ``message_id`` of the original prompt sent to the chat.
        cancel_event:
            ``threading.Event``; when set the method returns ``None``.
        answer_queue:
            Optional ``queue.SimpleQueue`` shared with ``MiniAppHTTPServer``.
            Pass ``None`` when the Mini App is not in use.
        poll_interval:
            Seconds to sleep between ``getUpdates`` requests on error.

        Returns
        -------
        dict or None
            The accepted answer payload, including the answer text and source.
        """
        import queue as _queue_mod

        # Shared mode keeps Mini App answers local, but routes plain Telegram
        # replies through the host-local hub instead of getUpdates polling here.
        if self._shared_hub_client is not None:
            if answer_queue is None:
                return self._shared_hub_client.wait_for_reply_details(
                    prompt_message_id,
                    cancel_event,
                    poll_interval=poll_interval,
                )

            # Run the hub wait concurrently so local Mini App submissions can
            # still win even while the shared hub is long-polling Telegram.
            hub_cancel = threading.Event()
            hub_result: dict[str, Any] = {}
            hub_done = threading.Event()

            def _mirror_cancel() -> None:
                cancel_event.wait()
                hub_cancel.set()

            def _wait_for_hub_reply() -> None:
                result = self._shared_hub_client.wait_for_reply_details(
                    prompt_message_id,
                    hub_cancel,
                    poll_interval=poll_interval,
                )
                if result is not None:
                    hub_result["value"] = result
                hub_done.set()

            threading.Thread(
                target=_mirror_cancel,
                daemon=True,
                name="telegram-bridge-shared-cancel-mirror",
            ).start()
            threading.Thread(
                target=_wait_for_hub_reply,
                daemon=True,
                name="telegram-bridge-shared-hub-wait",
            ).start()

            while not cancel_event.is_set():
                local_answer = None
                try:
                    local_answer = self._normalize_answer_payload(
                        answer_queue.get_nowait(),
                        default_source="telegram_miniapp",
                    )
                except _queue_mod.Empty:
                    pass

                remote_answer = None
                if hub_done.is_set():
                    remote_answer = self._normalize_answer_payload(
                        hub_result.get("value"),
                        default_source="telegram_reply",
                    )

                accepted_answer = self._pick_first_answer(local_answer, remote_answer)
                if accepted_answer is not None:
                    hub_cancel.set()
                    return accepted_answer

                if hub_done.is_set():
                    return None

                cancel_event.wait(timeout=min(max(poll_interval / 10.0, 0.05), 0.2))
            return None

        self.flush_updates()

        while not cancel_event.is_set():
            # ── 1. Check local HTTP-server queue (Mini App POST /submit) ──
            if answer_queue is not None:
                try:
                    return self._normalize_answer_payload(
                        answer_queue.get_nowait(),
                        default_source="telegram_miniapp",
                    )
                except _queue_mod.Empty:
                    pass

            # ── 2 & 3. Poll Telegram getUpdates ──────────────────────────
            try:
                params: dict = {
                    "timeout": 3,
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
                    if str(msg.get("chat", {}).get("id")) != self.chat_id:
                        continue

                    # ── Path 2 (reserved): web_app_data from sendData() ──
                    # Note: sendData() only fires for ReplyKeyboard-opened Mini
                    # Apps.  Our InlineKeyboard Mini App uses POST /submit
                    # (Path 1) instead.  This branch is kept for forward
                    # compatibility if the open mode is ever changed.
                    web_app_data = msg.get("web_app_data")
                    if web_app_data:
                        answer = self._normalize_answer_payload(
                            {
                                "text": web_app_data.get("data", ""),
                                "source": "telegram_web_app_data",
                                "received_at": time.monotonic_ns(),
                            },
                            default_source="telegram_web_app_data",
                        )
                        try:
                            reply_id = msg.get("message_id")
                            if isinstance(reply_id, int):
                                self.react_to_message(reply_id, "\U0001f44d")
                        except Exception:
                            pass
                        return answer

                    # ── Path 3: plain-text reply to the prompt message ────
                    reply_to = msg.get("reply_to_message")
                    if reply_to and reply_to.get("message_id") == prompt_message_id:
                        try:
                            reply_id = msg.get("message_id")
                            if isinstance(reply_id, int):
                                self.react_to_message(reply_id, "\U0001f44d")
                        except Exception:
                            pass
                        return self._normalize_answer_payload(
                            {
                                "text": msg.get("text", ""),
                                "source": "telegram_reply",
                                "received_at": time.monotonic_ns(),
                            },
                            default_source="telegram_reply",
                        )

            except Exception as exc:
                if "timeout" not in str(exc).lower():
                    try:
                        print(f"[TelegramBridge] poll_for_answer error: {exc}")
                    except UnicodeEncodeError:
                        pass
                time.sleep(poll_interval)

        return None

    def complete_prompt_session(
        self,
        prompt_message_id: int,
        *,
        status: str,
        source: Optional[str] = None,
    ) -> None:
        """Notify the shared hub that a prompt lifecycle finished locally."""
        if self._shared_hub_client is None:
            return
        self._shared_hub_client.complete_prompt(
            prompt_message_id,
            status=status,
            source=source,
        )

    def edit_message(
        self,
        message_id: int,
        new_text: str,
        max_retries: int = 3,
        parse_mode: Optional[str] = None,
    ) -> bool:
        """Edit a previously sent message.

        ``parse_mode`` may be ``None`` (plain text), ``'HTML'``, or
        ``'MarkdownV2'``.  Retries up to *max_retries* times on transient
        failures.  Returns True if the edit succeeded, False otherwise.
        Diagnostic details are written to _remote_input_diag.log.

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
                payload: dict = {
                    "chat_id": self.chat_id,
                    "message_id": message_id,
                    "text": new_text,
                }
                if parse_mode:
                    payload["parse_mode"] = parse_mode
                resp = requests.post(
                    self._api("editMessageText"),
                    json=payload,
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

    def send_text(self, text: str) -> Optional[int]:
        """Send a plain text message to the configured Telegram chat.

        Returns the message_id on success, None on failure.
        """
        if not self.bot_token or not self.chat_id:
            return None

        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
                json={"chat_id": self.chat_id, "text": text},
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("ok"):
                    return data["result"]["message_id"]
        except Exception as e:
            print(f"[TelegramBridge] send_text error: {e}")

        return None

    def delete_message(self, message_id: int) -> bool:
        """Delete a message from the chat.  Returns True on success."""
        diag_path = os.path.join(_SCRIPT_DIR, "_remote_input_diag.log")
        try:
            resp = requests.post(
                self._api("deleteMessage"),
                json={"chat_id": self.chat_id, "message_id": message_id},
                timeout=10,
            )
            return resp.status_code == 200
        except Exception as exc:
            try:
                with open(diag_path, "a", encoding="utf-8") as f:
                    f.write(
                        f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] "
                        f"[delete_message] msg_id={message_id}: {exc}\n"
                    )
            except Exception:
                pass
            return False

    def react_to_message(self, message_id: int, emoji: str = "👍") -> bool:
        """React to a message in chat using Telegram `setMessageReaction`.

        Returns True when the reaction is accepted by Telegram, False otherwise.
        This is best-effort and should not interrupt the main input flow.
        Failures are written to _remote_input_diag.log for diagnostics.
        """
        diag_path = os.path.join(_SCRIPT_DIR, "_remote_input_diag.log")
        def _log(msg):
            try:
                with open(diag_path, "a", encoding="utf-8") as f:
                    f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [react_to_message] {msg}\n")
            except Exception:
                pass

        try:
            resp = requests.post(
                self._api("setMessageReaction"),
                json={
                    "chat_id": self.chat_id,
                    "message_id": message_id,
                    "reaction": [{"type": "emoji", "emoji": emoji}],
                    "is_big": False,
                },
                timeout=10,
            )
            if resp.status_code == 200:
                _log(f"Reaction '{emoji}' set on msg {message_id}: SUCCESS")
                return True
            _log(f"Reaction '{emoji}' on msg {message_id}: FAILED HTTP {resp.status_code} - {resp.text}")
            return False
        except Exception as exc:
            _log(f"Reaction '{emoji}' on msg {message_id}: EXCEPTION - {exc}")
            return False

    # ------------------------------------------------------------------
    # Polling
    # ------------------------------------------------------------------

    def set_my_commands(self, commands: list) -> bool:
        """Register bot commands for the Telegram command menu.

        Each item in commands must be a dict with 'command' and 'description' keys.
        This makes the '/' button in Telegram show available commands.
        Returns True on success, False otherwise.
        """
        if not self.bot_token:
            return False

        try:
            resp = requests.post(
                self._api("setMyCommands"),
                json={"commands": commands},
                timeout=10,
            )
            if resp.status_code == 200 and resp.json().get("ok"):
                print("[TelegramBridge] Bot command menu registered successfully.")
                return True
            print(f"[TelegramBridge] setMyCommands failed: {resp.status_code} \u2014 {resp.text[:200]}")
        except Exception as exc:
            print(f"[TelegramBridge] setMyCommands error: {exc}")
        return False

    def flush_updates(self) -> None:
        """Consume all pending updates so we only see new messages."""
        # In shared mode the host-local hub owns getUpdates, so local instances
        # intentionally avoid touching Telegram polling state.
        if self._shared_hub_client is not None:
            return
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
        except Exception as exc:
            print(f"[TelegramBridge] flush_updates failed: {exc} "
                  "\u2014 stale updates may be reprocessed.")

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
        # Shared mode delegates inbound reply routing to the host-local hub so
        # this process never competes for Telegram getUpdates ownership.
        if self._shared_hub_client is not None:
            return self._shared_hub_client.wait_for_reply(
                prompt_message_id,
                cancel_event,
                poll_interval=poll_interval,
            )

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
                        # Best-effort visual ack on the user's response message.
                        # ✅ is REACTION_INVALID per the Telegram Bot API; 👍 is used instead.
                        try:
                            reply_message_id = msg.get("message_id")
                            if isinstance(reply_message_id, int):
                                self.react_to_message(reply_message_id, "👍")
                        except Exception:
                            pass
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
    """Return True when Telegram credentials are available.

    Checks two sources in order:
    1. ``telegram_config.json`` file (same directory as this script)
    2. Environment variables ``TELEGRAM_BOT_TOKEN`` and ``TELEGRAM_CHAT_ID``
    """
    return shared_is_telegram_configured(_DEFAULT_CONFIG)
