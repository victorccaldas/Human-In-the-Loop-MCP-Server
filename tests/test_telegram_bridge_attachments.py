# pyright: reportMissingImports=false

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import _telegram_bridge as bridge_module


class _MockResponse:
    def __init__(self, status_code=200, payload=None, text="OK"):
        self.status_code = status_code
        self._payload = payload or {"result": {"message_id": 123}}
        self.text = text

    def json(self):
        return self._payload


def _build_bridge(monkeypatch):
    credentials = type("Creds", (), {"bot_token": "token", "chat_id": "123"})()
    monkeypatch.setattr(bridge_module, "load_telegram_credentials", lambda config_path=None: credentials)
    monkeypatch.setattr(bridge_module, "is_shared_telegram_enabled", lambda: False)
    monkeypatch.setattr(bridge_module, "detect_unsafe_direct_polling", lambda credentials=None: None)
    return bridge_module.TelegramBridge()


def test_send_prompt_with_attachment_uses_send_document(monkeypatch, tmp_path):
    bridge = _build_bridge(monkeypatch)
    attachment_path = tmp_path / "report.txt"
    attachment_path.write_text("hello", encoding="utf-8")
    calls = []

    def _fake_post(url, data=None, files=None, json=None, timeout=None):
        calls.append({"url": url, "data": data, "files": files, "json": json})
        if url.endswith("pinChatMessage"):
            return _MockResponse(payload={"result": True})
        return _MockResponse(payload={"result": {"message_id": 321}})

    monkeypatch.setattr(bridge_module.requests, "post", _fake_post)

    message_id = bridge.send_prompt("Title", "Prompt", attachment_path=str(attachment_path))

    assert message_id == 321
    assert calls[0]["url"].endswith("sendDocument")
    assert calls[0]["files"]["document"].name.endswith("report.txt")


def test_edit_message_uses_caption_endpoint_for_document_prompt(monkeypatch, tmp_path):
    bridge = _build_bridge(monkeypatch)
    attachment_path = tmp_path / "report.txt"
    attachment_path.write_text("hello", encoding="utf-8")
    calls = []

    def _fake_post(url, data=None, files=None, json=None, timeout=None):
        calls.append({"url": url, "data": data, "files": files, "json": json})
        if url.endswith("pinChatMessage"):
            return _MockResponse(payload={"result": True})
        return _MockResponse(payload={"result": {"message_id": 654}})

    monkeypatch.setattr(bridge_module.requests, "post", _fake_post)

    message_id = bridge.send_prompt("Title", "Prompt", attachment_path=str(attachment_path))
    ok = bridge.edit_message(message_id, "<b>Updated</b>", parse_mode="HTML")

    assert ok is True
    assert calls[0]["url"].endswith("sendDocument")
    assert calls[-1]["url"].endswith("editMessageCaption")
    assert calls[-1]["json"]["caption"] == "<b>Updated</b>"