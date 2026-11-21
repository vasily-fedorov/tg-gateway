#!/usr/bin/env python3
"""Hub: Telegram-бот, который держит маршруты, подтверждения и аудит.

Схема (MVP):
    агент A ──JSON──▶ hub ──карточка──▶ владелец ──клик──▶ hub ──JSON──▶ агент B
                          ▲                                              │
                          └──────────── JSON reply (done/skip/ask) ◀─────┘

Карточки живут в ЛС с хабом; агенты общаются с хабом тоже в ЛС (bot-to-bot).
"""
from __future__ import annotations

import json
import os
import pathlib
import secrets
import sys
import time
from typing import Any, Dict, Iterable, Optional

from . import commands, policy, protocol, rpc
from .db import CARD_TTL_S, TERMINAL, Db
from .tg import Tg

HELP = (
    "TG Gateway — шлюз между вашими ботами.\n\n"
    "/pair <алиас> — код подключения бота (отправить код боту как /pair <код>)\n"
    "/bots — подключённые боты\n"
    "/routes — маршруты и политика\n"
    "/route <от> <кому> ask|auto — политика подтверждений ('-' = вы)\n"
    "/cancel <задача> — отменить задачу\n"
    "/unpair <алиас> — отключить бота (бот освобождается)\n"
    "/describe <алиас> <текст> — что бот умеет (пойдёт в agent card)\n"
    "/card [алиас] — A2A-карточка хаба или бота\n"
    "/log [алиас] [N] — последние задачи (пришедшие и отправленные)\n"
    "/task <id> — детали задачи и её события\n"
    "/protocol — протокол в формате для агента (что от него ждут)\n"
    "/help — это сообщение\n\n"
    "Всё, что бот присылает вам на подтверждение, приходит карточкой сюда.\n"
    "Ответ на вопрос бота — просто текстом в ответ на карточку."
)


def new_id(n: int = 3) -> str:
    return secrets.token_hex(n)


def envelope(**payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False)


