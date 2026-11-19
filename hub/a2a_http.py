#!/usr/bin/env python3
"""HTTP-фронт A2A: стандартный A2A-клиент → наш хаб.

Зачем: сам хаб общается по Telegram (JSON-RPC в ЛС), поэтому у него нет URL,
карточки на `/.well-known/agent-card.json` и HTTP-методов. Этот модуль поднимает
локальный A2A-сервер (stdlib `http.server`) внутри процесса хаба и переводит
HTTP/JSON-RPC в те же операции хаба:

    A2A client ──HTTP──▶ a2a_http ──▶ Hub (SQLite, карточки владельцу) ──Telegram──▶ бот

Отличия от «настоящего» A2A (честно): `message/send` всегда асинхронный —
владелец подтверждает карточку, поэтому клиент получает Task в состоянии
`auth-required` и дальше поллит `tasks/get`. Стриминга и push-вебхуков нет
(`capabilities.streaming=false`, `pushNotifications=false` в карточке).
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional, Tuple

from . import rpc

A2A_SRC = "a2a-client"


class _Handler(BaseHTTPRequestHandler):
    hub: Any = None          # инъекция из start()
    token: Optional[str] = None

    # ── infra ────────────────────────────────────────────────────────────────
    def log_message(self, format: str, *args: Any) -> None:  # тишина в journal
        return

    def _send(self, code: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth_ok(self) -> bool:
        if not self.token:
            return True
        header = self.headers.get("Authorization", "")
        return header == f"Bearer {self.token}"

    def _body(self) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            data = json.loads(raw.decode("utf-8") or "{}")
        except Exception as exc:  # noqa: BLE001
            return None, f"bad json: {exc}"
        if not isinstance(data, dict):
            return None, "json-rpc должен быть объектом"
        return data, None

    # ── GET: discovery ───────────────────────────────────────────────────────
    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        if path not in ("/.well-known/agent-card.json", "/.well-known/agent.json",
                        "/health", "/card"):
            return self._send(404, {"error": "not found"})
        if not self._auth_ok():
            return self._send(401, {"error": "unauthorized"})
        if path == "/health":
            return self._send(200, {"ok": True})
        owner = next(iter(self.hub.owners)) if self.hub.owners else None
        card = rpc.hub_card(owner, self.hub.db.bots(owner)) if owner else {}
        card["metadata"] = {**card.get("metadata", {}), "transport": "http-a2a-proxy",
                            "upstream": "telegram"}
        return self._send(200, card)

    # ── POST: JSON-RPC ───────────────────────────────────────────────────────
    def do_POST(self) -> None:  # noqa: N802
        if not self._auth_ok():
            return self._send(401, {"error": "unauthorized"})
        payload, err = self._body()
        if err:
            return self._send(200, json.loads(rpc.rpc_err(None, rpc.ERR_PARSE, err)))
        rpc_id = payload.get("id")
        method = str(payload.get("method") or "")
        params = payload.get("params") or {}
        try:
            result, code = self.hub.http_rpc(method, params)
        except Exception as exc:  # noqa: BLE001
            return self._send(200, json.loads(rpc.rpc_err(rpc_id, rpc.ERR_INTERNAL,
                                                          f"{type(exc).__name__}: {exc}")))
        if code != 0:
            return self._send(200, json.loads(rpc.rpc_err(rpc_id, code, result.get("message", ""))))
        return self._send(200, json.loads(rpc.rpc_ok(rpc_id, result)))


def start(hub: Any, *, port: int, token: Optional[str] = None,
          host: str = "127.0.0.1") -> ThreadingHTTPServer:
    """Поднять A2A-фронт в фоновом потоке. Возвращает сервер (можно shutdown())."""
    handler = type("BoundHandler", (_Handler,), {"hub": hub, "token": token})
    server = ThreadingHTTPServer((host, port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True,
                              name=f"a2a-http-{port}")
    thread.start()
    return server
