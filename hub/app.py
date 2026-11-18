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
import pathlib
import secrets
import sys
import time
from typing import Any, Dict, Iterable, Optional

from . import policy, rpc
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
            self.db.expire(row["tg_id"])

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
            return self.say(uid, HELP)
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
        if low.startswith("/describe"):
            parts = text.split(maxsplit=2)
            if len(parts) < 2:
                return self.say(uid, "Использование: /describe <алиас> <что умеет>")
            desc = parts[2] if len(parts) > 2 else ""
            if not self.db.set_description(uid, parts[1].strip(), desc):
                return self.say(uid, "Такого бота нет.")
            self.db.event("bot_described", owner=uid, alias=parts[1])
            return self.say(uid, f"Навык «{parts[1]}» обновлён — попадёт в agent card.")
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
                return self.say(uid, "Использование: /cancel <task_id>")
            return self.cancel_task(uid, parts[1])
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
            self.send_to_agent(task["dst_alias"], uid,
                               envelope(t="cancel", task=task_id, ctx=task["ctx"], hops=task["hops"]))
        self.say(uid, f"Задача {task_id} отменена, агент уведомлён.")

    # ── агент ────────────────────────────────────────────────────────────────
    def on_agent(self, bot_id: int, username: str, text: str, chat_id: Any) -> None:
        bot = self.db.bot_by_id(bot_id)
        if not bot:
            if text.lower().startswith("/pair"):
                return self.pair_bot(bot_id, username, text)
            return self.say(chat_id or bot_id, "Бот не подключён. Владелец: /pair <алиас>")
        if not text.startswith("{"):
            return self.say(bot_id, "Формат: JSON-RPC 2.0 или конверт (см. README хаба)")
        try:
            env = json.loads(text)
        except ValueError:
            return self.say(bot_id, "Битый JSON.")
        if "jsonrpc" in env or "method" in env:
            return self.on_rpc(bot, env)
        if env.get("t") == "relay":
            return self.on_relay(bot, env)
        if env.get("t") == "reply":
            return self.on_reply(bot, env)
        self.say(bot_id, "Неизвестный тип конверта.")

    # ── JSON-RPC (A2A-форма поверх Telegram) ─────────────────────────────────
    def on_rpc(self, bot: Any, env: Dict[str, Any]) -> None:
        bot_id = int(bot["bot_id"])
        rpc_id = env.get("id")
        method = str(env.get("method") or "")
        params = env.get("params") or {}
        owner = int(bot["owner_tg"])

        if method in ("agent/card", "agent/getCard", "a2a/discover"):
            target = str(params.get("alias") or "").strip()
            if target:
                peer = self.db.bot_by_alias(owner, target)
                if not peer:
                    return self.say(bot_id, rpc.rpc_err(rpc_id, rpc.ERR_TASK_NOT_FOUND,
                                                        f"нет бота «{target}»"))
                return self.say(bot_id, rpc.rpc_ok(rpc_id, {"card": rpc.bot_card(peer, owner)}))
            return self.say(bot_id, rpc.rpc_ok(rpc_id, {
                "card": rpc.hub_card(owner, self.db.bots(owner)),
                "cards": [rpc.bot_card(b, owner) for b in self.db.bots(owner)]}))

        if method in ("message/send", "message/stream"):
            msg = params.get("message") or {}
            parts = msg.get("parts") or []
            text = "".join(str(p.get("text") or "") for p in parts
                           if str(p.get("kind") or "text") == "text").strip()
            message_id = str(msg.get("messageId") or "")
            ctx = str(msg.get("contextId") or "")
            hops = int((params.get("metadata") or {}).get("hops") or 1)
            to_alias = str(msg.get("to") or params.get("to") or "").strip()
            if not to_alias:
                return self.say(bot_id, rpc.rpc_err(rpc_id, rpc.ERR_INVALID_PARAMS,
                                                    "нужен params.message.to (алиас)"))
            if message_id:
                seen = self.db.seen_message(message_id)
                if seen:
                    task = self.db.task(seen)
                    self.db.event("rpc_dedup", task_id=seen, owner=owner)
                    if task:
                        return self.say(bot_id, rpc.rpc_ok(rpc_id, {"task": rpc.task_view(task),
                                                                   "deduplicated": True}))
            before = {r["task_id"] for r in self.db.q(
                "SELECT task_id FROM tasks WHERE owner_tg=? ORDER BY created_at DESC LIMIT 5",
                (owner,))}
            self.on_relay(bot, {"to": to_alias, "text": text, "ctx": ctx, "hops": hops})
            fresh = [r for r in self.db.q(
                "SELECT * FROM tasks WHERE owner_tg=? ORDER BY created_at DESC LIMIT 3", (owner,))
                if r["task_id"] not in before]
            if not fresh:
                return self.say(bot_id, rpc.rpc_err(rpc_id, rpc.ERR_INVALID_REQUEST,
                                                    self._last_refusal or "запрос отклонён"))
            task = fresh[0]
            if message_id:
                self.db.remember_message(message_id, task["task_id"])
            return self.say(bot_id, rpc.rpc_ok(rpc_id, {"task": rpc.task_view(task)}))

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
                self.send_to_agent(task["dst_alias"], owner,
                                   envelope(t="cancel", task=task["task_id"],
                                            ctx=task["ctx"], hops=task["hops"]))
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
                            status="submitted" if approve else "working")
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
        hops = int(task["hops"]) + 1
        ok, res = self.tg.send(int(target["bot_id"]), envelope(
            t="inbound", task=task_id, ctx=task["ctx"], hops=hops,
            **{"from": task["src_alias"]}, text=task["text"]))
        status = "working" if ok else "failed"
        self.db.update_task(task_id, status=status, hops=hops)
        self.db.event("delivered" if ok else "deliver_failed", task_id=task_id,
                      to=task["dst_alias"], hops=hops, error=None if ok else str(res)[:200])
        return bool(ok)

    # ── ответ агента ─────────────────────────────────────────────────────────
    def on_reply(self, bot: Any, env: Dict[str, Any]) -> None:
        owner = int(bot["owner_tg"])
        task = self.db.task(str(env.get("task") or ""))
        if not task or int(task["owner_tg"]) != owner:
            return self.say(int(bot["bot_id"]), "Задача не найдена.")
        verdict = str(env.get("verdict") or "done").lower()
        text = str(env.get("text") or "").strip()
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
        if len(parts) != 3 or parts[0] != "hub":
            return self.tg.answer_callback(cb_id, "Непонятная карточка")
        _, verdict, task_id = parts
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
    hub.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