class Hub:
    def __init__(self, token: str, db_path: pathlib.Path | str, owner_ids: Iterable[int] = (),
                 *, tg: Optional[Tg] = None, clock=time.time, open_registration: bool = False,
                 proxy: str | None = None):
        self.tg = tg or Tg(token, proxy=proxy)
        self.db = Db(db_path)
        self.owners = {int(x) for x in owner_ids}
        self.open_registration = bool(open_registration)
        self.clock = clock
        self._last_refusal = ""

    # ── основной цикл ────────────────────────────────────────────────────────
    def run(self, *, max_updates: Optional[int] = None) -> None:
        offset: Optional[int] = None
        handled = 0
        while True:
            for update in self.tg.get_updates(offset):
                offset = int(update["update_id"]) + 1
                try:
                    self.handle(update)
                except Exception as exc:  # noqa: BLE001 — хаб не должен падать
                    self.db.event("hub_error", error=f"{type(exc).__name__}: {exc}"[:300])
                handled += 1
                if max_updates and handled >= max_updates:
                    return
            self.expire_all()

    def expire_all(self) -> None:
        for row in self.db.q("SELECT tg_id FROM owners"):
            owner = int(row["tg_id"])
            expired = list(self.db.expire(owner) or [])
            for task in expired:
                parked = self.db.one(
                    "SELECT 1 FROM events WHERE task_id=? AND kind='card_expired_peer'",
                    (task["task_id"],))
                if not parked:
                    continue
                self.say(owner, f"⌛ Получатель не ответил за 20 минут: задача {task['task_id']} "
                                f"→ {task['dst_alias']} не доставлена.")

    # ── диспетчер ────────────────────────────────────────────────────────────
    def handle(self, update: Dict[str, Any]) -> None:
        if "callback_query" in update:
            return self.on_callback(update["callback_query"])
        msg = update.get("message") or {}
        text = (msg.get("text") or "").strip()
        frm = msg.get("from") or {}
        chat = msg.get("chat") or {}
        uid = int(frm.get("id") or 0)
        chat_id = chat.get("id")
        if frm.get("is_bot"):
            return self.on_agent(uid, frm.get("username") or str(uid), text, chat_id)
        if chat.get("type") != "private":
            return None
        if uid in self.owners or self.open_registration:
            # Самостоятельная регистрация: у каждого владельца свой namespace
            # (bots/routes/tasks фильтруются по owner_tg), чужие данные не видны.
            self.db.ensure_owner(uid)
            return self.on_owner(uid, text)
        self.say(chat_id, "Этот шлюз закрыт: доступ только у владельца, "
                          "указанного в TG_GATEWAY_OWNERS.")

    # ── владелец ─────────────────────────────────────────────────────────────
    def on_owner(self, uid: int, text: str) -> None:
        self.db.ensure_owner(uid)
        low = text.lower()
        if low in ("/start", "/help", "help", "помощь"):
            return self.say(uid, commands.help_text())
        if low == "/menu":
            return self.send_menu_section(uid, uid, None, "main")
        if low.startswith("/pair"):
            parts = text.split()
            if len(parts) < 2:
                return self.say(uid, "Использование: /pair <алиас>, например /pair aa")
            alias = parts[1].strip()[:20]
            code = "HUB-" + secrets.token_hex(2).upper()
            self.db.add_pair_code(uid, alias, code)
            return self.say(uid, f"Код для бота «{alias}»:\n\n/pair {code} {alias}\n\n"
                                 "Отправьте эту строку в ЛС нужному боту-агенту.\n"
                                 "Код одноразовый, живёт до первого использования.")
        if low.startswith("/bots"):
            rows = self.db.bots(uid)
            if not rows:
                return self.say(uid, "Пока никто не подключён. /pair <алиас>")
            lines = [f"• {r['alias']} — @{r['username']} (id {r['bot_id']})" for r in rows]
            return self.say(uid, "Подключённые боты:\n" + "\n".join(lines))
        if low.startswith("/routes"):
            rows = self.db.routes(uid)
            if not rows:
                return self.say(uid, "Маршрутов нет. /route <от> <кому> ask|auto")
            lines = [f"• {r['src_alias'] or 'вы'} → {r['dst_alias']}: {r['policy']} "
                     f"({r['per_hour']}/час)" for r in rows]
            return self.say(uid, "Маршруты:\n" + "\n".join(lines))
        if low.startswith("/route"):
            parts = text.split()
            if len(parts) < 4:
                return self.say(uid, "Использование: /route <от> <кому> ask|auto")
            src = "" if parts[1] in ("-", "вы", "me") else parts[1]
            dst, pol = parts[2], parts[3].lower()
            if pol not in ("ask", "auto"):
                return self.say(uid, "Политика: ask или auto")
            self.db.set_route(uid, src, dst, policy=pol)
            return self.say(uid, f"Маршрут {src or 'вы'} → {dst}: {pol}")
        if low.startswith("/protocol"):
            return self.say(uid, protocol.AGENT_PROTOCOL)
        if low.startswith("/log"):
            parts = text.split()
            alias = parts[1].strip() if len(parts) > 1 else ""
            limit = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 15
            if alias:
                rows = self.db.q(
                    "SELECT * FROM tasks WHERE owner_tg=? AND (src_alias=? OR dst_alias=?) "
                    "ORDER BY created_at DESC LIMIT ?", (uid, alias, alias, min(limit, 50)))
            else:
                rows = self.db.q("SELECT * FROM tasks WHERE owner_tg=? ORDER BY created_at DESC "
                                 "LIMIT ?", (uid, min(limit, 50)))
            if not rows:
                return self.say(uid, "Задач нет.")
            lines = []
            for r in rows:
                when = time.strftime("%H:%M", time.localtime(float(r["created_at"])))
                text_short = (r["text"] or r["question"] or "").replace("\n", " ")[:40]
                lines.append(f"{r['task_id']}  {r['src_alias']}→{r['dst_alias']}  "
                             f"{r['status']}  h{r['hops']}  {when}  «{text_short}»")
            self.db.event("owner_log", owner=uid, alias=alias or "*", count=len(rows))
            return self.say(uid, "```\n" + "\n".join(lines) + "\n```")
        if low.startswith("/task"):
            parts = text.split()
            if len(parts) < 2:
                return self.say(uid, "Использование: /task <task_id>")
            task = self.db.task(parts[1].strip())
            if not task or int(task["owner_tg"]) != uid:
                return self.say(uid, "Задача не найдена.")
            return self.say(uid, self._task_text(task, body_limit=3000))
        if low.startswith("/describe"):
            parts = text.split(maxsplit=2)
            if len(parts) == 1:
                rows = self.db.bots(uid)
                if not rows:
                    return self.say(uid, "Ботов нет. /pair <алиас>")
                lines = [f"• {r['alias']} — {(r['description'] or '').strip() or '— нет описания —'}"
                         for r in rows]
                return self.say(uid, "Описания (для agent card):\n" + "\n".join(lines) +
                                "\n\nЗадать: /describe <алиас> <что умеет>")
            alias = parts[1].strip()
            bot = self.db.bot_by_alias(uid, alias)
            if not bot:
                return self.say(uid, f"Бота «{alias}» нет. /bots")
            if len(parts) == 2 or not parts[2].strip():
                current = (bot["description"] or "").strip() or "— нет описания —"
                return self.say(uid, f"«{alias}»: {current}\n\n"
                                     f"Задать: /describe {alias} <что умеет>")
            if not self.db.set_description(uid, alias, parts[2].strip()):
                return self.say(uid, "Такого бота нет.")
            self.db.event("bot_described", owner=uid, alias=alias)
            return self.say(uid, f"✅ «{alias}» умеет: {parts[2].strip()[:120]}\n"
                                 "Это попадёт в agent card (/card " + alias + ").")
        if low.startswith("/card"):
            parts = text.split()
            if len(parts) > 1:
                peer = self.db.bot_by_alias(uid, parts[1].strip())
                if not peer:
                    return self.say(uid, "Такого бота нет.")
                return self.say(uid, rpc.bot_card(peer, uid)["description"] +
                                "\n\n" + envelope(**rpc.bot_card(peer, uid)))
            card = rpc.hub_card(uid, self.db.bots(uid))
            return self.say(uid, object.__str__(card["description"]) + "\n\n" +
                            envelope(**card))
        if low.startswith("/unpair"):
            parts = text.split()
            if len(parts) < 2:
                return self.say(uid, "Использование: /unpair <алиас>")
            bot = self.db.bot_by_alias(uid, parts[1].strip())
            if not bot:
                return self.say(uid, "Такого бота нет.")
            self.db.set_enabled(int(bot["bot_id"]), False)
            self.db.event("bot_unpaired", owner=uid, alias=parts[1])
            return self.say(uid, f"Бот «{parts[1]}» отключён.")
        if low.startswith("/cancel"):
            parts = text.split()
            if len(parts) < 2:
                if self.db.get_state(uid):
                    self.db.clear_state(uid)
                    return self.say(uid, "Ввод отменён.")
                return self.say(uid, "Использование: /cancel <task_id>")
            return self.cancel_task(uid, parts[1])
        # ввод для начатого в меню действия (кнопки не умеют принимать текст)
        if not text.startswith("/"):
            st = self.db.get_state(uid)
            if st:
                return self.handle_state_input(uid, str(st["action"]), str(st["payload"] or ""), text)

        # ответ на висящий вопрос агента
        pending = self.db.q("SELECT * FROM tasks WHERE owner_tg=? AND status='input-required' "
                            "ORDER BY created_at DESC LIMIT 1", (uid,))
        if pending and text:
            task = pending[0]
            self.db.update_task(task["task_id"], status="working", answer=text)
            self.db.event("ask_answered", task_id=task["task_id"], owner=uid,
                          length=len(text))
            self.send_to_agent(task["dst_alias"], task["owner_tg"],
                               envelope(t="answer", task=task["task_id"], text=text))
            return self.say(uid, "Ответ передан агенту.")
        self.say(uid, "Не понял. /help")

    def cancel_task(self, uid: int, task_id: str) -> None:
        task = self.db.task(task_id)
        if not task or int(task["owner_tg"]) != uid:
            return self.say(uid, "Задача не найдена.")
        if task["status"] in TERMINAL:
            return self.say(uid, f"Задача уже {task['status']}.")
        self.db.update_task(task_id, status="cancelled")
        self.db.event("task_cancelled", task_id=task_id, owner=uid)
        if task["dst_alias"]:
            self._notify_cancel(task["dst_alias"], uid, task_id)
        self.say(uid, f"Задача {task_id} отменена, агент уведомлён.")

    # ── агент ────────────────────────────────────────────────────────────────
    def on_agent(self, bot_id: int, username: str, text: str, chat_id: Any) -> None:
        bot = self.db.bot_by_id(bot_id)
        if not bot:
            if text.lower().startswith("/pair"):
                return self.pair_bot(bot_id, username, text)
            return self.say(chat_id or bot_id, "Бот не подключён. Владелец: /pair <алиас>")
        if not text.startswith("{"):
            return self.say(bot_id, rpc.rpc_err(None, rpc.ERR_PARSE,
                                                "нужен JSON-RPC 2.0"))
        try:
            env = json.loads(text)
        except ValueError:
            return self.say(bot_id, "Битый JSON.")
        if "method" not in env:
            return self.say(bot_id, rpc.rpc_err(env.get("id"), rpc.ERR_INVALID_REQUEST,
                                                "нужен JSON-RPC 2.0 (method/params)"))
        return self.on_rpc(bot, env)

    def install_commands(self) -> bool:
        """Подсказки команд в Telegram: без этого «/» ничего не автодополняет."""
        ok, info = self.tg.call("setMyCommands", commands=json.dumps(commands.bot_commands(),
                                                                   ensure_ascii=False))
        self.db.event("commands_published", ok=bool(ok), error=None if ok else str(info)[:200])
        return bool(ok)

    def _notify_cancel(self, alias: str, owner: int, task_id: str) -> bool:
        payload = {"jsonrpc": "2.0", "id": "c-" + new_id(3), "method": "tasks/cancel",
                   "params": {"id": task_id, "metadata": {"task": task_id}}}
        return self.send_to_agent(alias, owner, json.dumps(payload, ensure_ascii=False))

    # ── JSON-RPC (A2A-форма поверх Telegram) ─────────────────────────────────
    def on_rpc(self, bot: Any, env: Dict[str, Any]) -> None:
        bot_id = int(bot["bot_id"])
        rpc_id = env.get("id")
        method = str(env.get("method") or "")
        params = env.get("params") or {}
        owner = int(bot["owner_tg"])

        if method in ("agent/protocol", "agent/help"):
            return self.say(bot_id, rpc.rpc_ok(rpc_id, {"protocol": protocol.AGENT_PROTOCOL}))

        if method in ("agent/card", "agent/getCard", "a2a/discover"):
            target = str(params.get("alias") or "").strip()
            if target:
                peer = self.db.bot_by_alias(owner, target)
                if not peer:
                    return self.say(bot_id, rpc.rpc_err(rpc_id, rpc.ERR_TASK_NOT_FOUND,
                                                        f"нет бота «{target}»"))
                me = self.db.bot_by_id(bot_id)
                return self.say(bot_id, rpc.rpc_ok(rpc_id, {
                    "card": rpc.bot_card(peer, owner),
                    "routes": self.routes_for_bot(owner, str(me["alias"])) if me else []}))
            # Компактный ответ: карточка спрашивающего + агенты + маршруты.
            # Полные карточки всех ботов не шлём — сообщение Telegram ограничено 4096
            # символами, а обрезанный JSON ломает разбор на стороне агента.
            me = self.db.bot_by_id(bot_id)
            bots = self.db.bots(owner)
            return self.say(bot_id, rpc.rpc_ok(rpc_id, {
                "card": rpc.bot_card(me, owner) if me else {},
                "agents": self.visible_agents(owner, str(me["alias"])) if me else [],
                "routes": self.routes_for_bot(owner, str(me["alias"])) if me else [],
                "protocolVersion": rpc.PROTOCOL_VERSION}))

        if method in ("message/send", "message/stream"):
            msg = params.get("message") or {}
            parts = msg.get("parts") or []
            text_parts, attachments = [], []
            for part in parts:
                kind = str(part.get("kind") or "text")
                if kind == "text":
                    text_parts.append(str(part.get("text") or ""))
                elif kind in ("data", "file"):
                    # в теле — только текст; data/file едут отдельными parts
                    attachments.append(part)
            text = "\n".join(x.strip() for x in text_parts if x.strip()).strip()
            message_id = str(msg.get("messageId") or "")
            ctx = str(msg.get("contextId") or "")
            meta = params.get("metadata") or {}
            hops = int(meta.get("hops") or 1)
            # TTL конверта (A2A-TG): просроченное не принимаем
            ttl_err = policy.ttl_error(sent_at=meta.get("sent_at"),
                                      ttl_seconds=params.get("ttl_seconds") or meta.get("ttl_seconds"),
                                      now=self.clock())
            if ttl_err:
                self.db.event("ttl_rejected", owner=owner, reason=ttl_err)
                return self.say(bot_id, rpc.rpc_err(rpc_id, rpc.ERR_INVALID_PARAMS, ttl_err))
            to_alias = str(msg.get("to") or params.get("to") or "").strip()
            if not to_alias:
                return self.say(bot_id, rpc.rpc_err(rpc_id, rpc.ERR_INVALID_PARAMS,
                                                    "нужен params.message.to (алиас)"))
            fp = policy.fingerprint(src=str(bot["alias"]), dst=to_alias,
                                    text=text + json.dumps(attachments, ensure_ascii=False,
                                                           sort_keys=True))
            if message_id:
                seen = self.db.seen_message(message_id)
                if seen:
                    task = self.db.task(seen)
                    self.db.event("rpc_dedup", task_id=seen, owner=owner)
                    if task:
                        return self.say(bot_id, rpc.rpc_ok(rpc_id, {"task": rpc.task_view(task),
                                                                   "deduplicated": True}))
            dup = self.db.seen_fingerprint(fp, window_s=policy.FINGERPRINT_WINDOW_S)
            if dup:
                task = self.db.task(dup)
                self.db.event("rpc_dedup_fingerprint", task_id=dup, owner=owner)
                if task:
                    return self.say(bot_id, rpc.rpc_ok(rpc_id, {"task": rpc.task_view(task),
                                                               "deduplicated": True}))
            before = {r["task_id"] for r in self.db.q(
                "SELECT task_id FROM tasks WHERE owner_tg=? ORDER BY created_at DESC LIMIT 5",
                (owner,))}
            self.on_relay(bot, {"to": to_alias, "text": text, "ctx": ctx, "hops": hops,
                                "attachments": attachments})
            fresh = [r for r in self.db.q(
                "SELECT * FROM tasks WHERE owner_tg=? ORDER BY created_at DESC LIMIT 3", (owner,))
                if r["task_id"] not in before]
            if not fresh:
                return self.say(bot_id, rpc.rpc_err(rpc_id, rpc.ERR_INVALID_REQUEST,
                                                    self._last_refusal or "запрос отклонён"))
            task = fresh[0]
            if message_id:
                self.db.remember_message(message_id, task["task_id"], fp)
            return self.say(bot_id, rpc.rpc_ok(rpc_id, {"task": rpc.task_view(task)}))

        if method in ("tasks/update", "tasks/status"):
            # Внутренний канал исполнитель → хаб (не публичный A2A-метод).
            task_id = str(params.get("task") or params.get("id") or "")
            task = self.db.task(task_id)
            if not task or int(task["owner_tg"]) != owner:
                return self.say(bot_id, rpc.rpc_err(rpc_id, rpc.ERR_TASK_NOT_FOUND,
                                                    "задача не найдена"))
            status = params.get("status") or {}
            # Принимаем и spec-вид (TASK_STATE_WORKING), и короткий (working).
            state = str(status.get("state") or "").replace("TASK_STATE_", "").lower()
            state = state.replace("_", "-")
            text = ""
            for part in (params.get("artifact") or {}).get("parts") or []:
                if str(part.get("kind") or "text") == "text":
                    text += str(part.get("text") or "")
            if not text:
                for part in (status.get("message") or {}).get("parts") or []:
                    if str(part.get("kind") or "text") == "text":
                        text += str(part.get("text") or "")
            text = text.strip()
            if state in ("working", "submitted", "auth-required"):
                # Прогресс/хартбит: задача остаётся живой, карточку не трогаем
                if str(task["status"]) != "submitted":
                    self.db.heartbeat(task_id, text)
                    if text:
                        self.db.add_message(task_id, "agent", text)
                    self.db.event("task_progress", task_id=task_id, owner=owner,
                                  chars=len(text))
                return self.say(bot_id, rpc.rpc_ok(rpc_id,
                                                   {"task": rpc.task_view(self.db.task(task_id))}))
            if state == "input-required":
                question = text[:1500]
                self.db.update_task(task_id, status="input-required", question=question)
                self.db.add_message(task_id, "agent", question)
                self.db.event("task_input_required", task_id=task_id, owner=owner, via="rpc")
                self.tg.send(owner, f"Агент «{task['dst_alias']}» спрашивает:\n\n"
                                    f"{question}\n\nОтветьте текстом.")
                return self.say(bot_id, rpc.rpc_ok(rpc_id,
                                                   {"task": rpc.task_view(self.db.task(task_id))}))
            if state in ("failed", "canceled", "cancelled", "rejected"):
                final = "cancelled" if state.startswith("cancel") else state
                self.db.update_task(task_id, status=final, progress=text[:300])
                if text:
                    self.db.add_message(task_id, "agent", text)
                self.db.event(f"task_{final}", task_id=task_id, owner=owner, via="rpc")
                return self.say(bot_id, rpc.rpc_ok(rpc_id,
                                                   {"task": rpc.task_view(self.db.task(task_id))}))
            if str(task["status"]) in TERMINAL:
                # Поздний/повторный апдейт по закрытой задаче: только фиксируем, без последствий.
                self.db.event("late_update_ignored", task_id=task_id, owner=owner,
                              status=str(task["status"]), chars=len(text))
                return self.say(bot_id, rpc.rpc_ok(rpc_id,
                                                   {"task": rpc.task_view(self.db.task(task_id))}))
            # completed либо legacy-вызов без state: текст = финальный ответ
            verdict = "done" if (text or state == "completed") else "skip"
            self.on_reply(bot, {"t": "reply", "task": task_id, "verdict": verdict,
                                "text": text, "question": ""})
            return self.say(bot_id, rpc.rpc_ok(rpc_id,
                                               {"task": rpc.task_view(self.db.task(task_id))}))

        if method in ("tasks/list", "tasks/poll"):
            alias = str(bot["alias"])
            rows = self.db.list_tasks(
                owner,
                status=str(params.get("status") or "").replace("TASK_STATE_", "").lower(),
                ctx=str(params.get("contextId") or params.get("ctx") or ""),
                since=float(params.get("since") or 0),
                cursor=float(params.get("cursor") or 0),
                limit=int(params.get("limit") or params.get("pageSize") or 50))
            # Клиент опрашивает свои задачи: видимые ему — где он источник или адресат
            rows = [r for r in rows if alias in (r["src_alias"], r["dst_alias"])] or rows
            nxt = min([float(r["updated_at"]) for r in rows], default=0.0)
            return self.say(bot_id, rpc.rpc_ok(rpc_id, {
                "tasks": [rpc.task_view(r, messages=self.db.messages(r["task_id"], 5))
                          for r in rows],
                "nextCursor": nxt, "cursor": nxt}))

        if method == "tasks/get":
            task = self.db.task(str(params.get("id") or params.get("taskId") or ""))
            if not task or int(task["owner_tg"]) != owner:
                return self.say(bot_id, rpc.rpc_err(rpc_id, rpc.ERR_TASK_NOT_FOUND,
                                                    "задача не найдена"))
            hist = params.get("historyLength")
            limit = 20 if hist is None else int(hist)
            return self.say(bot_id, rpc.rpc_ok(rpc_id, {
                "task": rpc.task_view(task, history_length=limit,
                                      messages=self.db.messages(task["task_id"], max(limit, 1) * 2))}))

        if method == "tasks/cancel":
            task = self.db.task(str(params.get("id") or params.get("taskId") or ""))
            if not task or int(task["owner_tg"]) != owner:
                return self.say(bot_id, rpc.rpc_err(rpc_id, rpc.ERR_TASK_NOT_FOUND,
                                                    "задача не найдена"))
            if str(task["status"]) in TERMINAL:
                return self.say(bot_id, rpc.rpc_err(rpc_id, rpc.ERR_TASK_NOT_CANCELABLE,
                                                    f"задача уже {task['status']}"))
            self.db.update_task(task["task_id"], status="cancelled")
            self.db.event("task_cancelled", task_id=task["task_id"], owner=owner, via="rpc")
            if task["dst_alias"]:
                self._notify_cancel(str(task["dst_alias"]), owner, task["task_id"])
            return self.say(bot_id, rpc.rpc_ok(rpc_id,
                                               {"task": rpc.task_view(self.db.task(task["task_id"]))}))

        if method == "ping":
            return self.say(bot_id, rpc.rpc_ok(rpc_id, {"pong": True, "ts": rpc._iso(None)}))

        return self.say(bot_id, rpc.rpc_err(rpc_id, rpc.ERR_METHOD_NOT_FOUND, method))

    def pair_bot(self, bot_id: int, username: str, text: str) -> None:
        parts = text.split()
        if len(parts) < 2:
            return self.say(bot_id, "Использование: /pair <код> [алиас]")
        row = self.db.take_pair_code(parts[1])
        if not row:
            return self.say(bot_id, "Код не найден или уже использован.")
        alias = (parts[2] if len(parts) > 2 else row["alias"]).strip()[:20]
        current = self.db.bot_owner(bot_id)
        if current is not None and current != int(row["owner_tg"]):
            self.db.event("pair_conflict", owner=int(row["owner_tg"]), bot_id=bot_id)
            return self.say(bot_id, "Бот уже подключён к другому владельцу.")
        self.db.pair_bot(bot_id=bot_id, username=username, alias=alias,
                         owner=int(row["owner_tg"]))
        self.db.event("bot_paired", owner=int(row["owner_tg"]), bot_id=bot_id, alias=alias)
        self.say(bot_id, f"Подключено как «{alias}».")
        self.say(int(row["owner_tg"]), f"✅ Подключён бот @{username} как «{alias}».\n"
                                      f"Маршрут: /route - {alias} ask")

    # ── ретрансляция ─────────────────────────────────────────────────────────
    def on_relay(self, bot: Any, env: Dict[str, Any]) -> None:
        owner = int(bot["owner_tg"])
        dst_alias = str(env.get("to") or "").strip()
        text = str(env.get("text") or "").strip()
        ctx = str(env.get("ctx") or "") or None
        hops = int(env.get("hops") or 1)
        target = self.db.bot_by_alias(owner, dst_alias)
        if not target:
            return self.say(int(bot["bot_id"]), f"Нет бота «{dst_alias}» у владельца.")
        if (str(bot["routes_mode"] or "explicit") == "explicit"
                and not self.db.route(owner, str(bot["alias"]), dst_alias)):
            self.db.event("hidden_dst_refused", owner=owner, src=str(bot["alias"]), dst=dst_alias)
            return self.say(int(bot["bot_id"]), f"Нет маршрута к «{dst_alias}».")
        if not policy.hop_allowed(hops):
            self.db.event("hop_limit", owner=owner, hops=hops, src=bot["alias"], dst=dst_alias)
            return self.say(int(bot["bot_id"]), f"Предел глубины ({policy.MAX_HOPS}).")
        route = self.db.route(owner, bot["alias"], dst_alias)
        max_text = int(route["max_text"]) if route else policy.MAX_TEXT
        per_hour = int(route["per_hour"]) if route else policy.PER_HOUR
        rpolicy = str(route["policy"]) if route else "ask"
        err = policy.check_limits(text, max_text=max_text, per_hour=per_hour,
                                  used_hour=self.db.hours_used(owner, bot["alias"], dst_alias))
        if err:
            self._last_refusal = err
            self.db.event("refused", owner=owner, reason=err, src=bot["alias"], dst=dst_alias)
            return self.say(int(bot["bot_id"]), f"Отказ: {err}")
        task_id = new_id()
        ctx = ctx or new_id(4)
        approve = policy.needs_approval(policy=rpolicy, ctx=ctx if hops > 1 else "",
                                        hops=hops, direction="relay")
        stored_dst = str(target["alias"])
        self.db.create_task(task_id=task_id, owner=owner, direction="relay", ctx=ctx,
                            hops=hops, src=bot["alias"], dst=stored_dst, text=text,
                            status="submitted" if approve else "working",
                            attachments=json.dumps(env.get("attachments") or [],
                                                   ensure_ascii=False))
        self.db.event("relay_parked" if approve else "relay_auto", task_id=task_id, owner=owner,
                      src=bot["alias"], dst=dst_alias, hops=hops, ctx=ctx, length=len(text))
        if approve:
            ok, res = self.tg.send(owner, self.card_text(bot["alias"], stored_dst, text, task_id,
                                                         ctx, hops),
                                   [[("Отправить", f"hub:ok:{task_id}"),
                                     ("Отклонить", f"hub:no:{task_id}")]])
            if ok and isinstance(res, dict):
                self.db.update_task(task_id, card_chat=owner, card_msg=res.get("message_id"))
            return
        self.after_src_approval(task_id)

    def after_src_approval(self, task_id: str) -> None:
        """Исходящая сторона согласовала — доставляем получателю."""
        self.deliver(task_id)

    def card_text(self, src: str, dst: str, text: str, task_id: str, ctx: str, hops: int) -> str:
        preview = text.replace("\n", " ")[:300]
        return (f"Переслать агенту «{dst}» от «{src}»?\n\n{preview}\n\n"
                f"· задача {task_id} · ctx {ctx} · hop {hops}")

    def deliver(self, task_id: str) -> bool:
        task = self.db.task(task_id)
        if not task:
            return False
        target = self.db.bot_by_alias(int(task["owner_tg"]), task["dst_alias"])
        if not target and str(task["dst_alias"] or "").startswith("@"):
            target = self.db.bot_by_username(task["dst_alias"])
        if not target:
            self.db.update_task(task_id, status="failed")
            self.db.event("deliver_failed", task_id=task_id, reason="no_target")
            return False
        dst_alias = str(task["dst_alias"])
        blocked_until = self.db.peer_blocked(dst_alias)
        if blocked_until > self.clock():
            self.db.update_task(task_id, status="failed")
            self.db.event("peer_blocked", task_id=task_id, to=dst_alias, until=blocked_until)
            return False
        hops = int(task["hops"]) + 1
        if hops > policy.MAX_HOPS:
            # Бюджет глубины: 4-е сообщение в цепочке не доставляем вообще.
            self.db.update_task(task_id, status="failed")
            self.db.event("hop_limit", task_id=task_id, hops=hops, owner=int(task["owner_tg"]))
            self.say(int(task["owner_tg"]),
                     f"⛔ Предел глубины ({policy.MAX_HOPS}): задача {task_id} остановлена.")
            return False
        parts = [{"kind": "text", "text": task["text"] or ""}]
        try:
            parts.extend(json.loads(task["attachments"] or "[]"))
        except ValueError:
            pass
        turns = self.context_digest(task)
        if turns:
            parts.append({"kind": "data", "data": {"context": turns}})
        payload = {
            "jsonrpc": "2.0", "id": "d-" + new_id(4), "method": "message/send",
            "params": {
                "message": {"role": "user", "messageId": "m-" + task_id,
                            "contextId": task["ctx"], "parts": parts},
                "metadata": {"task": task_id, "hops": hops, "from": task["src_alias"],
                             "sent_at": self.clock(), "contextTurns": len(turns),
                             # ответ на задачу: endpoint не должен снова запускать ход
                             "kind": "answer" if str(task["direction"]) == "reply" else "request",
                             "protocolVersion": rpc.PROTOCOL_VERSION},
            },
        }
        ok, res = self.tg.send(int(target["bot_id"]),
                               json.dumps(payload, ensure_ascii=False))
        status = "working" if ok else "failed"
        self.db.update_task(task_id, status=status, hops=hops)
        if ok:
            self.db.peer_ok(dst_alias)
        elif self.db.peer_failed(dst_alias, str(res)):
            self.db.event("peer_circuit_open", task_id=task_id, to=dst_alias)
        self.db.event("delivered" if ok else "deliver_failed", task_id=task_id,
                      to=task["dst_alias"], hops=hops, error=None if ok else str(res)[:200])
        return bool(ok)

    def context_digest(self, task: Any) -> list:
        """Выжимка предыдущих ходов ветки (contextId) — хаб как оркестратор A2A.

        Не общая память: только эта ветка, только для получателя, лимит 5 ходов и
        240 символов на поле. Выключается маршрутом (`context_mode='off'`).
        """
        owner = int(task["owner_tg"])
        dst = str(task["dst_alias"] or "")
        route = (self.db.route(owner, str(task["src_alias"] or ""), dst)
                 or self.db.route(owner, "", dst))
        mode = str(route["context_mode"] or "") if route else ""
        if mode.strip().lower() == "off":
            return []
        if not str(task["ctx"] or "").strip():
            return []
        rows = self.db.context_turns(owner, str(task["ctx"]), before=float(task["created_at"]))
        turns = []
        for row in reversed(rows):          # хронологический порядок
            question = " ".join(str(row["text"] or "").split())[:240]
            answer = " ".join(str(row["answer"] or "").split())[:240]
            if not question and not answer:
                continue
            turns.append({"from": row["src_alias"], "to": row["dst_alias"],
                          "question": question, "answer": answer,
                          "state": row["status"]})
        return turns

    # ── ответ агента ─────────────────────────────────────────────────────────

    def _bot_mode(self, owner: int, alias: str) -> str:
        bot = self.db.bot_by_alias(owner, alias)
        return str(bot["routes_mode"] or "explicit") if bot else "explicit"

    def routes_for_bot(self, owner: int, alias: str) -> list:
        """Куда этому боту можно отправлять.

        `routes_mode='all'` — все включённые боты владельца (текущее поведение);
        `'explicit'` — только те, на кого есть явный маршрут (скрытые остаются
        невидимыми и недоступными).
        """
        if self._bot_mode(owner, alias) == "explicit":
            rows = self.db.q(
                "SELECT b.alias, b.username, b.description, r.policy, r.max_text, "
                "       r.per_hour, r.context_mode, 1 AS explicit "
                "FROM routes r JOIN bots b ON b.owner_tg=r.owner_tg AND b.alias=r.dst_alias "
                "WHERE r.owner_tg=? AND r.src_alias=? AND b.enabled=1 ORDER BY b.alias",
                (owner, alias))
        else:
            rows = self.db.q(
            "SELECT b.alias, b.username, b.description, "
            "       r.policy, r.max_text, r.per_hour, r.context_mode, (r.id IS NOT NULL) AS explicit "
            "FROM bots b LEFT JOIN routes r ON r.owner_tg=b.owner_tg AND r.dst_alias=b.alias "
            "  AND r.src_alias=? "
                "WHERE b.owner_tg=? AND b.enabled=1 AND b.alias<>? ORDER BY b.alias",
                (alias, owner, alias))
        out = []
        for r in rows:
            out.append({"alias": r["alias"], "username": r["username"],
                        "description": r["description"] or "",
                        "policy": r["policy"] or "ask",
                        "max_text": int(r["max_text"] or policy.MAX_TEXT),
                        "per_hour": int(r["per_hour"] or policy.PER_HOUR),
                        "context": ("off" if str(r["context_mode"] or "") == "off" else "on"),
                        "explicit_route": bool(r["explicit"])})
        return out

    def visible_agents(self, owner: int, alias: str) -> list:
        """Кого этот бот имеет право видеть: он сам + разрешённые адресаты."""
        me = self.db.bot_by_alias(owner, alias)
        out = []
        if me:
            out.append({"alias": me["alias"], "username": me["username"],
                        "description": (me["description"] or "")[:160], "self": True})
        for r in self.routes_for_bot(owner, alias):
            out.append({"alias": r["alias"], "username": r["username"],
                        "description": (r["description"] or "")[:160], "self": False})
        return out

    def on_reply(self, bot: Any, env: Dict[str, Any]) -> None:
        """Исполнитель закрыл задачу: фиксируем ответ и отправляем его инициатору."""
        owner = int(bot["owner_tg"])
        task = self.db.task(str(env.get("task") or ""))
        if not task or int(task["owner_tg"]) != owner:
            return self.say(int(bot["bot_id"]), "Задача не найдена.")
        verdict = str(env.get("verdict") or "done").lower()
        text = str(env.get("text") or "").strip()
        task_id = str(task["task_id"])

        if verdict == "skip":
            self.db.update_task(task_id, status="skipped")
            self.db.event("task_skipped", task_id=task_id, owner=owner)
            return
        if verdict == "ask":
            question = str(env.get("question") or text or "").strip()[:1500]
            self.db.update_task(task_id, status="input-required", question=question)
            self.db.add_message(task_id, "agent", question)
            self.db.event("task_input_required", task_id=task_id, owner=owner)
            self.tg.send(owner, f"Агент «{task['dst_alias']}» спрашивает:\n\n{question}\n\n"
                                "Ответьте текстом на это сообщение.")
            return

        # done: ответ фиксируется на самой задаче (artifacts) и уходит инициатору
        self.db.update_task(task_id, status="completed", answer=text, question="")
        self.db.add_message(task_id, "agent", text)
        self.db.event("task_completed", task_id=task_id, owner=owner, length=len(text))

        src = str(task["src_alias"] or "")
        dst = str(task["dst_alias"] or "")
        hops = int(task["hops"] or 1)
        # Петля гасится здесь: ответ на ответ не создаёт новую задачу.
        if not src or src == dst or str(task["direction"]) == "reply":
            return
        if not policy.hop_allowed(hops):
            self.db.event("hop_limit", task_id=task_id, owner=owner, hops=hops + 1)
            return
        back_id = new_id()
        route = self.db.route(owner, dst, src)
        rpolicy = str(route["policy"]) if route else "ask"
        approve = policy.needs_approval(policy=rpolicy, ctx=task["ctx"], hops=hops + 1,
                                        direction="reply")
        self.db.create_task(task_id=back_id, owner=owner, direction="reply", ctx=task["ctx"],
                            hops=hops, src=dst, dst=src, text=text,
                            status="submitted" if approve else "working")
        self.db.event("reply_parked" if approve else "reply_auto", task_id=back_id, owner=owner,
                      src=dst, dst=src, hops=hops + 1)
        if approve:
            ok, res = self.tg.send(owner, self.card_text(dst, src, text, back_id, task["ctx"],
                                                         hops + 1),
                                   [[("Переслать", f"hub:ok:{back_id}"),
                                     ("Отклонить", f"hub:no:{back_id}")]])
            if ok and isinstance(res, dict):
                self.db.update_task(back_id, card_chat=owner, card_msg=res.get("message_id"))
            return
        self.deliver(back_id)

    # ── клик по карточке ─────────────────────────────────────────────────────
    def on_callback(self, cb: Dict[str, Any]) -> None:
        uid = int((cb.get("from") or {}).get("id") or 0)
        data = str(cb.get("data") or "")
        cb_id = str(cb.get("id") or "")
        msg = cb.get("message") or {}
        chat_id = (msg.get("chat") or {}).get("id")
        msg_id = msg.get("message_id")
        if uid not in self.owners and not self.db.owner(uid):
            return self.tg.answer_callback(cb_id, "Нет прав")
        parts = data.split(":")
        if len(parts) < 3 or parts[0] != "hub":
            return self.tg.answer_callback(cb_id, "Непонятная карточка")
        verdict, task_id = parts[1], ":".join(parts[2:])
        if verdict == "menu":          # разделы inline-меню — до всякой работы с задачами
            self.tg.answer_callback(cb_id, "")
            return self.send_menu_section(uid, chat_id, msg_id, task_id)
        if verdict in ("card", "route", "rpol", "rlim", "rmax", "task", "tcancel",
                       "desc", "new", "newr", "rdel", "rdelok", "tfull", "a2apol", "rctx",
                       "rmode"):
            self.tg.answer_callback(cb_id, "")
            if verdict == "card":
                return self.send_bot_card(uid, chat_id, msg_id, task_id)
            if verdict == "route":
                return self.send_route(uid, chat_id, msg_id, task_id)
            if verdict in ("rpol", "rlim", "rmax"):
                return self.toggle_route(uid, chat_id, msg_id, task_id, verdict)
            if verdict == "desc":
                return self.begin_describe(uid, chat_id, msg_id, task_id)
            if verdict == "new":
                return self.begin_new(uid, chat_id, msg_id, task_id)
            if verdict == "newr":
                step, _, value = task_id.partition(":")
                return self.route_wizard(uid, chat_id, msg_id, step, value)
            if verdict == "rdel":
                return self.confirm_route_delete(uid, chat_id, msg_id, task_id)
            if verdict == "rdelok":
                return self.do_route_delete(uid, chat_id, msg_id, task_id)
            if verdict == "tfull":
                return self.send_task_full(uid, chat_id, task_id)
            if verdict == "rctx":
                return self.toggle_context(uid, chat_id, msg_id, task_id)
            if verdict == "rmode":
                return self.toggle_routes_mode(uid, chat_id, msg_id, task_id)
            if verdict == "a2apol":
                return self.toggle_a2a_policy(uid, chat_id, msg_id, task_id)
            if verdict == "task":
                return self.send_task_card(uid, chat_id, msg_id, task_id)
            if verdict == "tcancel":
                return self.cancel_from_menu(uid, chat_id, msg_id, task_id)
        task = self.db.task(task_id)
        if not task:
            self.tg.answer_callback(cb_id, "Задача не найдена")
            return self.finish_card(chat_id, msg_id, "⌛ Задача неактивна.")

        if int(task["owner_tg"]) != uid:  # чужую карточку подтверждать нельзя
            self.db.event("card_unauthorized", task_id=task_id, owner=uid)
            self.tg.answer_callback(cb_id, "Нет прав")
            return self.finish_card(chat_id, msg_id, "⛔ Нет прав.")
        if task["status"] in TERMINAL:
            self.tg.answer_callback(cb_id, "Уже закрыта")
            return self.finish_card(chat_id, msg_id, f"⌛ Задача уже {task['status']}.")
        self.tg.answer_callback(cb_id, "Принято")
        if verdict == "no":
            self.db.update_task(task_id, status="rejected")
            self.db.event("card_rejected", task_id=task_id, owner=uid)
            return self.finish_card(chat_id, msg_id, "❌ Отклонено.")
        self.db.update_task(task_id, status="working")
        self.db.event("card_approved", task_id=task_id, owner=uid)
        ok = self.deliver(task_id)
        self.finish_card(chat_id, msg_id,
                         f"✅ Отправлено → {task['dst_alias']}" if ok else "❌ Не доставлено")

    def begin_describe(self, uid: int, chat: Any, msg_id: Any, alias: str) -> None:
        if not self.db.bot_by_alias(uid, alias):
            return self.send_bot_card(uid, chat, msg_id, alias)
        self.db.set_state(uid, "describe", alias)
        self._show(uid, chat, msg_id,
                   f"Пришлите описание для «{alias}» одним сообщением — что он умеет.\n"
                   "Оно попадёт в agent card. Написать /cancel — отменить.",
                   [[("⬅️ Бот", f"hub:card:{alias}"), ("❌ Закрыть", "hub:menu:close")]])

    def begin_new(self, uid: int, chat: Any, msg_id: Any, kind: str) -> None:
        if kind == "pair":
            self.db.set_state(uid, "pair_alias")
            self._show(uid, chat, msg_id,
                       "Пришлите алиас нового бота (например aa).\n"
                       "Я выдам одноразовый код — его нужно отправить боту сообщением "
                       "«/pair <код> <алиас>». /cancel — отменить.",
                       [[("⬅️ Боты", "hub:menu:bots"), ("❌ Закрыть", "hub:menu:close")]])
            return
        if kind == "rpol":
            policy = "auto" if policy == "ask" else "ask"
        elif kind == "rlim":
            per_hour = next((x for x in (5, 20, 50, 100) if x > per_hour), 5)
        else:
            max_text = next((x for x in (1000, 2000, 3000, 4000) if x > max_text), 1000)
        self.db.set_route(uid, str(route["src_alias"]), str(route["dst_alias"]),
                          policy=policy, max_text=max_text, per_hour=per_hour)
        self.db.event("route_updated", owner=uid, route=int(route["id"]),
                      policy=policy, per_hour=per_hour, max_text=max_text)
        self.send_route(uid, chat, msg_id, route_id)

    def confirm_route_delete(self, uid: int, chat: Any, msg_id: Any, route_id: str) -> None:
        route = (self.db.one("SELECT * FROM routes WHERE id=? AND owner_tg=?",
                             (int(route_id), uid)) if str(route_id).isdigit() else None)
        if not route:
            return self.send_menu_section(uid, chat, msg_id, "routes")
        text = (f"🗑 Удалить маршрут {route['src_alias'] or 'вы'} → {route['dst_alias']}?\n\n"
                "Задачи и история останутся, но новые отправки по этому маршруту станут "
                "невозможны (нужен будет новый).")
        rows = [[("✅ Да, удалить", f"hub:rdelok:{route['id']}")],
                [("⬅️ Отмена", f"hub:route:{route['id']}"), ("❌ Закрыть", "hub:menu:close")]]
        self._show(uid, chat, msg_id, text, rows)

    def do_route_delete(self, uid: int, chat: Any, msg_id: Any, route_id: str) -> None:
        route = (self.db.one("SELECT * FROM routes WHERE id=? AND owner_tg=?",
                             (int(route_id), uid)) if str(route_id).isdigit() else None)
        if not route:
            return self.send_menu_section(uid, chat, msg_id, "routes")
        src, dst = str(route["src_alias"]), str(route["dst_alias"])
        self.db.delete_route(uid, int(route["id"]))
        self.db.event("route_deleted", owner=uid, route=int(route["id"]), src=src, dst=dst,
                      via="menu")
        self._show(uid, chat, msg_id, f"🗑 Маршрут {src or 'вы'} → {dst} удалён.",
                   [[("⬅️ Маршруты", "hub:menu:routes"), ("❌ Закрыть", "hub:menu:close")]])

    def _task_text(self, task: Any, *, body_limit: int = 1200) -> str:
        """Человеческий вид задачи: суть, вложения, вопрос/ответ, события."""
        view = rpc.task_view(task)
        created = time.strftime("%d.%m %H:%M", time.localtime(float(task["created_at"])))
        keys = task.keys() if hasattr(task, "keys") else ()
        who = f" · клиент {cid}" if cid else ""
        lines = [f"🗂 {task['task_id']}   {task['src_alias']} → {task['dst_alias']}{who}",
                 f"состояние: {view['status']['state']} ({task['status']}) · "
                 f"hops {task['hops']} · ctx {task['ctx']} · создана {created}"]
        body = (task["text"] or "").strip()
        lines += ["", "📄 Задача:", body[:body_limit] if body else "— пусто —"]
        if len(body) > body_limit:
            lines.append(f"… ещё {len(body) - body_limit} симв. — кнопка «Полный текст»")
        try:
            attachments = json.loads(task["attachments"] or "[]")
        except ValueError:
            attachments = []
        if attachments:
            lines += ["", "📎 Вложения:"]
            for item in attachments[:5]:
                if item.get("kind") == "data":
                    lines.append("• data: " + json.dumps(item.get("data"),
                                                         ensure_ascii=False)[:300])
                elif item.get("kind") == "file":
                    info = item.get("file") or {}
                    lines.append(f"• файл: {info.get('name', '?')} "
                                 f"({info.get('mimeType', '?')}) {info.get('uri', '')}")
        if task["question"]:
            lines += ["", "❓ Вопрос:", str(task["question"])[:600]]
        if task["answer"]:
            lines += ["", "💬 Ответ:", str(task["answer"])[:600]]
        events = self.db.q("SELECT kind FROM events WHERE task_id=? ORDER BY id",
                           (task["task_id"],))
        if events:
            lines += ["", "события: " + ", ".join(e["kind"] for e in events[-10:])]
        return "\n".join(lines)

    def send_task_card(self, uid: int, chat: Any, msg_id: Any, task_id: str) -> None:
        back = [[("⬅️ Задачи", "hub:menu:log"), ("❌ Закрыть", "hub:menu:close")]]
        task = self.db.task(task_id)
        if not task or int(task["owner_tg"]) != uid:
            return self._show(uid, chat, msg_id, "Задача не найдена.", back)
        rows = []
        if (task["text"] or "").strip():
            rows.append([("📄 Полный текст", f"hub:tfull:{task_id}")])
        if task["status"] not in TERMINAL:
            rows.append([("❌ Отменить задачу", f"hub:tcancel:{task_id}")])
        rows.extend(back)
        self.db.event("menu_task", owner=uid, task=task_id)
        self._show(uid, chat, msg_id, self._task_text(task), rows)

    def send_task_full(self, uid: int, chat: Any, task_id: str) -> None:
        """Полный текст задачи отдельными сообщениями (лимит Telegram 4096)."""
        task = self.db.task(task_id)
        if not task or int(task["owner_tg"]) != uid:
            return self.say(chat or uid, "Задача не найдена.")
        body = (task["text"] or "").strip() or "— пусто —"
        head = f"📄 {task_id} · {task['src_alias']} → {task['dst_alias']} · полный текст"
        chunks = [body[i:i + 3400] for i in range(0, len(body), 3400)] or [""]
        for index, chunk in enumerate(chunks):
            prefix = head + "\n\n" if index == 0 else "…\n"
            self.say(chat or uid, prefix + chunk)
        self.db.event("menu_task_full", owner=uid, task=task_id, length=len(body))

    def cancel_from_menu(self, uid: int, chat: Any, msg_id: Any, task_id: str) -> None:
        task = self.db.task(task_id)
        if not task or int(task["owner_tg"]) != uid:
            return self.send_task_card(uid, chat, msg_id, task_id)
        if task["status"] in TERMINAL:
            self.tg.answer_callback("", "")
            return self.send_task_card(uid, chat, msg_id, task_id)
        self.db.update_task(task_id, status="cancelled")
        self.db.event("task_cancelled", task_id=task_id, owner=uid, via="menu")
        if task["dst_alias"]:
            self._notify_cancel(str(task["dst_alias"]), uid, task_id)
        self.send_task_card(uid, chat, msg_id, task_id)

    def send_menu_section(self, uid: int, chat_id: Any, msg_id: Any, section: str) -> None:
        """Inline-меню: разделы редактируют одно сообщение, «Закрыть» его удаляет."""
        chat = chat_id or uid
        if section == "close":
            ok, info = self.tg.delete(chat, msg_id)
            self.db.event("menu_closed", owner=uid, ok=bool(ok),
                          error=None if ok else str(info)[:120])
            return

        if section in ("", "main"):
            text = "Главное меню:"
            rows = [list(r) for r in commands.MENU_ROWS]
            rows.append([("❌ Закрыть", "hub:menu:close")])
        else:
            rows = None
            if section == "bots":
                found = self.db.bots(uid)
                text = ("Подключённые боты — нажмите, чтобы открыть agent card:"
                        if found else "Ботов нет. /pair <алиас>")
                rows = [[(f"🪪 {r['alias']} · @{r['username']}", f"hub:card:{r['alias']}")]
                        for r in found] or []
                rows.append([("➕ Добавить бота", "hub:new:pair")])
                rows.append([("⬅️ Меню", "hub:menu:main"), ("❌ Закрыть", "hub:menu:close")])
            elif section == "routes":
                found = self.db.routes(uid)
                text = ("Маршруты — нажмите, чтобы настроить:" if found
                        else "Маршрутов нет. /route <от> <кому> ask|auto")
                rows = [[(f"🔀 {r['src_alias'] or 'вы'} → {r['dst_alias']} · {r['policy']}",
                          f"hub:route:{r['id']}")] for r in found] or []
                rows.append([("➕ Новый маршрут", "hub:new:route")])
                rows.append([("⬅️ Меню", "hub:menu:main"), ("❌ Закрыть", "hub:menu:close")])
            elif section == "log":
                found = self.db.q("SELECT * FROM tasks WHERE owner_tg=? ORDER BY created_at DESC "
                                  "LIMIT 8", (uid,))
                text = ("Последние задачи — нажмите для деталей:" if found else "Задач нет.")
                rows = [[(f"{r['task_id']} · {r['src_alias']}→{r['dst_alias']} · {r['status']}",
                          f"hub:task:{r['task_id']}")] for r in found] or []
                rows.append([("⬅️ Меню", "hub:menu:main"), ("❌ Закрыть", "hub:menu:close")])
            elif section == "protocol":
                text = protocol.AGENT_PROTOCOL
            else:
                text = commands.help_text()
            if not rows:
                rows = [[("⬅️ Меню", "hub:menu:main"), ("❌ Закрыть", "hub:menu:close")]]

        if msg_id:
            ok, info = self.tg.edit(chat, int(msg_id), text[:3900], rows)
            if ok:
                self.db.event("menu_section", owner=uid, section=section, mode="edit")
                return
            # сообщение могло устареть (старше 48ч / удалено) — пришлём новое
            self.db.event("menu_edit_failed", owner=uid, section=section, error=str(info)[:120])
        ok, info = self.tg.send(chat, text[:3900], rows)
        self.db.event("menu_section", owner=uid, section=section, ok=bool(ok), mode="send")

    def finish_card(self, chat_id: Any, msg_id: Any, text: str) -> None:
        if chat_id and msg_id:
            self.tg.edit(chat_id, int(msg_id), text)
        else:
            self.say(chat_id, text)

    # ── утилиты ──────────────────────────────────────────────────────────────
    def send_to_agent(self, alias: str, owner: int, text: str) -> bool:
        bot = self.db.bot_by_alias(owner, alias)
        if not bot:
            return False
        ok, _ = self.tg.send(int(bot["bot_id"]), text)
        return bool(ok)

    def say(self, chat_id: Any, text: str) -> None:
        if chat_id is None:
            return
        self.tg.send(chat_id, text)


