"""gateway-relay — approval cards, relay tools and the inbound pump for `gateway`."""
from __future__ import annotations

import asyncio
from pathlib import Path

from . import core

_ROUTES_SCHEMA = {
    "name": "relay_routes",
    "description": "Список разрешённых адресатов для передачи сообщений. Других адресатов не существует.",
    "parameters": {"type": "object", "properties": {}, "required": []},
}

_SEND_SCHEMA = {
    "name": "relay_send",
    "description": (
        "Передать сообщение другому агенту через супергруппу. НЕ отправляет сразу: "
        "владельцу уходит карточка в топик approvals с кнопками «Отправить»/«Отклонить». "
        "Вызывать можно свободно — без нажатия кнопки ничего не уйдёт."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "target": {"type": "string", "description": "Ключ маршрута из relay_routes"},
            "text": {"type": "string", "description": "Текст сообщения (без секретов)"},
        },
        "required": ["target", "text"],
    },
}

_PROTOCOL_SCHEMA = {
    "name": "relay_protocol",
    "description": ("Протокол tg-gateway в формате для агента: какие сообщения приходят, "
                    "как отвечать, какие методы и правила. Вызови, если не уверен в формате."),
    "parameters": {"type": "object", "properties": {}, "required": []},
}

_STATUS_SCHEMA = {
    "name": "relay_status",
    "description": ("Статус задачи в хабе (A2A tasks/get): состояние, вопрос, артефакт. "
                    "Работает только в endpoint-режиме."),
    "parameters": {
        "type": "object",
        "properties": {"task_id": {"type": "string", "description": "task_id от relay_send"}},
        "required": ["task_id"],
    },
}

_CANCEL_SCHEMA = {
    "name": "relay_cancel",
    "description": (
        "Отменить свою задачу (только инициатор). Владельцу уходит карточка "
        "«Отменить?»; после подтверждения пиру отправляется уведомление об отмене."
    ),
    "parameters": {
        "type": "object",
        "properties": {"task_id": {"type": "string", "description": "task_id из relay_send"}},
        "required": ["task_id"],
    },
}

_ASK_SCHEMA = {
    "name": "ask_default",
    "description": (
        "Спросить свой default-профиль (мозги агента) и получить текстовый ответ. "
        "Вопрос уходит как внешний запрос через gateway."
    ),
    "parameters": {
        "type": "object",
        "properties": {"question": {"type": "string", "description": "Вопрос default-профилю"}},
        "required": ["question"],
    },
}

_INBOX_SCHEMA = {
    "name": "gateway_inbox",
    "description": (
        "Принятые владельцем сообщения от других агентов (что пришло через gateway). "
        "ack=true очищает инбокс после прочтения."
    ),
    "parameters": {
        "type": "object",
        "properties": {"ack": {"type": "boolean", "description": "Пометить прочитанным"}},
        "required": [],
    },
}


def _telegram_callback_factory(native, adapter):
    """Owner's card buttons. Scoped by pattern so core button flows keep working."""
    from telegram.ext import CallbackQueryHandler

    async def _on_callback(update, context):
        query = getattr(update, "callback_query", None)
        if query is None:
            return
        payload = query.data or ""
        if not payload.startswith("gw"):
            return
        message = getattr(query, "message", None)
        await asyncio.to_thread(
            core.resolve_card_click, payload,
            str(getattr(query.from_user, "id", "")), query.id,
            str(getattr(message, "chat_id", "")), str(getattr(message, "message_id", "")),
        )

    native.add_handler(CallbackQueryHandler(_on_callback, pattern=r"^gw(out|in):"))


def register(ctx) -> None:
    # The ingress copy (launch profile home) only needs the inbound hook.
    ctx.register_hook("pre_gateway_dispatch", core.pre_gateway_dispatch)
    ctx.register_telegram_handler(_telegram_callback_factory)

    home = Path(__file__).resolve().parents[2]
    if core._home() != home:
        ctx.register_tool(name="gateway_inbox", toolset="gateway_relay",
                          schema=_INBOX_SCHEMA, handler=core.tool_inbox,
                          description="Owner-approved inbound messages", emoji="\U0001f4e5")
        return

    ctx.register_tool(name="relay_routes", toolset="gateway_relay",
                      schema=_ROUTES_SCHEMA, handler=core.tool_routes,
                      description="Allowlisted relay destinations", emoji="\U0001f517")
    ctx.register_tool(name="relay_send", toolset="gateway_relay",
                      schema=_SEND_SCHEMA, handler=core.tool_send,
                      description="Relay via an owner-approved card", emoji="\u2709")
    ctx.register_tool(name="relay_protocol", toolset="gateway_relay",
                      schema=_PROTOCOL_SCHEMA, handler=core.tool_protocol,
                      description="Agent-facing protocol text", emoji="\U0001f4d8")
    ctx.register_tool(name="relay_status", toolset="gateway_relay",
                      schema=_STATUS_SCHEMA, handler=core.tool_status,
                      description="Task status via hub", emoji="\U0001f50e")
    ctx.register_tool(name="relay_cancel", toolset="gateway_relay",
                      schema=_CANCEL_SCHEMA, handler=core.tool_cancel,
                      description="Cancel a submitted task", emoji="\u21a9")
    ctx.register_tool(name="ask_default", toolset="gateway_relay",
                      schema=_ASK_SCHEMA, handler=core.tool_ask_default,
                      description="Ask this agent's default profile", emoji="\U0001f9e0")
    core.start_watcher(ctx)
