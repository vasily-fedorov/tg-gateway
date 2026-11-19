#!/usr/bin/env python3
"""A2A-совместимая форма ответов поверх Telegram-транспорта.

Транспорт остаётся Telegram (DM hub↔бот), но контракт — JSON-RPC 2.0 и объекты
A2A: Agent Card, Task, TaskStatus, Part(kind=text). Стриминг и push не поддержаны
(`capabilities.streaming/pushNotifications = false`) — это указано в карточке.
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, Optional

PROTOCOL_VERSION = "1.0"

# Внутренние состояния → состояния A2A. `skipped` в A2A нет: отдаём как completed
# с metadata.skipped=true, чтобы не выдумывать несуществующее состояние.
STATE_MAP = {
    "submitted": "auth-required",   # ждём подтверждения владельца
    "working": "working",
    "input-required": "input-required",
    "completed": "completed",
    "failed": "failed",
    "rejected": "rejected",
    "cancelled": "canceled",
    "skipped": "completed",
}


def a2a_state(status: str) -> str:
    return STATE_MAP.get(str(status), "unknown")


def task_view(task: Any, *, history_length: Optional[int] = None,
              a2a: bool = False) -> Dict[str, Any]:
    """A2A Task из строки БД."""
    t = dict(task)
    state = a2a_state(t["status"])
    status: Dict[str, Any] = {"state": state, "timestamp": _iso(t.get("updated_at"))}
    if t.get("question"):
        status["message"] = {"role": "agent", "messageId": f"{t['task_id']}-q",
                             "parts": [{"kind": "text", "text": t["question"]}]}
    view: Dict[str, Any] = {
        "id": t["task_id"],
        "contextId": t["ctx"],
        "status": status,
        "kind": "task",
        "metadata": {
            "hops": t.get("hops"),
            "from": t.get("src_alias"),
            "to": t.get("dst_alias"),
            "humanApproval": True,
        },
    }
    if t["status"] == "skipped":
        view["metadata"]["skipped"] = True
    # artifacts — результат задачи (ответ агента); вход — в history
    if t.get("answer"):
        view["artifacts"] = [{"artifactId": f"{t['task_id']}-out",
                              "parts": [{"kind": "text", "text": t["answer"]}]}]
    messages = []
    if t.get("text"):
        messages.append({"role": "user", "messageId": f"{t['task_id']}-in",
                         "parts": [{"kind": "text", "text": t["text"]}]})
    if t.get("answer"):
        messages.append({"role": "agent", "messageId": f"{t['task_id']}-a",
                         "parts": [{"kind": "text", "text": t["answer"]}]})
    if history_length is None:          # A2A: не задано = без ограничения
        limit = len(messages) or 1
    else:
        limit = int(history_length or 0)
    messages = [] if limit == 0 else messages[-limit:]
    if messages:
        view["history"] = messages
    return view


def bot_card(bot: Any, owner: Optional[int] = None) -> Dict[str, Any]:
    b = dict(bot)
    desc = (b.get("description") or "").strip() or "Агент Hermes за шлюзом tg-gateway."
    skills = [{
        "id": b["alias"],
        "name": b["alias"],
        "description": desc,
        "tags": ["telegram", "hermes"],
    }]
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "name": f"@{b['username']}",
        "description": desc,
        "version": "0.5.0",
        "capabilities": {"streaming": False, "pushNotifications": False,
                         "stateTransitionHistory": False, "humanApproval": True},
        "securitySchemes": {"bearer": {"type": "http", "scheme": "bearer",
                                        "description": "TG_GATEWAY_A2A_TOKEN"}},
        "security": [{"bearer": []}],
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": skills,
        "metadata": {"transport": "telegram", "alias": b["alias"],
                     "owner": owner if owner is not None else b.get("owner_tg")},
    }


def hub_card(owner: int, bots: list, *, public_url: str = "") -> Dict[str, Any]:
    skills, agents = [], []
    for b in bots:
        b = dict(b)
        desc = (b.get("description") or "").strip() or f"@{b['username']}"
        bot_url = f"{public_url}/bot/{b['alias']}" if public_url else ""
        skills.append({
            "id": b["alias"],
            "name": b["alias"],
            "description": desc,
            "tags": ["telegram", "hermes"],
            "url": bot_url,
        })
        agents.append({"alias": b["alias"], "name": f"@{b['username']}", "description": desc,
                       "url": bot_url,
                       "card": f"{bot_url}/.well-known/agent-card.json" if bot_url else ""})
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "name": "tg-gateway hub",
        "description": "A2A-совместимый шлюз поверх Telegram: задачи между ботами с "
                       "подтверждением владельца.",
        "version": "0.5.0",
        "capabilities": {"streaming": False, "pushNotifications": False,
                         "stateTransitionHistory": False, "humanApproval": True},
        "securitySchemes": {"bearer": {"type": "http", "scheme": "bearer",
                                        "description": "TG_GATEWAY_A2A_TOKEN"}},
        "security": [{"bearer": []}],
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": skills,
        # v1.0: клиент берёт RPC-URL отсюда (Hermes: supportedInterfaces[JSONRPC])
        "supportedInterfaces": ([{"protocolBinding": "JSONRPC", "url": public_url}]
                                if public_url else []),
        "url": public_url or "",
        "metadata": {"transport": "telegram", "owner": owner, "bots": len(skills),
                     "agents": agents,
                     "transportNote": "hub не требует публичного HTTP: агенты пишут "
                                      "JSON-RPC в ЛС бота"},
    }


def rpc_ok(rpc_id: Any, result: Any) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": rpc_id, "result": result}, ensure_ascii=False)


def rpc_err(rpc_id: Any, code: int, message: str) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": rpc_id,
                       "error": {"code": code, "message": message}}, ensure_ascii=False)


# JSON-RPC коды (A2A использует стандартные + свои -32001..-32007)
ERR_PARSE = -32700
ERR_INVALID_REQUEST = -32600
ERR_METHOD_NOT_FOUND = -32601
ERR_INVALID_PARAMS = -32602
ERR_INTERNAL = -32603
ERR_TASK_NOT_FOUND = -32001
ERR_TASK_NOT_CANCELABLE = -32002
ERR_FORBIDDEN_BOT = -32003
ERR_UNAUTHORIZED = -32004
ERR_RATE_LIMITED = -32005


def _iso(ts: Optional[float]) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(ts or time.time())))
