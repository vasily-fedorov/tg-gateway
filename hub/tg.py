#!/usr/bin/env python3
"""Минимальный клиент Telegram Bot API для хаба: long polling, отправка, кнопки.

Без внешних зависимостей — только stdlib. Ошибки возвращаются как (ok, data).
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple


class Tg:
    def __init__(self, token: str, *, timeout: int = 35, base: str = "https://api.telegram.org"):
        self.token = token
        self.timeout = timeout
        self.base = base.rstrip("/")

    # ── low level ────────────────────────────────────────────────────────────
    def call(self, method: str, **params: Any) -> Tuple[bool, Any]:
        url = f"{self.base}/bot{self.token}/{method}"
        data = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None}).encode()
        req = urllib.request.Request(url, data=data)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = json.load(resp)
        except urllib.error.HTTPError as exc:  # тело ошибки тоже JSON
            try:
                payload = json.loads(exc.read().decode("utf-8", "replace"))
            except Exception:  # noqa: BLE001
                return False, f"http {exc.code}"
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)[:300]
        if payload.get("ok"):
            return True, payload.get("result")
        return False, payload.get("description") or payload

    # ── updates / messages ───────────────────────────────────────────────────
    def get_updates(self, offset: Optional[int], timeout: int = 30) -> List[Dict[str, Any]]:
        ok, data = self.call("getUpdates", offset=offset, timeout=timeout,
                             allowed_updates=json.dumps(["message", "callback_query"]))
        return data if ok and isinstance(data, list) else []

    def send(self, chat_id: Any, text: str, buttons: Optional[List[List[Tuple[str, str]]]] = None,
             *, reply_to: Optional[int] = None, thread_id: Optional[int] = None,
             ) -> Tuple[bool, Any]:
        markup = None
        if buttons:
            markup = json.dumps({"inline_keyboard": [
                [{"text": label, "callback_data": data} for label, data in row] for row in buttons]})
        return self.call("sendMessage", chat_id=chat_id, text=text[:4000],
                         reply_markup=markup, reply_to_message_id=reply_to,
                         message_thread_id=thread_id)

    def edit(self, chat_id: Any, message_id: int, text: str) -> Tuple[bool, Any]:
        return self.call("editMessageText", chat_id=chat_id, message_id=message_id, text=text[:4000])

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        self.call("answerCallbackQuery", callback_query_id=callback_id, text=text[:200])

    @staticmethod
    def buttons_ok(data: str) -> bool:
        """callback_data ≤ 64 байт — держим короткий формат kind:verdict:task."""
        return len(data.encode()) <= 64