def main() -> int:
    import os
    token = os.environ.get("TG_GATEWAY_TOKEN", "").strip()
    owners = os.environ.get("TG_GATEWAY_OWNERS", "").strip()
    if not token or not owners:
        print("нужны TG_GATEWAY_TOKEN и TG_GATEWAY_OWNERS", file=sys.stderr)
        return 2
    db_path = os.environ.get("TG_GATEWAY_DB", str(pathlib.Path.home() / "tg-gateway/hub.sqlite3"))
    # По умолчанию шлюз ЗАКРЫТ: работают только TG_GATEWAY_OWNERS.
    open_reg = os.environ.get("TG_GATEWAY_OPEN", "0").strip().lower() in ("1", "true", "yes")
    proxy = (os.environ.get("TG_GATEWAY_PROXY") or os.environ.get("HTTPS_PROXY")
             or os.environ.get("https_proxy") or "").strip() or None
    hub = Hub(token, db_path, [int(x) for x in owners.split(",") if x.strip()],
              open_registration=open_reg, proxy=proxy)
    print(f"hub: db={db_path} owners={sorted(hub.owners)} proxy={'yes' if proxy else 'no'}",
          flush=True)
    if hub.install_commands():
        print("hub: команды опубликованы в Telegram (автодополнение включено)", flush=True)
    hub.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
