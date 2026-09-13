"""Real Telegram adapter (Section 6.5): httpx over the Bot API directly, no
bot framework. The Bot API can't fetch a message by ID, so a successful
sendMessage/editMessageText response (which carries message_id) is the only
proof of delivery — exactly what executor/verifier.py already expect."""
from __future__ import annotations

import httpx

from cutoff.models import Button
from cutoff.pipeline.executor import AdapterError

TIMEOUT = 25.0


def _keyboard(buttons: list[Button] | None) -> dict | None:
    if not buttons:
        return None
    return {"inline_keyboard": [[{"text": b.text, "callback_data": b.callback_data} for b in buttons]]}


def _raise_for_status(resp: httpx.Response) -> None:
    if resp.status_code == 200:
        return
    retry_after = None
    if resp.status_code == 429:
        try:
            retry_after = resp.json().get("parameters", {}).get("retry_after")
        except Exception:
            pass
    raise AdapterError(f"Telegram API error {resp.status_code}: {resp.text}", status_code=resp.status_code,
                        retry_after=retry_after)


class TelegramMessenger:
    def __init__(self, bot_token: str, chat_id: str):
        self._base = f"https://api.telegram.org/bot{bot_token}"
        self._chat_id = chat_id

    def send(self, key: str, text: str, buttons: list[Button] | None) -> str:
        payload = {"chat_id": self._chat_id, "text": text}
        keyboard = _keyboard(buttons)
        if keyboard:
            payload["reply_markup"] = keyboard
        resp = httpx.post(f"{self._base}/sendMessage", json=payload, timeout=TIMEOUT)
        _raise_for_status(resp)
        return str(resp.json()["result"]["message_id"])

    def edit(self, message_id: str, text: str, buttons: list[Button] | None) -> None:
        payload = {"chat_id": self._chat_id, "message_id": int(message_id), "text": text}
        keyboard = _keyboard(buttons)
        if keyboard:
            payload["reply_markup"] = keyboard
        resp = httpx.post(f"{self._base}/editMessageText", json=payload, timeout=TIMEOUT)
        _raise_for_status(resp)
