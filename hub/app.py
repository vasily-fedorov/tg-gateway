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

from . import clients as clientlib
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
    "/share <алиас> — код приглашения для связи с чужим владельцем\n"
    "/link <код> <мой_алиас> — связать своего бота с ботом другого владельца\n"
    "/links — межвладельческие связи (id, политики)\n"
    "/unlink <id> — разорвать связь\n"
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
        if low.startswith("/clients"):
            return self.send_menu_section(uid, uid, None, "clients")
        if low.startswith("/client"):
            parts = text.split()
            if len(parts) < 2:
                return self.say(uid, "Использование: /client <cl_xxxxxx>")
            return self.send_client_card(uid, uid, None, parts[1].strip())
        if low.startswith("/token"):
            parts = text.split()
            if len(parts) < 2:
                return self.say(uid, "Использование: /token <cl_xxxxxx>")
            return self.do_client_rotate(uid, uid, None, parts[1].strip())
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
        if low.startswith("/share"):
            parts = text.split()
            if len(parts) < 2:
                return self.say(uid, "Использование: /share <алиас>")
            bot = self.db.bot_by_alias(uid, parts[1].strip())
            if not bot:
                return self.say(uid, "Такого бота нет.")
            code = "LINK-" + secrets.token_hex(2).upper()
            self.db.create_invite(int(bot["bot_id"]), uid, code)
            self.db.event("invite_created", owner=uid, alias=parts[1])
            return self.say(uid, f"Код для связи с ботом «{parts[1]}»:\n\n{code}\n\n"
                                 "Передайте его второму владельцу: он введёт "
                                 f"/link {code} <свой_алиас>\n"
                                 "Код одноразовый. Входящие от чужого владельца "
                                 "по умолчанию требуют вашего подтверждения.")
        if low.startswith("/link"):
            parts = text.split()
            if len(parts) < 3:
                return self.say(uid, "Использование: /link <код> <мой_алиас>")
            mine = self.db.bot_by_alias(uid, parts[2].strip())
            if not mine:
                return self.say(uid, "Сначала /pair <алиас> — такого бота у вас нет.")
            row = self.db.redeem_invite(parts[1])
            if not row:
                return self.say(uid, "Код не найден или уже использован.")
            if int(row["owner_tg"]) == uid:
                return self.say(uid, "Это ваш собственный код.")
            self.db.add_link(int(mine["bot_id"]), int(row["bot_id"]))
            self.db.event("link_created", owner=uid, src=int(mine["bot_id"]),
                          dst=int(row["bot_id"]))
            other = self.db.bot_by_id(int(row["bot_id"]))
            self.say(uid, f"Связь создана: «{mine['alias']}» ↔ @{other['username']}.\n"
                          "Исходящие — по вашей политике, входящие от него — по его.")
            self.say(int(row["owner_tg"]),
                     f"🔗 @{mine['username']} связался с вашим ботом @{other['username']}. "
                     "Его исходящие будут приходить вам на подтверждение.")
            return
        if low.startswith("/links"):
            rows = self.db.links_for_owner(uid)
            if not rows:
                return self.say(uid, "Связей нет. /share <алиас> → код для второго владельца.")
            lines = [f"• #{r['id']} @{r['src_username']} → @{r['dst_username']} "
                     f"(out {r['policy_src']}, in {r['policy_dst']})" for r in rows]
            return self.say(uid, "Связи:\n" + "\n".join(lines))
        if low.startswith("/unlink"):
            parts = text.split()
            if len(parts) < 2 or not parts[1].strip().isdigit():
                return self.say(uid, "Использование: /unlink <id из /links>")
            link = self.db.link_by_id(int(parts[1]))
            if not link:
                return self.say(uid, "Связи с таким id нет.")
            self.db.drop_link(int(parts[1]))
            self.db.event("link_dropped", owner=uid, link=int(parts[1]))
            return self.say(uid, "Связь разорвана.")
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
            me = self.db.bot_by_id(bot_id)
            routes = self.routes_for_bot(owner, str(me["alias"])) if me else []
            return self.say(bot_id, rpc.rpc_ok(rpc_id, {
                "card": rpc.hub_card(owner, self.db.bots(owner)),
                "cards": [rpc.bot_card(b, owner) for b in self.db.bots(owner)],
                "routes": routes}))

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
            task_id = str(params.get("task") or params.get("id") or "")
            task = self.db.task(task_id)
            if not task or int(task["owner_tg"]) != owner:
                return self.say(bot_id, rpc.rpc_err(rpc_id, rpc.ERR_TASK_NOT_FOUND,
                                                    "задача не найдена"))
            status = params.get("status") or {}
            state = str(status.get("state") or "")
            text = ""
            artifact = params.get("artifact") or {}
            for part in (artifact.get("parts") or []):
                if str(part.get("kind") or "text") == "text":
                    text += str(part.get("text") or "")
            message = status.get("message") or {}
            if not text:
                for part in (message.get("parts") or []):
                    if str(part.get("kind") or "text") == "text":
                        text += str(part.get("text") or "")
            text = text.strip()
            if state == "input-required":
                verdict, question = "ask", text
            elif state == "failed":
                self.db.update_task(task_id, status="failed")
                self.db.event("task_failed", task_id=task_id, owner=owner, via="rpc")
                return self.say(bot_id, rpc.rpc_ok(rpc_id, {"task": rpc.task_view(task)}))
            elif text:
                verdict, question = "done", ""
            else:
                verdict, question = "skip", ""
            self.on_reply(bot, {"t": "reply", "task": task_id, "verdict": verdict,
                                "text": text, "question": question})
            fresh = self.db.task(task_id)
            return self.say(bot_id, rpc.rpc_ok(rpc_id, {"task": rpc.task_view(fresh)}))

        if method in ("tasks/list", "tasks/poll"):
            since = float(params.get("since") or 0)
            alias = str(bot["alias"])
            limit = int(params.get("limit") or 50)
            rows = self.db.q(
                "SELECT * FROM tasks WHERE owner_tg=? AND updated_at > ? "
                "AND (src_alias=? OR dst_alias=?) ORDER BY updated_at LIMIT ?",
                (owner, since, alias, alias, limit))
            cursor = max([float(r["updated_at"]) for r in rows], default=since)
            return self.say(bot_id, rpc.rpc_ok(rpc_id, {
                "tasks": [rpc.task_view(r) for r in rows], "cursor": cursor}))

        if method == "tasks/get":
            task = self.db.task(str(params.get("id") or ""))
            if not task or int(task["owner_tg"]) != owner:
                return self.say(bot_id, rpc.rpc_err(rpc_id, rpc.ERR_TASK_NOT_FOUND,
                                                    "задача не найдена"))
            return self.say(bot_id, rpc.rpc_ok(rpc_id, {"task": rpc.task_view(task)}))

        if method == "tasks/cancel":
            task = self.db.task(str(params.get("id") or ""))
            if not task or int(task["owner_tg"]) != owner:
                return self.say(bot_id, rpc.rpc_err(rpc_id, rpc.ERR_TASK_NOT_FOUND,
                                                    "задача не найдена"))
            if task["status"] in TERMINAL:
                return self.say(bot_id, rpc.rpc_err(rpc_id, rpc.ERR_TASK_NOT_CANCELABLE,
                                                    f"задача уже {task['status']}"))
            self.db.update_task(task["task_id"], status="cancelled")
            self.db.event("task_cancelled", task_id=task["task_id"], owner=owner, via="rpc")
            if task["dst_alias"]:
                self._notify_cancel(task["dst_alias"], owner, task["task_id"])
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
        link = None
        if not target:
            link = self.db.link_for(int(bot["bot_id"]), dst_alias)
            if link:
                target = self.db.bot_by_id(int(link["dst_id"]))
        if not target:
            return self.say(int(bot["bot_id"]), f"Нет бота «{dst_alias}» у владельца.")
        if not policy.hop_allowed(hops):
            self.db.event("hop_limit", owner=owner, hops=hops, src=bot["alias"], dst=dst_alias)
            return self.say(int(bot["bot_id"]), f"Предел глубины ({policy.MAX_HOPS}).")
        route = self.db.route(owner, bot["alias"], dst_alias)
        max_text = int(route["max_text"]) if route else policy.MAX_TEXT
        per_hour = int(route["per_hour"]) if route else policy.PER_HOUR
        rpolicy = str(link["policy_src"]) if link else (str(route["policy"]) if route else "ask")
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
        stored_dst = f"@{target['username']}" if link else dst_alias
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
        self.after_src_approval(task_id, link)

    def after_src_approval(self, task_id: str, link: Optional[Dict[str, Any]] = None) -> None:
        """Исходящая сторона согласовала. Для чужого владельца — спросить и его."""
        task = self.db.task(task_id)
        if not task:
            return
        if link and str(link["policy_dst"]) == "ask":
            # Ещё не доставлено: ждём карточку получателя, значит снова submitted
            # (иначе задача висит в working и не попадает в TTL карточек).
            self.db.update_task(task_id, status="submitted")
            dst_owner = int(link["dst_owner"])
            preview = str(task["text"] or "").replace("\n", " ")[:300]
            ok, res = self.tg.send(
                dst_owner,
                f"Входящее от чужого владельца: {task['src_alias']} → @{link['dst_username']}"
                f"\n\n{preview}\n\n· задача {task_id} · ctx {task['ctx']}",
                [[("Принять", f"hub:in:{task_id}"), ("Отклонить", f"hub:inx:{task_id}")]])
            if ok:
                self.db.event("cross_inbound_parked", task_id=task_id, owner=dst_owner)
            return
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
    def _is_a2a_task(self, task: Any) -> bool:
        return str(task["src_alias"] or "") == "a2a-client"

    LEGACY_CLIENT = {"client_id": "owner", "legacy": True, "name": "legacy (token владельца)",
                     "bots_set": None, "policy": "inherit", "max_text": 4000,
                     "per_min": 0, "per_hour": 0}

    def http_rpc(self, method: str, params: Dict[str, Any],
                 client: Optional[Dict[str, Any]] = None) -> Tuple[Dict[str, Any], int]:
        """JSON-RPC из HTTP-фронта (стандартный A2A-клиент). (result, код ошибки).

        `client` — резолвнутый внешний клиент (hub/clients.py); без него — legacy
        токен владельца со всеми правами и без лимитов.
        """
        client = dict(client or self.LEGACY_CLIENT)
        owner = next(iter(self.owners)) if self.owners else None
        if owner is None:
            return {"message": "у хаба нет владельца"}, rpc.ERR_INTERNAL

        method = {"SendMessage": "message/send", "GetTask": "tasks/get",
                  "CancelTask": "tasks/cancel", "ListTasks": "tasks/list",
                  "GetAgentCard": "agent/card", "StreamMessage": "message/stream"}.get(method, method)


        if method in ("agent/card", "agent/getCard", "a2a/discover"):
            alias = str(params.get("alias") or "").strip()
            if alias:
                found = self.db.bot_by_alias(owner, alias)
                if not found:
                    return {"message": f"бот «{alias}» не найден"}, rpc.ERR_TASK_NOT_FOUND
                return {"card": rpc.bot_card(found, owner),
                        "routes": self.routes_for_bot(owner, alias)}, 0
            return {"card": rpc.hub_card(owner, self.db.bots(owner),
                                         public_url=self._a2a_public_url())}, 0

        if method in ("agent/protocol", "agent/help"):
            return {"protocol": protocol.AGENT_PROTOCOL}, 0

        if method in ("message/send", "message/stream"):
            return self._http_message_send(owner, params, client)

        if method == "tasks/get":
            task = self.db.task(str(params.get("id") or ""))
            if not task or int(task["owner_tg"]) != owner:
                return {"message": "задача не найдена"}, rpc.ERR_TASK_NOT_FOUND
            if not client.get("legacy") and str(task["client_id"] or "") != str(client["client_id"]):
                return {"message": "задача не найдена"}, rpc.ERR_TASK_NOT_FOUND
            raw_len = params.get("historyLength")
            try:
                history_length = None if raw_len is None else int(raw_len)
            except (TypeError, ValueError):
                history_length = None
            return {"task": rpc.task_view(task, history_length=history_length,
                                          a2a=True)}, 0

        if method in ("tasks/list", "tasks/poll"):
            since = float(params.get("since") or 0)
            ctx = str(params.get("contextId") or "").strip()
            status = str(params.get("status") or "").strip()
            sql = "SELECT * FROM tasks WHERE owner_tg=? AND updated_at > ?"
            args: list = [owner, since]
            if ctx:
                sql += " AND ctx=?"
                args.append(ctx)
            if status:
                sql += " AND status=?"
                args.append(status)
            if not client.get("legacy"):
                sql += " AND client_id=?"
                args.append(str(client["client_id"]))
            sql += " ORDER BY updated_at LIMIT ?"
            args.append(int(params.get("limit") or 50))
            rows = self.db.q(sql, tuple(args))
            cursor = max([float(r["updated_at"]) for r in rows], default=since)
            return {"tasks": [rpc.task_view(r) for r in rows], "cursor": cursor}, 0

        if method == "tasks/cancel":
            task = self.db.task(str(params.get("id") or ""))
            if not task or int(task["owner_tg"]) != owner:
                return {"message": "задача не найдена"}, rpc.ERR_TASK_NOT_FOUND
            if not client.get("legacy") and str(task["client_id"] or "") != str(client["client_id"]):
                return {"message": "задача не найдена"}, rpc.ERR_TASK_NOT_FOUND
            if task["status"] in TERMINAL:
                return {"message": f"задача уже {task['status']}"}, rpc.ERR_TASK_NOT_CANCELABLE
            self.db.update_task(str(task["task_id"]), status="cancelled")
            self.db.event("task_cancelled", task_id=task["task_id"], owner=owner, via="a2a")
            if task["dst_alias"]:
                self._notify_cancel(str(task["dst_alias"]), owner, str(task["task_id"]))
            return {"task": rpc.task_view(self.db.task(str(task["task_id"])))}, 0

        return {"message": f"неизвестный метод: {method}"}, rpc.ERR_METHOD_NOT_FOUND

    def routes_for_bot(self, owner: int, alias: str) -> list:
        """Куда этому боту можно отправлять: все включённые боты + политика маршрута."""
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

    def _a2a_public_url(self) -> str:
        url = (os.environ.get("TG_GATEWAY_A2A_PUBLIC_URL") or "").strip()
        if url:
            return url.rstrip("/")
        port = (os.environ.get("TG_GATEWAY_A2A_PORT") or "").strip()
        host = (os.environ.get("TG_GATEWAY_A2A_HOST") or "127.0.0.1").strip()
        return f"http://{host}:{port}" if port else ""

    def _http_message_send(self, owner: int, params: Dict[str, Any],
                           client: Dict[str, Any] | None = None) -> Tuple[Dict[str, Any], int]:
        client = dict(client or self.LEGACY_CLIENT)
        cid = str(client["client_id"])
        msg = params.get("message") or {}
        to_alias = str(params.get("to") or msg.get("to") or "").strip()
        if not to_alias:
            bots = self.db.bots(owner)
            if len(bots) == 1:
                to_alias = str(bots[0]["alias"])
            else:
                names = ", ".join(str(b["alias"]) for b in bots)
                return {"message": "нужен params.to (алиас бота) или путь /bot/<alias>; "
                                   f"доступны: {names}"}, rpc.ERR_INVALID_PARAMS
        target = self.db.bot_by_alias(owner, to_alias)
        if not target:
            return {"message": f"бот «{to_alias}» не найден"}, rpc.ERR_TASK_NOT_FOUND
        allowed = client.get("bots_set")
        if allowed is not None and to_alias not in allowed:
            self.db.event("client_denied", owner=owner, client=cid, reason="bot", dst=to_alias)
            return {"message": f"клиенту «{client['name'] or cid}» запрещён бот «{to_alias}»"}, \
                rpc.ERR_FORBIDDEN_BOT
        chunks, attachments = [], []
        for part in (msg.get("parts") or []):
            kind = str(part.get("kind") or "text")
            if kind == "text":
                chunks.append(str(part.get("text") or ""))
            elif kind in ("data", "file"):
                attachments.append(part)
        text = "\n".join(x.strip() for x in chunks if x.strip()).strip()
        max_text = int(client.get("max_text") or policy.MAX_TEXT)
        per_min = int(client.get("per_min") or 0)
        per_hour = int(client.get("per_hour") or 0)
        if per_min and self.db.client_task_count(cid, 60) >= per_min:
            self.db.event("client_rate_limited", owner=owner, client=cid, window="1m")
            return {"message": f"лимит клиента {per_min}/мин исчерпан"}, rpc.ERR_RATE_LIMITED
        if per_hour and self.db.client_task_count(cid, 3600) >= per_hour:
            self.db.event("client_rate_limited", owner=owner, client=cid, window="1h")
            return {"message": f"лимит клиента {per_hour}/час исчерпан"}, rpc.ERR_RATE_LIMITED
        err = policy.check_limits(text, max_text=max_text)
        if err:
            return {"message": err}, rpc.ERR_INVALID_PARAMS
        task_id = new_id()
        ctx = str(msg.get("contextId") or "") or new_id(4)
        self.db.create_task(task_id=task_id, owner=owner, direction="a2a", ctx=ctx, hops=1,
                            src="a2a-client", dst=to_alias, text=text, status="submitted",
                            attachments=json.dumps(attachments, ensure_ascii=False),
                            client_id=None if client.get("legacy") else cid)
        # Политика хаба: спрашивать владельца или доставлять сразу.
        client_policy = str(client.get("policy") or "inherit").strip().lower()
        mode = (client_policy if client_policy in ("ask", "auto")
                else (str(target["a2a_policy"] or "").strip()
                      or (os.environ.get("TG_GATEWAY_A2A_POLICY") or "ask").strip()).lower())
        mode = mode if mode in ("ask", "auto") else "ask"
        if mode == "auto":
            ok = self.deliver(task_id)
            self.db.event("a2a_auto_delivered", task_id=task_id, owner=owner, client=cid,
                          dst=to_alias, ok=bool(ok), length=len(text))
        else:
            preview = text.replace("\n", " ")[:300] or "(без текста)"
            ok, res = self.tg.send(owner, f"🌐 Внешний A2A-клиент → «{to_alias}»?\n\n{preview}\n\n"
                                          f"· задача {task_id} · ctx {ctx}",
                                   [[("Отправить", f"hub:ok:{task_id}"),
                                     ("Отклонить", f"hub:no:{task_id}")]])
            if ok and isinstance(res, dict):
                self.db.update_task(task_id, card_chat=owner, card_msg=res.get("message_id"))
            self.db.event("a2a_parked", task_id=task_id, owner=owner, client=cid,
                          dst=to_alias, length=len(text))
        wait_s = float(params.get("wait_seconds")
                       or (params.get("metadata") or {}).get("wait_seconds")
                       or os.environ.get("TG_GATEWAY_A2A_WAIT") or 0)
        wait_s = max(0.0, min(wait_s, 100.0))
        if wait_s:
            deadline = time.time() + wait_s
            while time.time() < deadline:
                task = self.db.task(task_id)
                if task and task["status"] in TERMINAL:
                    break
                if task and task["answer"]:
                    break
                time.sleep(0.5)
        task = self.db.task(task_id)
        view = rpc.task_view(task)
        if view["status"]["state"] == "working":
            view["status"]["message"] = {
                "role": "agent", "messageId": f"{task_id}-working",
                "parts": [{"kind": "text",
                           "text": f"Задача {task_id} в работе у «{to_alias}» — "
                                   f"результат в tasks/get (contextId {task['ctx']})."}]}
        if view["status"]["state"] == "auth-required":
            view["status"]["message"] = {
                "role": "agent", "messageId": f"{task_id}-pending",
                "parts": [{"kind": "text",
                           "text": "Задача ждёт подтверждения владельца в Telegram. "
                                   f"Готово будет видно через tasks/get (id {task_id}), "
                                   f"contextId {task['ctx']}."}]}
        return {"task": view}, 0

    def on_reply(self, bot: Any, env: Dict[str, Any]) -> None:
        owner = int(bot["owner_tg"])
        task = self.db.task(str(env.get("task") or ""))
        if not task or int(task["owner_tg"]) != owner:
            return self.say(int(bot["bot_id"]), "Задача не найдена.")
        verdict = str(env.get("verdict") or "done").lower()
        text = str(env.get("text") or "").strip()

        if self._is_a2a_task(task):
            if verdict == "skip":
                self.db.update_task(task["task_id"], status="skipped")
                self.db.event("a2a_skipped", task_id=task["task_id"], owner=owner)
            elif verdict == "ask":
                question = str(env.get("question") or text or "").strip()[:1500]
                self.db.update_task(task["task_id"], status="input-required", question=question)
                self.db.event("a2a_input_required", task_id=task["task_id"], owner=owner)
                self.tg.send(owner, f"🌐 Клиент ждёт ответа от «{task['dst_alias']}»:\n\n"
                                    f"{question}\n\nОтветьте текстом.")
            else:
                self.db.update_task(task["task_id"], status="completed", answer=text)
                self.db.event("a2a_answered", task_id=task["task_id"], owner=owner,
                              length=len(text))
            return
        if verdict == "skip":
            self.db.update_task(task["task_id"], status="skipped")
            self.db.event("task_skipped", task_id=task["task_id"], owner=owner)
            return
        if verdict == "ask":
            question = str(env.get("question") or text or "").strip()[:1500]
            self.db.update_task(task["task_id"], status="input-required", question=question)
            self.db.event("task_input_required", task_id=task["task_id"], owner=owner)
            self.tg.send(owner, f"Агент «{task['dst_alias']}» спрашивает:\n\n{question}\n\n"
                                f"Ответьте текстом на это сообщение.\n· задача {task['task_id']}")
            return
        # done: ответ возвращается инициатору как новая задача в том же ctx
        src = str(task["src_alias"])
        dst = str(task["dst_alias"])
        hops = int(task["hops"])
        back_id = new_id()
        route = self.db.route(owner, dst, src)
        rpolicy = str(route["policy"]) if route else "ask"
        approve = policy.needs_approval(policy=rpolicy, ctx=task["ctx"], hops=hops + 1,
                                        direction="reply")
        # hops у задачи = номер хопа, с которым сообщение уйдёт получателю (deliver добавит +1)
        self.db.create_task(task_id=back_id, owner=owner, direction="reply", ctx=task["ctx"],
                            hops=hops, src=dst, dst=src, text=text,
                            status="submitted" if approve else "working")
        self.db.event("reply_parked" if approve else "reply_auto", task_id=back_id, owner=owner,
                      src=dst, dst=src, hops=hops + 1)
        self.db.update_task(task["task_id"], status="completed")
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
                       "cl", "cname", "cbot", "cpol", "cmax", "cph", "crot", "croty",
                       "crev", "crevy", "cexp", "lnk", "lpol"):
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
            if verdict == "lnk":
                return self.send_link_card(uid, chat_id, msg_id, task_id)
            if verdict == "lpol":
                return self.toggle_link_policy(uid, chat_id, msg_id, task_id)
            if verdict == "cl":
                return self.send_client_card(uid, chat_id, msg_id, task_id)
            if verdict == "cname":
                return self.begin_client_rename(uid, chat_id, msg_id, task_id)
            if verdict == "cbot":
                return self.toggle_client_bot(uid, chat_id, msg_id, task_id)
            if verdict in ("cpol", "cmax", "cph"):
                return self.cycle_client_setting(uid, chat_id, msg_id, task_id, verdict)
            if verdict == "cexp":
                return self.cycle_client_expiry(uid, chat_id, msg_id, task_id)
            if verdict == "crot":
                return self.confirm_client_rotate(uid, chat_id, msg_id, task_id)
            if verdict == "croty":
                return self.do_client_rotate(uid, chat_id, msg_id, task_id)
            if verdict == "crev":
                return self.confirm_client_revoke(uid, chat_id, msg_id, task_id)
            if verdict == "crevy":
                return self.do_client_revoke(uid, chat_id, msg_id, task_id)
            if verdict == "rctx":
                return self.toggle_context(uid, chat_id, msg_id, task_id)
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

        # Входящая карточка чужого владельца: права проверяем по владельцу получателя
        if verdict in ("in", "inx"):
            dst_bot = self.db.bot_by_username(str(task["dst_alias"]))
            if not dst_bot or int(dst_bot["owner_tg"]) != uid:
                self.db.event("card_unauthorized", task_id=task_id, owner=uid)
                self.tg.answer_callback(cb_id, "Нет прав")
                return self.finish_card(chat_id, msg_id, "⛔ Нет прав.")
            if task["status"] in TERMINAL:
                self.tg.answer_callback(cb_id, "Уже закрыта")
                return self.finish_card(chat_id, msg_id, f"⌛ Задача уже {task['status']}.")
            if verdict == "inx":
                self.tg.answer_callback(cb_id, "Отклонено")
                self.db.update_task(task_id, status="rejected")
                self.db.event("cross_inbound_rejected", task_id=task_id, owner=uid)
                self.say(int(task["owner_tg"]),
                         f"Получатель отклонил входящее по задаче {task_id}.")
                return self.finish_card(chat_id, msg_id, "❌ Отклонено.")
            self.tg.answer_callback(cb_id, "Принято")
            ok = self.deliver(task_id)
            self.db.event("cross_inbound_approved", task_id=task_id, owner=uid, ok=bool(ok))
            return self.finish_card(chat_id, msg_id,
                                    "✅ Доставлено" if ok else "❌ Не доставлено")

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
        src_bot = self.db.bot_by_alias(int(task["owner_tg"]), str(task["src_alias"]))
        link = None
        if src_bot and str(task["dst_alias"] or "").startswith("@"):
            link = self.db.link_for(int(src_bot["bot_id"]), str(task["dst_alias"]))
        if link:
            self.after_src_approval(task_id, link)
            sent = self.db.task(task_id)["status"] != "rejected"
            return self.finish_card(chat_id, msg_id,
                                    "✅ Отправлено, ждём подтверждения получателя"
                                    if sent else "❌ Отклонено")
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
        if kind == "client":
            self.db.set_state(uid, "client_new")
            self._show(uid, chat, msg_id,
                       "Имя нового внешнего клиента (например langchain-prod).\n"
                       "Дальше выдам токен — он показывается один раз. /cancel — отменить.",
                       [[("⬅️ Клиенты", "hub:menu:clients"), ("❌ Закрыть", "hub:menu:close")]])
            return
        self.db.set_state(uid, "new_route", "{}")
        return self.route_wizard(uid, chat, msg_id, "src", "")

    def route_wizard(self, uid: int, chat: Any, msg_id: Any, step: str, value: str) -> None:
        """Пошаговое создание маршрута: отправитель → получатель → политика."""
        st = self.db.get_state(uid)
        draft = {}
        if st and str(st["action"]) == "new_route":
            try:
                draft = json.loads(st["payload"] or "{}")
            except ValueError:
                draft = {}

        if step == "reset":
            self.db.clear_state(uid)
            draft = {"src": ""}
            self.db.set_state(uid, "new_route", json.dumps(draft, ensure_ascii=False))
            return self.route_wizard(uid, chat, msg_id, "src", "")

        if step == "src" and not value:
            draft = {"src": ""}
            self.db.set_state(uid, "new_route", json.dumps(draft, ensure_ascii=False))
            rows = [[("— это я —", "hub:newr:src:-")]]
            rows += [[(f"🪪 {b['alias']}", f"hub:newr:src:{b['alias']}")]
                     for b in self.db.bots(uid)]
            rows.append([("⬅️ Маршруты", "hub:menu:routes"), ("❌ Закрыть", "hub:menu:close")])
            return self._show(uid, chat, msg_id, "Новый маршрут. Шаг 1/3: кто отправляет?", rows)

        if step == "src":
            draft["src"] = "" if value in ("-", "") else value
            self.db.set_state(uid, "new_route", json.dumps(draft, ensure_ascii=False))
            rows = [[(f"🪪 {b['alias']}", f"hub:newr:dst:{b['alias']}")]
                    for b in self.db.bots(uid) if b["alias"] != draft.get("src")]
            rows.append([("⬅️ Назад", "hub:newr:reset"), ("❌ Закрыть", "hub:menu:close")])
            return self._show(uid, chat, msg_id,
                              f"Новый маршрут {draft.get('src') or 'вы'} → ? Шаг 2/3: кому?", rows)

        if step == "dst":
            draft["dst"] = value
            self.db.set_state(uid, "new_route", json.dumps(draft, ensure_ascii=False))
            rows = [[("ask — спрашивать владельца", "hub:newr:pol:ask"),
                     ("auto — без карточки", "hub:newr:pol:auto")],
                    [("⬅️ Назад", "hub:newr:reset"), ("❌ Закрыть", "hub:menu:close")]]
            return self._show(uid, chat, msg_id,
                              f"Новый маршрут {draft.get('src') or 'вы'} → {value}. "
                              "Шаг 3/3: когда спрашивать?", rows)

        if step == "pol":
            src = str(draft.get("src") or "")
            dst = str(draft.get("dst") or "")
            policy = value if value in ("ask", "auto") else "ask"
            if not dst:
                return self.route_wizard(uid, chat, msg_id, "src", "")
            self.db.set_route(uid, src, dst, policy=policy)
            self.db.clear_state(uid)
            self.db.event("route_created", owner=uid, src=src, dst=dst, policy=policy, via="menu")
            route = self.db.route(uid, src, dst)
            return self.send_route(uid, chat, msg_id, str(route["id"]))

        return self.route_wizard(uid, chat, msg_id, "src", "")

    def handle_state_input(self, uid: int, action: str, payload: str, text: str) -> None:
        """Текст, присланный в ответ на запрос из меню."""
        value = text.strip()
        if value.lower() in ("/cancel", "отмена", "cancel"):
            self.db.clear_state(uid)
            return self.say(uid, "Ввод отменён.")

        if action == "describe":
            alias = payload
            ok = self.db.set_description(uid, alias, value)
            self.db.clear_state(uid)
            if not ok:
                return self.say(uid, "Такого бота нет — ввод сброшен.")
            self.db.event("bot_described", owner=uid, alias=alias, via="menu")
            return self.send_bot_card(uid, uid, None, alias)

        if action == "client_name":
            ok = bool(self.db.client(uid, payload))
            if not ok:
                self.db.clear_state(uid)
                return self.say(uid, "Клиент не найден — ввод сброшен.")
            self.db.update_client(payload, name=value[:60])
            self.db.clear_state(uid)
            self.db.event("client_renamed", owner=uid, client=payload)
            return self.send_client_card(uid, uid, None, payload)

        if action == "client_new":
            row, token = self.db.create_client(owner=uid, name=value[:60], bots="*")
            self.db.clear_state(uid)
            self.db.event("client_created", owner=uid, client=row["client_id"])
            self.say(uid, f"🔑 Клиент {row['client_id']} создан.\n\nТокен "
                          f"(показывается один раз, сохраните):\n\n{token}\n\n"
                          "Подключайте как Bearer. Права и лимиты — в карточке клиента.")
            return self.send_client_card(uid, uid, None, row["client_id"])

        if action == "pair_alias":
            alias = value[:20]
            if self.db.bot_by_alias(uid, alias):
                return self.say(uid, f"«{alias}» уже подключён. Пришлите другой алиас или /cancel.")
            code = "HUB-" + secrets.token_hex(2).upper()
            self.db.add_pair_code(uid, alias, code)
            self.db.clear_state(uid)
            self.db.event("pair_code_issued", owner=uid, alias=alias, via="menu")
            return self.tg.send(uid, f"Код для «{alias}»:\n\n/pair {code} {alias}\n\n"
                                     "Отправьте эту строку боту — он привяжется.",
                                [[("⬅️ Боты", "hub:menu:bots"), ("❌ Закрыть", "hub:menu:close")]])

        if action == "new_route":
            parts = value.split()
            if len(parts) < 2:
                return self.say(uid, "Нужно: <от> <кому> [ask|auto]. Пример: god aa ask")
            src = "" if parts[0] in ("-", "вы", "me") else parts[0]
            dst = parts[1]
            pol = parts[2].lower() if len(parts) > 2 else "ask"
            if pol not in ("ask", "auto"):
                return self.say(uid, "Политика: ask или auto")
            if not self.db.bot_by_alias(uid, dst):
                return self.say(uid, f"Нет бота «{dst}». Сначала /pair.")
            self.db.set_route(uid, src, dst, policy=pol)
            self.db.clear_state(uid)
            self.db.event("route_created", owner=uid, src=src, dst=dst, policy=pol, via="menu")
            return self.say(uid, f"✅ Маршрут {src or 'вы'} → {dst}: {pol}\n"
                                 "Дальше его можно править в /menu → Маршруты.")

        self.db.clear_state(uid)
        return self.say(uid, "Ввод сброшен.")

    def _show(self, uid: int, chat: Any, msg_id: Any, text: str, rows: list) -> bool:
        """Показать экран: правим сообщение, если можем, иначе шлём новое."""
        if msg_id:
            ok, info = self.tg.edit(chat or uid, int(msg_id), text[:3900], rows)
            if ok:
                return True
            self.db.event("menu_edit_failed", owner=uid, error=str(info)[:120])
        ok, _ = self.tg.send(chat or uid, text[:3900], rows)
        return bool(ok)

    def send_bot_card(self, uid: int, chat: Any, msg_id: Any, alias: str) -> None:
        bot = self.db.bot_by_alias(uid, alias)
        back = [[("⬅️ Боты", "hub:menu:bots"), ("❌ Закрыть", "hub:menu:close")]]
        if not bot:
            return self._show(uid, chat, msg_id, f"Бота «{alias}» нет.", back)
        card = rpc.bot_card(bot, uid)
        caps = card["capabilities"]
        text = (f"🪪 {card['name']}  ({alias})\n{card['description']}\n\n"
                f"protocol: {card['protocolVersion']}\n"
                f"capabilities: streaming={caps['streaming']}, "
                f"push={caps['pushNotifications']}, humanApproval={caps['humanApproval']}\n"
                f"skills: {', '.join(s['id'] for s in card['skills']) or '—'}\n"
                f"bot_id: {bot['bot_id']}\n"
                f"A2A: {self._a2a_mode(bot)} "
                f"({'карточка владельцу' if self._a2a_mode(bot) == 'ask' else 'доставка сразу'})")
        self.db.event("menu_bot_card", owner=uid, alias=alias)
        self._show(uid, chat, msg_id, text,
                   [[("✏️ Изменить описание", f"hub:desc:{alias}")],
                    [(f"🌐 A2A → {'авто' if self._a2a_mode(bot) == 'ask' else 'спрашивать'}",
                      f"hub:a2apol:{alias}")], *back])

    def _a2a_mode(self, bot: Any) -> str:
        mode = (str(bot["a2a_policy"] or "").strip()
                or (os.environ.get("TG_GATEWAY_A2A_POLICY") or "ask").strip()).lower()
        return mode if mode in ("ask", "auto") else "ask"

    def toggle_a2a_policy(self, uid: int, chat: Any, msg_id: Any, alias: str) -> None:
        bot = self.db.bot_by_alias(uid, alias)
        if not bot:
            return self.send_bot_card(uid, chat, msg_id, alias)
        new_mode = "auto" if self._a2a_mode(bot) == "ask" else "ask"
        self.db.set_a2a_policy(uid, alias, new_mode)
        self.db.event("a2a_policy_changed", owner=uid, alias=alias, mode=new_mode)
        self.send_bot_card(uid, chat, msg_id, alias)

    def send_route(self, uid: int, chat: Any, msg_id: Any, route_id: str) -> None:
        back = [[("⬅️ Маршруты", "hub:menu:routes"), ("❌ Закрыть", "hub:menu:close")]]
        route = self.db.one("SELECT * FROM routes WHERE id=? AND owner_tg=?",
                            (int(route_id), uid)) if str(route_id).isdigit() else None
        if not route:
            return self._show(uid, chat, msg_id, "Маршрут не найден.", back)
        policy = str(route["policy"])
        ctx_mode = str(route["context_mode"] or "on").strip().lower()
        per_hour = int(route["per_hour"])
        next_limit = next((x for x in (5, 20, 50, 100) if x > per_hour), 5)
        next_max = next((x for x in (1000, 2000, 3000, 4000) if x > int(route["max_text"])), 1000)
        text = (f"🔀 {route['src_alias'] or 'вы'} → {route['dst_alias']}\n\n"
                f"политика: {policy} "
                f"({'спрашивать владельца' if policy == 'ask' else 'продолжения без карточки'})\n"
                f"лимит: {per_hour}/час · max_text: {route['max_text']}\n"
                f"контекст ветки: {'вкл (последние 5 ходов)' if ctx_mode != 'off' else 'выкл'}\n"
                f"id: {route['id']}")
        rows = [
            [(f"Политика → {'auto' if policy == 'ask' else 'ask'}", f"hub:rpol:{route['id']}")],
                    [(f"🧠 Контекст → {'выкл' if ctx_mode == 'off' else 'вкл'}",
                      f"hub:rctx:{route['id']}")],
            [(f"Лимит → {next_limit}/час", f"hub:rlim:{route['id']}")],
            [(f"max_text → {next_max}", f"hub:rmax:{route['id']}")],
            [("🗑 Удалить маршрут", f"hub:rdel:{route['id']}")],
            *back,
        ]
        self.db.event("menu_route", owner=uid, route=int(route["id"]))
        self._show(uid, chat, msg_id, text, rows)

    # ── внешние клиенты (меню) ───────────────────────────────────────────────
    def send_link_card(self, uid: int, chat: Any, msg_id: Any, link_id: str) -> None:
        back = [[("⬅️ Связи", "hub:menu:links"), ("❌ Закрыть", "hub:menu:close")]]
        row = self.db.one(
            "SELECT l.*, s.username AS src_username, s.alias AS src_alias, "
            "s.owner_tg AS src_owner, d.username AS dst_username, d.owner_tg AS dst_owner "
            "FROM links l JOIN bots s ON s.bot_id=l.src_bot_id "
            "JOIN bots d ON d.bot_id=l.dst_bot_id WHERE l.id=?",
            (int(link_id),)) if str(link_id).isdigit() else None
        if not row or uid not in (int(row["src_owner"]), int(row["dst_owner"])):
            return self.send_menu_section(uid, chat, msg_id, "links")
        mine_src = int(row["src_owner"]) == uid
        text = (f"🔗 Связь #{row['id']}\n"
                f"@{row['src_username']} → @{row['dst_username']}\n\n"
                f"исходящие (спрашивать отправителя): {row['policy_src']}\n"
                f"входящие (спрашивать получателя): {row['policy_dst']}\n\n"
                "«ask» — карточка владельцу, «auto» — без карточки.\n"
                "Свою сторону меняете вы, чужую — её владелец.")
        rows = []
        if mine_src:
            rows.append([(f"Исходящие → {'auto' if row['policy_src'] == 'ask' else 'ask'}",
                          f"hub:lpol:{row['id']}:src")])
        if int(row["dst_owner"]) == uid:
            rows.append([(f"Входящие → {'auto' if row['policy_dst'] == 'ask' else 'ask'}",
                          f"hub:lpol:{row['id']}:dst")])
        rows.extend(back)
        self.db.event("menu_link", owner=uid, link=int(row["id"]))
        self._show(uid, chat, msg_id, text, rows)

    def toggle_link_policy(self, uid: int, chat: Any, msg_id: Any, payload: str) -> None:
        link_id, _, which = payload.partition(":")
        row = self.db.one(
            "SELECT l.*, s.owner_tg AS src_owner, d.owner_tg AS dst_owner "
            "FROM links l JOIN bots s ON s.bot_id=l.src_bot_id "
            "JOIN bots d ON d.bot_id=l.dst_bot_id WHERE l.id=?",
            (int(link_id),)) if link_id.isdigit() else None
        if not row:
            return self.send_menu_section(uid, None, msg_id, "links")
        if which == "src" and int(row["src_owner"]) != uid:
            return self.send_link_card(uid, chat, msg_id, link_id)
        if which == "dst" and int(row["dst_owner"]) != uid:
            return self.send_link_card(uid, chat, msg_id, link_id)
        current = str(row["policy_src"] if which == "src" else row["policy_dst"])
        self.db.set_link_policy(int(link_id), which, "auto" if current == "ask" else "ask")
        self.db.event("link_policy_changed", owner=uid, link=int(link_id), which=which)
        self.send_link_card(uid, chat, msg_id, link_id)

    def send_client_card(self, uid: int, chat: Any, msg_id: Any, client_id: str) -> None:
        back = [[("⬅️ Клиенты", "hub:menu:clients"), ("❌ Закрыть", "hub:menu:close")]]
        row = self.db.client(uid, client_id)
        if not row:
            return self._show(uid, chat, msg_id, "Клиент не найден.", back)
        bots = [b["alias"] for b in self.db.bots(uid)]
        allowed = clientlib.parse_bots(row["bots"])
        marks = "все" if allowed is None else (", ".join(sorted(allowed)) or "— ни один —")
        hour = self.db.client_task_count(client_id, 3600)
        day = self.db.client_task_count(client_id, 86400)
        seen = row["last_seen_at"]
        seen_s = (time.strftime("%d.%m %H:%M", time.localtime(float(seen))) if seen else "не было")
        expires = row["expires_at"]
        expires_s = self._expiry_label(expires)
        state = "активен" if int(row["enabled"]) and not row["revoked_at"] else "отозван"
        text = (f"🔑 {row['name'] or client_id}  ({client_id})\n"
                f"токен: {row['token_prefix']}      состояние: {state}\n"
                f"боты: {marks}\n"
                f"политика: {row['policy']} · max_text {row['max_text']} · "
                f"{row['per_min']}/мин · {row['per_hour']}/час\n"
                f"запросов: {hour}/час, {day}/сутки · последний: {seen_s}\n"
                f"срок: {expires_s}")
        rows = [[(f"{'✅' if allowed is None or b in (allowed or ()) else '⬜'} {b}",
                  f"hub:cbot:{client_id}:{b}")] for b in bots] or []
        rows.append([("✏️ Имя", f"hub:cname:{client_id}"),
                     (f"Политика → {self._next_client_policy(row['policy'])}", f"hub:cpol:{client_id}")])
        rows.append([(f"max_text → {self._next_num(int(row['max_text']), (2000, 3000, 4000, 5000))}",
                      f"hub:cmax:{client_id}"),
                     (f"час → {self._next_num(int(row['per_hour']), (50, 100, 200, 500))}",
                      f"hub:cph:{client_id}")])
        rows.append([(f"📅 Срок → {self._next_expiry_label(expires)}", f"hub:cexp:{client_id}")])
        rows.append([("🔄 Ротация токена", f"hub:crot:{client_id}"),
                     ("⛔ Отозвать", f"hub:crev:{client_id}")])
        rows.extend(back)
        self.db.event("menu_client", owner=uid, client=client_id)
        self._show(uid, chat, msg_id, text, rows)

    @staticmethod
    def _next_client_policy(current: str) -> str:
        order = ["inherit", "ask", "auto"]
        return order[(order.index(current) + 1) % 3] if current in order else "inherit"

    @staticmethod
    def _next_num(current: int, options: tuple) -> int:
        return next((x for x in options if x > current), options[0])

    EXPIRY_PRESETS = (7, 30, 90)          # дни; None = бессрочно

    @staticmethod
    def _expiry_label(expires: Any) -> str:
        if not expires:
            return "бессрочно"
        left = (float(expires) - time.time()) / 86400
        stamp = time.strftime("%d.%m.%Y", time.localtime(float(expires)))
        if left <= 0:
            return f"истёк {stamp}"
        return f"до {stamp} ({int(left)} дн)" if left >= 1 else f"до {stamp} (<1 дн)"

    def _next_expiry_days(self, expires: Any) -> Optional[int]:
        """Бессрочно → 7 → 30 → 90 → бессрочно."""
        if not expires:
            return self.EXPIRY_PRESETS[0]
        left = (float(expires) - time.time()) / 86400
        for days in self.EXPIRY_PRESETS:
            if left < days:
                nxt = [d for d in self.EXPIRY_PRESETS if d > days]
                return nxt[0] if nxt else None
        return None

    def _next_expiry_label(self, expires: Any) -> str:
        days = self._next_expiry_days(expires)
        return f"{days} дн" if days else "бессрочно"

    def cycle_client_expiry(self, uid: int, chat: Any, msg_id: Any, client_id: str) -> None:
        row = self.db.client(uid, client_id)
        if not row:
            return self.send_menu_section(uid, None, msg_id, "clients")
        days = self._next_expiry_days(row["expires_at"])
        expires_at = time.time() + days * 86400 if days else None
        self.db.update_client(client_id, expires_at=expires_at)
        self.db.event("client_expiry_changed", owner=uid, client=client_id, days=days)
        self.send_client_card(uid, None, msg_id, client_id)

    def begin_client_rename(self, uid: int, chat: Any, msg_id: Any, client_id: str) -> None:
        if not self.db.client(uid, client_id):
            return self.send_menu_section(uid, None, msg_id, "clients")
        self.db.set_state(uid, "client_name", client_id)
        self._show(uid, chat, msg_id,
                   "Пришлите новое имя клиента (до 60 символов). /cancel — отменить.",
                   [[("⬅️ Клиенты", "hub:menu:clients"), ("❌ Закрыть", "hub:menu:close")]])

    def toggle_client_bot(self, uid: int, chat: Any, msg_id: Any, payload: str) -> None:
        client_id, _, alias = payload.partition(":")
        row = self.db.client(uid, client_id)
        if not row or not alias:
            return self.send_menu_section(uid, None, msg_id, "clients")
        all_bots = [b["alias"] for b in self.db.bots(uid)]
        allowed = clientlib.parse_bots(row["bots"])
        current = set(all_bots) if allowed is None else set(allowed)
        current.symmetric_difference_update({alias})      # toggle
        field = "*" if current >= set(all_bots) else clientlib.dump_bots(current)
        self.db.update_client(client_id, bots=field)
        self.db.event("client_bots_changed", owner=uid, client=client_id, bots=field)
        self.send_client_card(uid, None, msg_id, client_id)

    def cycle_client_setting(self, uid: int, chat: Any, msg_id: Any, payload: str,
                             kind: str) -> None:
        client_id, _, _ = payload.partition(":")
        row = self.db.client(uid, client_id)
        if not row:
            return self.send_menu_section(uid, None, msg_id, "clients")
        if kind == "cpol":
            self.db.update_client(client_id, policy=self._next_client_policy(row["policy"]))
        elif kind == "cmax":
            self.db.update_client(client_id,
                                  max_text=self._next_num(int(row["max_text"]),
                                                          (2000, 3000, 4000, 5000)))
        else:
            self.db.update_client(client_id,
                                  per_hour=self._next_num(int(row["per_hour"]),
                                                          (50, 100, 200, 500)))
        self.db.event("client_updated", owner=uid, client=client_id, what=kind)
        self.send_client_card(uid, None, msg_id, client_id)

    def confirm_client_rotate(self, uid: int, chat: Any, msg_id: Any, client_id: str) -> None:
        row = self.db.client(uid, client_id)
        if not row:
            return self.send_menu_section(uid, None, msg_id, "clients")
        self._show(uid, chat, msg_id,
                   f"Ротировать токен клиента «{row['name'] or client_id}»?\n"
                   "Старый токен перестанет работать сразу.",
                   [[("🔄 Да, ротировать", f"hub:croty:{client_id}")],
                    [("⬅️ Назад", f"hub:cl:{client_id}"), ("❌ Закрыть", "hub:menu:close")]])

    def do_client_rotate(self, uid: int, chat: Any, msg_id: Any, client_id: str) -> None:
        token = self.db.rotate_client_token(uid, client_id)
        if not token:
            return self.send_menu_section(uid, None, msg_id, "clients")
        self.db.event("client_token_rotated", owner=uid, client=client_id)
        self.say(uid, f"🔑 Новый токен клиента {client_id} (показывается один раз):\n\n"
                      f"{token}\n\nСтарый больше не работает.")
        self.send_client_card(uid, None, None, client_id)

    def confirm_client_revoke(self, uid: int, chat: Any, msg_id: Any, client_id: str) -> None:
        row = self.db.client(uid, client_id)
        if not row:
            return self.send_menu_section(uid, None, msg_id, "clients")
        self._show(uid, chat, msg_id,
                   f"Отозвать клиента «{row['name'] or client_id}»?\n"
                   "Его задачи останутся, доступ закроется сразу.",
                   [[("⛔ Да, отозвать", f"hub:crevy:{client_id}")],
                    [("⬅️ Назад", f"hub:cl:{client_id}"), ("❌ Закрыть", "hub:menu:close")]])

    def do_client_revoke(self, uid: int, chat: Any, msg_id: Any, client_id: str) -> None:
        self.db.revoke_client(uid, client_id)
        self.db.event("client_revoked", owner=uid, client=client_id)
        self.send_client_card(uid, None, msg_id, client_id)

    def toggle_context(self, uid: int, chat: Any, msg_id: Any, route_id: str) -> None:
        route = self.db.one("SELECT * FROM routes WHERE id=? AND owner_tg=?",
                            (int(route_id), uid)) if str(route_id).isdigit() else None
        if not route:
            return self.send_menu_section(uid, None, msg_id, "routes")
        new_mode = "on" if str(route["context_mode"] or "on").strip().lower() == "off" else "off"
        self.db.set_context_mode(uid, int(route["id"]), new_mode)
        self.db.event("route_context_changed", owner=uid, route=int(route["id"]), mode=new_mode)
        self.send_route(uid, None, msg_id, route_id)

    def toggle_route(self, uid: int, chat: Any, msg_id: Any, route_id: str, kind: str) -> None:
        route = self.db.one("SELECT * FROM routes WHERE id=? AND owner_tg=?",
                            (int(route_id), uid)) if str(route_id).isdigit() else None
        if not route:
            return self.send_route(uid, chat, msg_id, route_id)
        policy = str(route["policy"])
        per_hour = int(route["per_hour"])
        max_text = int(route["max_text"])
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
        cid = task["client_id"] if "client_id" in keys else None
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
            elif section == "clients":
                found = self.db.clients(uid)
                text = ("Внешние A2A-клиенты — нажмите для настройки:"
                        if found else "Клиентов нет — внешние A2A подключаются только "
                                      "по токену владельца.")
                rows = [[(f"{'🟢' if int(r['enabled']) else '⚪️'} {r['name'] or r['client_id']} · "
                          f"{r['token_prefix']} · {r['per_hour']}/час",
                          f"hub:cl:{r['client_id']}")] for r in found] or []
                rows.append([("➕ Новый клиент", "hub:new:client")])
                rows.append([("⬅️ Меню", "hub:menu:main"), ("❌ Закрыть", "hub:menu:close")])
            elif section == "links":
                found = self.db.links_for_owner(uid)
                text = ("Связи — нажмите, чтобы настроить политики:" if found
                        else "Связей нет. /share <алиас>")
                rows = [[(f"🔗 #{r['id']} @{r['src_username']} → @{r['dst_username']} "
                          f"({r['policy_src']}/{r['policy_dst']})", f"hub:lnk:{r['id']}")]
                        for r in found] or []
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
    a2a_port = int(os.environ.get("TG_GATEWAY_A2A_PORT") or 0)
    if a2a_port:
        from . import a2a_http
        a2a_http.start(hub, port=a2a_port,
                       token=(os.environ.get("TG_GATEWAY_A2A_TOKEN") or "").strip() or None,
                       host=(os.environ.get("TG_GATEWAY_A2A_HOST") or "127.0.0.1").strip())
        print(f"hub: A2A http на :{a2a_port} (Bearer "
              f"{'включён' if os.environ.get('TG_GATEWAY_A2A_TOKEN') else 'выключен'})",
              flush=True)
    if hub.install_commands():
        print("hub: команды опубликованы в Telegram (автодополнение включено)", flush=True)
    hub.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
