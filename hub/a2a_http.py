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
import secrets as _secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional, Tuple

from . import clients as clientlib
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

    def _resolve_client(self):
        """(вид, клиент): ('legacy', None) / ('client', dict) / (None, None) — отказ.

        Legacy-токен (TG_GATEWAY_A2A_TOKEN) остаётся правами владельца; клиентские
        токены `tgw_<client_id>_<secret>` дают свои боты, политику и лимиты.
        """
        header = self.headers.get("Authorization", "")
        if not header.lower().startswith("bearer "):
            return (None, None)
        token = header[7:].strip()
        if not token:
            return (None, None)
        if self.token and _secrets.compare_digest(token, self.token):
            return ("legacy", None)
        if not self.token and not self.hub.db.q("SELECT 1 FROM clients LIMIT 1"):
            return ("legacy", None)          # фронт без токена и без клиентов — открытый локально
        row = self.hub.db.client_by_hash(clientlib.token_hash(token))
        if not row:
            return (None, None)
        client = clientlib.as_client(row)
        if not int(client.get("enabled") or 0) or client.get("revoked_at"):
            self.hub.db.event("client_denied", client=client["client_id"], reason="revoked")
            return (None, None)
        expires_at = client.get("expires_at")
        if expires_at and float(expires_at) < time.time():
            self.hub.db.event("client_denied", client=client["client_id"], reason="expired")
            return (None, None)
        return ("client", client)

    def _auth_ok(self) -> bool:
        kind, _ = self._resolve_client()
        return kind is not None

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
    # ── роутинг по пути: /bot/<alias> = отдельный агент ─────────────────────
    def _alias(self) -> Optional[str]:
        parts = [p for p in self.path.split("?")[0].split("/") if p]
        if len(parts) >= 2 and parts[0] == "bot":
            return parts[1]
        return None

    def _normalized_path(self) -> str:
        path = self.path.split("?")[0]
        alias = self._alias()
        if alias:
            path = "/" + "/".join(p for p in path.split("/") if p and p != "bot" and p != alias)
            if not path.endswith("/.well-known/agent-card.json") and path in ("/", ""):
                path = "/"
        return path

    def _owner(self) -> Optional[int]:
        return next(iter(self.hub.owners)) if self.hub.owners else None

    def do_GET(self) -> None:  # noqa: N802
        path = self._normalized_path()
        alias = self._alias()
        if path not in ("/.well-known/agent-card.json", "/.well-known/agent.json",
                        "/health", "/card"):
            return self._send(404, {"error": "not found"})
        if not self._auth_ok():
            return self._send(401, {"error": "unauthorized"})
        if path == "/health":
            return self._send(200, {"ok": True})
        owner = self._owner()
        if alias:
            bot = self.hub.db.bot_by_alias(owner, alias) if owner else None
            if not bot:
                return self._send(404, {"error": f"бот «{alias}» не найден"})
            url = f"{self.hub._a2a_public_url()}/bot/{alias}"
            card = rpc.bot_card(bot, owner)
            card["supportedInterfaces"] = [{"protocolBinding": "JSONRPC", "url": url}]
            card["url"] = url
        else:
            card = (rpc.hub_card(owner, self.hub.db.bots(owner),
                                 public_url=self.hub._a2a_public_url()) if owner else {})
        card["metadata"] = {**card.get("metadata", {}), "transport": "http-a2a-proxy",
                            "upstream": "telegram", "bot": alias}
        return self._send(200, card)

    # ── POST: JSON-RPC ───────────────────────────────────────────────────────
    def do_POST(self) -> None:  # noqa: N802
        kind, client = self._resolve_client()
        if kind is None:
            return self._send(401, {"error": "unauthorized"})
        payload, err = self._body()
        if err:
            return self._send(200, json.loads(rpc.rpc_err(None, rpc.ERR_PARSE, err)))
        rpc_id = payload.get("id")
        method = str(payload.get("method") or "")
        params = payload.get("params") or {}
        alias = self._alias()
        if alias and isinstance(params, dict):
            params.setdefault("to", alias)
        if client:
            self.hub.db.touch_client(str(client["client_id"]))
            client["last_method"] = method
        try:
            result, code = self.hub.http_rpc(method, params,
                                             client=client if kind == "client" else None)
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
