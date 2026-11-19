#!/usr/bin/env python3
"""Тесты хаба: pairing, маршруты, карточки, ответы, лимиты, TTL, политика.

Run: python3 tests/test_hub.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hub import clients as clientlib  # noqa: E402
from hub import policy, rpc  # noqa: E402
from hub.app import Hub  # noqa: E402

OWNER = 100000001
GOD = 200000002
AA = 300000003


class FakeTg:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.edits: list[tuple] = []
        self.callbacks: list[tuple] = []
        self.calls: list[tuple] = []

    def send(self, chat_id, text, buttons=None, **kwargs):
        self.sent.append({"chat": chat_id, "text": text, "buttons": buttons})
        return True, {"message_id": len(self.sent)}

    def edit(self, chat_id, message_id, text, buttons=None):
        self.edits.append((chat_id, message_id, text))
        if buttons:
            self.sent.append({"chat": chat_id, "text": text, "buttons": buttons, "edited": True})
        return True, True

    def delete(self, chat_id, message_id):
        self.calls.append(("deleteMessage", {"chat_id": chat_id, "message_id": message_id}))
        return True, True

    def answer_callback(self, callback_id, text=""):
        self.callbacks.append((callback_id, text))

    def get_updates(self, offset, timeout=30):
        return []

    def call(self, method, **params):
        self.calls.append((method, params))
        return True, {"ok": True}


def msg(uid: int, text: str, *, is_bot: bool = False, chat_type: str = "private",
        username: str | None = None):
    return {"message": {"text": text, "from": {"id": uid, "is_bot": is_bot, "username": username},
                        "chat": {"id": uid, "type": chat_type}, "message_id": 1}}


def cb(uid: int, data: str, *, message_id: int = 1):
    return {"callback_query": {"id": "cb1", "from": {"id": uid}, "data": data,
                               "message": {"message_id": message_id, "chat": {"id": OWNER}}}}


def new_hub(open_registration: bool = True) -> tuple[Hub, FakeTg]:
    """Тестам нужен открытый шлюз (несколько владельцев); прод по умолчанию закрыт."""
    tmp = Path(tempfile.mkdtemp(prefix="hub-"))
    tg = FakeTg()
    hub = Hub("TOKEN", tmp / "hub.sqlite3", [OWNER], tg=tg,
              open_registration=open_registration)
    hub.db.ensure_owner(OWNER)
    return hub, tg


def pair(hub: Hub, tg: FakeTg, *, bot_id: int, alias: str) -> None:
    hub.handle(msg(OWNER, f"/pair {alias}"))
    code = [ln for ln in tg.sent[-1]["text"].splitlines() if ln.startswith("/pair ")][0]
    hub.handle(msg(bot_id, code, is_bot=True, username=alias))


def send_relay(hub: Hub, text: str, to: str = "aa", *, bot_id: int = GOD,
               username: str = "god", ctx: str = "", hops: int = 1,
               message_id: str | None = None) -> str:
    """A2A message/send от имени агента; возвращает rpc id."""
    import uuid
    mid = message_id or ("m-" + uuid.uuid4().hex[:8])
    hub.handle(msg(bot_id, json.dumps({
        "jsonrpc": "2.0", "id": "s-" + uuid.uuid4().hex[:6], "method": "message/send",
        "params": {"message": {"role": "user", "messageId": mid, "contextId": ctx,
                               "parts": [{"kind": "text", "text": text}]},
                   "to": to, "metadata": {"hops": hops}}}), is_bot=True, username=username))
    return mid


def envelopes(tg: FakeTg, chat_id: int) -> list[dict]:
    """Нормализованный вид доставленного агенту: A2A message/send → {t,text,task,hops,from}."""
    out = []
    for item in tg.sent:
        if item["chat"] != chat_id or not item["text"].startswith("{"):
            continue
        data = json.loads(item["text"])
        if "result" in data or "error" in data:   # это ответ хаба на RPC, не доставка
            continue
        method = data.get("method")
        params = data.get("params") or {}
        if method == "message/send":
            meta = params.get("metadata") or {}
            text = "".join(str(x.get("text") or "")
                           for x in ((params.get("message") or {}).get("parts") or []))
            out.append({"t": "inbound", "text": text, "task": meta.get("task"),
                        "hops": meta.get("hops"), "from": meta.get("from"),
                        "jsonrpc": data})
        elif method == "tasks/cancel":
            out.append({"t": "cancel", "task": params.get("id"), "jsonrpc": data})
        else:
            out.append(data)
    return out


def run() -> dict:
    res = {}

    # 1. pairing
    hub, tg = new_hub()
    pair(hub, tg, bot_id=GOD, alias="god")
    pair(hub, tg, bot_id=AA, alias="aa")
    assert hub.db.bot_by_id(GOD)["alias"] == "god"
    assert hub.db.bot_by_id(AA)["alias"] == "aa"
    assert any("Подключён бот" in s["text"] for s in tg.sent if s["chat"] == OWNER)
    res["pairing"] = True

    # 2. повторный код не принимается
    hub.handle(msg(OWNER, "/pair bb"))
    code = [ln for ln in tg.sent[-1]["text"].splitlines() if ln.startswith("/pair ")][0]
    hub.handle(msg(111, code, is_bot=True, username="bb"))
    hub.handle(msg(222, code, is_bot=True, username="bb2"))
    assert hub.db.bot_by_id(111) and not hub.db.bot_by_id(222)
    res["pair_code_single_use"] = True

    # 3. relay по политике ask -> карточка, доставки нет
    hub, tg = new_hub()
    pair(hub, tg, bot_id=GOD, alias="god")
    pair(hub, tg, bot_id=AA, alias="aa")
    hub.db.set_route(OWNER, "god", "aa", policy="ask")
    before = len(envelopes(tg, AA))
    send_relay(hub, "привет, aa")
    cards = [s for s in tg.sent if s["chat"] == OWNER and s["buttons"]]
    assert cards and cards[-1]["buttons"][0][0][1].startswith("hub:ok:"), cards
    assert len(envelopes(tg, AA)) == before, "до клика доставки быть не должно"
    task_id = cards[-1]["buttons"][0][0][1].split(":")[2]
    assert hub.db.task(task_id)["status"] == "submitted"
    res["relay_card_before_delivery"] = True

    # 4. клик владельца -> доставка агенту aa
    hub.handle(cb(OWNER, f"hub:ok:{task_id}"))
    got = envelopes(tg, AA)
    assert got and got[-1]["t"] == "inbound" and got[-1]["text"] == "привет, aa", got
    assert got[-1]["hops"] == 2 and got[-1]["task"] == task_id
    assert hub.db.task(task_id)["status"] == "working"
    assert tg.edits and "Отправлено" in tg.edits[-1][2]
    res["card_click_delivers"] = True

    # 5. ответ агента (done) при policy=auto уходит без карточки
    hub.db.set_route(OWNER, "aa", "god", policy="auto")
    before = len(envelopes(tg, GOD))
    hub.handle(msg(AA, json.dumps({"jsonrpc": "2.0", "id": "u-1", "method": "tasks/update",
                                    "params": {"task": task_id,
                                               "status": {"state": "completed"},
                                               "artifact": {"parts": [{"kind": "text",
                                                                       "text": "и тебе привет"}]}}}), is_bot=True, username="aa"))
    got = envelopes(tg, GOD)
    assert len(got) == before + 1 and got[-1]["text"] == "и тебе привет", got
    assert got[-1]["hops"] == 3
    res["reply_auto_continuation"] = True

    # 6. skip -> задача закрыта, доставки нет
    before = len(envelopes(tg, AA))
    hub.handle(msg(AA, json.dumps({"jsonrpc": "2.0", "id": "u-2", "method": "tasks/update",
                   "params": {"task": task_id, "status": {"state": "completed"},
                              "metadata": {"skipped": True}}}),
                   is_bot=True, username="aa"))
    assert len(envelopes(tg, AA)) == before
    assert hub.db.task(task_id)["status"] == "skipped"
    res["reply_skip"] = True

    # 7. ask -> карточка владельцу, ответ текстом уходит агенту
    hub, tg = new_hub()
    pair(hub, tg, bot_id=GOD, alias="god")
    pair(hub, tg, bot_id=AA, alias="aa")
    hub.db.set_route(OWNER, "god", "aa", policy="auto")
    hub.db.set_route(OWNER, "aa", "god", policy="auto")
    send_relay(hub, "сколько места на /?")
    card = [x for x in tg.sent if x["chat"] == OWNER and x["buttons"]][-1]
    t2 = card["buttons"][0][0][1].split(":")[2]
    hub.handle(cb(OWNER, f"hub:ok:{t2}"))
    assert envelopes(tg, AA)[-1]["task"] == t2
    hub.handle(msg(AA, json.dumps({"jsonrpc": "2.0", "id": "u-3", "method": "tasks/update",
                                    "params": {"task": t2,
                                               "status": {"state": "input-required",
                                                          "message": {"parts": [{"kind": "text",
                                                                                 "text": "какой диск имеется в виду?"}]}}}}),
                   is_bot=True, username="aa"))
    assert hub.db.task(t2)["status"] == "input-required"
    assert any("какой диск" in s["text"] for s in tg.sent if s["chat"] == OWNER)
    before = len(envelopes(tg, AA))
    hub.handle(msg(OWNER, "корневой /"))
    ans = envelopes(tg, AA)
    assert len(ans) == before + 1 and ans[-1]["t"] == "answer" and ans[-1]["text"] == "корневой /"
    res["ask_roundtrip"] = True

    # 8. лимиты и глубина
    hub, tg = new_hub()
    pair(hub, tg, bot_id=GOD, alias="god")
    pair(hub, tg, bot_id=AA, alias="aa")
    send_relay(hub, "x", hops=4)
    assert any("Предел глубины" in s["text"] for s in tg.sent), tg.sent[-1]
    send_relay(hub, "a" * 4000)
    assert any("слишком длинный" in s["text"] for s in tg.sent), tg.sent[-1]
    send_relay(hub, "пароль: hunter2")
    res["limits_and_hops"] = True

    # 9. чужой бот без пары
    hub, tg = new_hub()
    hub.handle(msg(999, json.dumps({"jsonrpc": "2.0", "id": "s-9", "method": "message/send",
                                    "params": {"message": {"role": "user", "messageId": "z1",
                                                           "parts": [{"kind": "text", "text": "x"}]},
                                               "to": "aa"}}), is_bot=True))
    assert any("не подключён" in s["text"] for s in tg.sent), tg.sent
    res["unknown_bot_refused"] = True

    # 10. TTL карточки
    hub, tg = new_hub()
    pair(hub, tg, bot_id=GOD, alias="god")
    pair(hub, tg, bot_id=AA, alias="aa")
    send_relay(hub, "старое")
    task_id = [r for r in hub.db.open_tasks(OWNER)][0]["task_id"]
    hub.db.conn.execute("UPDATE tasks SET created_at=? WHERE task_id=?",
                        (policy_now() - 3600, task_id))
    hub.db.expire(OWNER)
    assert hub.db.task(task_id)["status"] == "failed"
    res["card_ttl"] = True

    # 10b. мультитенантность: второй владелец, те же алиасы
    OWNER_B = 777000111
    BOT_B = 8727385599
    hub, tg = new_hub()
    pair(hub, tg, bot_id=GOD, alias="god")
    pair(hub, tg, bot_id=AA, alias="aa")
    hub.handle(msg(OWNER_B, "/start"))
    assert hub.db.owner(OWNER_B), "новый владелец должен регистрироваться сам"
    hub.handle(msg(OWNER_B, "/pair god"))
    code_b = [ln for ln in tg.sent[-1]["text"].splitlines() if ln.startswith("/pair ")][0]
    hub.handle(msg(BOT_B, code_b, is_bot=True, username="owner_b_bot"))
    assert int(hub.db.bot_by_id(BOT_B)["owner_tg"]) == OWNER_B
    assert hub.db.bot_by_id(BOT_B)["alias"] == "god"
    tg.sent.clear()
    hub.handle(msg(OWNER, "/bots"))
    assert "owner_b_bot" not in tg.sent[-1]["text"]
    hub.handle(msg(OWNER_B, "/bots"))
    assert "owner_b_bot" in tg.sent[-1]["text"] and "peer_bot" not in tg.sent[-1]["text"]
    res["multi_tenant_isolation"] = True

    # 10c. чужая карточка не подтверждается
    hub.db.set_route(OWNER, "god", "aa", policy="ask")
    send_relay(hub, "только для A")
    card = [x for x in tg.sent if x["chat"] == OWNER and x["buttons"]][-1]
    task_a = card["buttons"][0][0][1].split(":")[2]
    before = len(envelopes(tg, AA))
    hub.handle(cb(OWNER_B, f"hub:ok:{task_a}"))
    assert len(envelopes(tg, AA)) == before, "чужой клик не должен отправлять"
    assert hub.db.task(task_a)["status"] == "submitted"
    assert any(ev["kind"] == "card_unauthorized" for ev in hub.db.q("SELECT * FROM events"))
    hub.handle(cb(OWNER, f"hub:ok:{task_a}"))
    assert len(envelopes(tg, AA)) == before + 1
    res["cross_tenant_click_denied"] = True

    # 10d. один бот — один владелец; /unpair освобождает
    hub.handle(msg(OWNER_B, "/pair god"))
    code_b2 = [ln for ln in tg.sent[-1]["text"].splitlines() if ln.startswith("/pair ")][0]
    hub.handle(msg(GOD, code_b2, is_bot=True, username="someone"))
    assert int(hub.db.bot_by_id(GOD)["owner_tg"]) == OWNER, "перепривязка запрещена"
    hub.handle(msg(OWNER, "/unpair aa"))
    assert not hub.db.bot_by_alias(OWNER, "aa")
    send_relay(hub, "x")
    assert any("Нет бота" in x["text"] for x in tg.sent), tg.sent[-1]
    res["bot_single_owner_and_unpair"] = True

    # 10e. закрытая регистрация
    tmp2 = Path(tempfile.mkdtemp(prefix="hub-closed-"))
    tg2 = FakeTg()
    closed = Hub("TOKEN", tmp2 / "hub.sqlite3", [OWNER], tg=tg2, open_registration=False)
    closed.handle(msg(999000111, "/start"))
    assert not closed.db.owner(999000111)
    assert any("закрыт" in x["text"] for x in tg2.sent)
    res["closed_registration"] = True

    # 10f. по умолчанию шлюз закрыт: посторонний не станет владельцем
    tmp3 = Path(tempfile.mkdtemp(prefix="hub-default-"))
    tg3 = FakeTg()
    default_hub = Hub("TOKEN", tmp3 / "hub.sqlite3", [OWNER], tg=tg3)
    default_hub.handle(msg(555000111, "/start"))
    assert not default_hub.db.owner(555000111)
    assert any("закрыт" in x["text"] for x in tg3.sent), tg3.sent
    default_hub.handle(msg(OWNER, "/bots"))
    assert default_hub.db.owner(OWNER), "владелец из allowlist работает"
    res["closed_by_default"] = True

    # 10g. межвладельческая связь: /share → /link → два подтверждения
    OWNER_B = 777000111
    BOT_B = 8727385599
    hub, tg = new_hub()
    pair(hub, tg, bot_id=GOD, alias="god")
    hub.handle(msg(OWNER_B, "/start"))
    hub.handle(msg(OWNER_B, "/pair bee"))
    code_b = [ln for ln in tg.sent[-1]["text"].splitlines() if ln.startswith("/pair ")][0]
    hub.handle(msg(BOT_B, code_b, is_bot=True, username="owner_b_bot"))
    # B выдаёт приглашение на своего бота
    hub.handle(msg(OWNER_B, "/share bee"))
    invite = [x for x in tg.sent if x["chat"] == OWNER_B][-1]["text"]
    link_code = [w for w in invite.split() if w.startswith("LINK-")][0]
    # A связывает своего god с ботом B
    hub.handle(msg(OWNER, f"/link {link_code} god"))
    links = hub.db.links_for_owner(OWNER)
    assert links and links[0]["dst_username"] == "owner_b_bot", links
    # A шлёт сообщение на @owner_b_bot: карточка A -> карточка B -> доставка
    send_relay(hub, "привет B", to="@owner_b_bot")
    card_a = [x for x in tg.sent if x["chat"] == OWNER and x["buttons"]][-1]
    task_id = card_a["buttons"][0][0][1].split(":")[2]
    assert not envelopes(tg, BOT_B), "до подтверждений доставки быть не должно"
    hub.handle(cb(OWNER, f"hub:ok:{task_id}"))
    card_b = [x for x in tg.sent if x["chat"] == OWNER_B and x["buttons"]][-1]
    assert "чужого владельца" in card_b["text"], card_b
    assert card_b["buttons"][0][0][1] == f"hub:in:{task_id}"
    assert not envelopes(tg, BOT_B), "ждём подтверждения получателя"
    # чужой (A) не может нажать карточку получателя
    hub.handle(cb(OWNER, f"hub:in:{task_id}", message_id=2))
    assert not envelopes(tg, BOT_B)
    hub.handle(cb(OWNER_B, f"hub:in:{task_id}", message_id=2))
    got = envelopes(tg, BOT_B)
    assert got and got[-1]["text"] == "привет B", got
    res["cross_owner_link_flow"] = True

    # 10h. отказ получателя и разрыв связи
    send_relay(hub, "второе", to="@owner_b_bot")
    t2 = [x for x in tg.sent if x["chat"] == OWNER and x["buttons"]][-1]
    task2 = t2["buttons"][0][0][1].split(":")[2]
    hub.handle(cb(OWNER, f"hub:ok:{task2}"))
    hub.handle(cb(OWNER_B, f"hub:inx:{task2}", message_id=3))
    assert hub.db.task(task2)["status"] == "rejected"
    assert not [e for e in envelopes(tg, BOT_B) if e.get("text") == "второе"]
    link_id = hub.db.links_for_owner(OWNER)[0]["id"]
    hub.handle(msg(OWNER, f"/unlink {link_id}"))
    send_relay(hub, "третье", to="@owner_b_bot")
    assert any("Нет бота" in x["text"] for x in tg.sent), tg.sent[-1]
    res["cross_owner_reject_and_unlink"] = True

    # 10i. без кода связи нет
    hub, tg = new_hub()
    pair(hub, tg, bot_id=GOD, alias="god")
    hub.handle(msg(OWNER, "/link LINK-NOPE god"))
    assert any("не найден" in x["text"] for x in tg.sent), tg.sent[-1]
    send_relay(hub, "x", to="@nobody_bot")
    assert any("Нет бота" in x["text"] for x in tg.sent), tg.sent[-1]
    res["no_link_no_delivery"] = True

    # 10j. JSON-RPC (A2A-форма) поверх Telegram
    hub, tg = new_hub()
    pair(hub, tg, bot_id=GOD, alias="god")
    pair(hub, tg, bot_id=AA, alias="aa")
    hub.handle(msg(OWNER, "/describe aa что умеет: смотреть диск и логи"))

    def rpc_call(mid, method, params, bot_id=GOD, username="god"):
        hub.handle(msg(bot_id, json.dumps({"jsonrpc": "2.0", "id": mid,
                                           "method": method, "params": params}),
                       is_bot=True, username=username))
        return json.loads(tg.sent[-1]["text"])

    res_send = rpc_call("r1", "message/send", {
        "message": {"role": "user", "messageId": "m-1", "parts": [{"kind": "text",
                                                                   "text": "привет, aa"}]},
        "message_to_fix": None, "to": "aa"})
    assert res_send["jsonrpc"] == "2.0" and res_send["id"] == "r1", res_send
    task_view = res_send["result"]["task"]
    assert task_view["id"] and task_view["status"]["state"] == "auth-required", task_view
    assert task_view["kind"] == "task" and task_view["artifacts"][0]["parts"][0]["text"]
    assert any(x["chat"] == OWNER and x["buttons"] for x in tg.sent), "карточка владельцу"
    res["rpc_message_send_auth_required"] = True

    # дедуп по messageId
    again = rpc_call("r2", "message/send", {
        "message": {"role": "user", "messageId": "m-1", "parts": [{"kind": "text",
                                                                   "text": "привет, aa"}]},
        "to": "aa"})
    assert again["result"]["deduplicated"] is True
    assert again["result"]["task"]["id"] == task_view["id"], again
    res["rpc_idempotent_message_id"] = True

    # agent/card отдаёт навыки
    card = rpc_call("r3", "agent/card", {})["result"]
    names = [s["id"] for s in card["card"]["skills"]]
    assert names == ["aa", "god"], names
    assert "смотреть диск" in [s for s in card["card"]["skills"] if s["id"] == "aa"][0]["description"]
    one = rpc_call("r4", "agent/card", {"alias": "aa"})["result"]["card"]
    assert one["name"] == "@aa" and one["capabilities"]["humanApproval"] is True, one
    res["rpc_agent_card"] = True

    # tasks/get и tasks/cancel
    got = rpc_call("r5", "tasks/get", {"id": task_view["id"]})
    assert got["result"]["task"]["status"]["state"] == "auth-required", got
    cancelled = rpc_call("r6", "tasks/cancel", {"id": task_view["id"]})
    assert cancelled["result"]["task"]["status"]["state"] == "canceled", cancelled
    assert hub.db.task(task_view["id"])["status"] == "cancelled"
    bad = rpc_call("r7", "tasks/nope", {})
    assert bad["error"]["code"] == -32601, bad
    res["rpc_tasks_get_cancel"] = True

    # tasks/update: агент сообщает итог в A2A-виде
    hub, tg = new_hub()
    pair(hub, tg, bot_id=GOD, alias="god")
    pair(hub, tg, bot_id=AA, alias="aa")
    hub.db.set_route(OWNER, "god", "aa", policy="auto")
    hub.db.set_route(OWNER, "aa", "god", policy="auto")
    hub.handle(msg(GOD, json.dumps({"jsonrpc": "2.0", "id": "s1", "method": "message/send",
                                    "params": {"message": {"role": "user", "messageId": "u-1",
                                                           "parts": [{"kind": "text",
                                                                      "text": "вопрос"}]},
                                               "to": "aa"}}), is_bot=True, username="god"))
    tv = json.loads(tg.sent[-1]["text"])["result"]["task"]
    hub.handle(cb(OWNER, f"hub:ok:{tv['id']}"))
    upd = json.loads(tg.sent[-1]["text"]) if False else None
    hub.handle(msg(AA, json.dumps({"jsonrpc": "2.0", "id": "s2", "method": "tasks/update",
                                   "params": {"task": tv["id"],
                                              "status": {"state": "completed"},
                                              "artifact": {"parts": [{"kind": "text",
                                                                      "text": "ответ 42"}]}}}),
                   is_bot=True, username="aa"))
    back = json.loads(tg.sent[-1]["text"])
    assert back["result"]["task"]["status"]["state"] in ("working", "completed"), back
    assert any(e.get("text") == "ответ 42" for e in envelopes(tg, GOD)), envelopes(tg, GOD)
    res["rpc_tasks_update"] = True

    # глубина: 4-й хоп не доставляется даже через A2A
    hub, tg = new_hub()
    pair(hub, tg, bot_id=GOD, alias="god")
    pair(hub, tg, bot_id=AA, alias="aa")
    hub.db.set_route(OWNER, "god", "aa", policy="auto")
    hub.db.set_route(OWNER, "aa", "god", policy="auto")
    hub.db.create_task(task_id="deep1", owner=OWNER, direction="reply", ctx="c1", hops=3,
                       src="aa", dst="god", text="четвёртое", status="working")
    before = len(envelopes(tg, GOD))
    assert hub.deliver("deep1") is False
    assert len(envelopes(tg, GOD)) == before
    assert hub.db.task("deep1")["status"] == "failed"
    res["hop_budget_blocks_4th"] = True

    # 10k. TTL конверта, отпечаток, вложения, поллинг, здоровье пира
    hub, tg = new_hub()
    pair(hub, tg, bot_id=GOD, alias="god")
    pair(hub, tg, bot_id=AA, alias="aa")
    hub.db.set_route(OWNER, "god", "aa", policy="auto")

    # просроченный конверт
    old_ts = 1_000_000.0
    hub.handle(msg(GOD, json.dumps({"jsonrpc": "2.0", "id": "t1", "method": "message/send",
                                    "params": {"message": {"role": "user", "messageId": "ttl-1",
                                                           "parts": [{"kind": "text", "text": "старое"}]},
                                               "to": "aa",
                                               "metadata": {"sent_at": old_ts}}}),
                   is_bot=True, username="god"))
    last = json.loads(tg.sent[-1]["text"])
    assert last.get("error", {}).get("code") == -32602, last
    assert hub.db.q("SELECT 1 FROM events WHERE kind='ttl_rejected'")
    res["envelope_ttl"] = True

    # отпечаток: тот же текст с новым messageId
    send_relay(hub, "один и тот же текст", message_id="fp-1")
    first_task = hub.db.q("SELECT task_id FROM tasks WHERE text='один и тот же текст'")[0]["task_id"]
    send_relay(hub, "один и тот же текст", message_id="fp-2")
    dup = json.loads(tg.sent[-1]["text"])
    assert dup["result"]["deduplicated"] is True, dup
    assert dup["result"]["task"]["id"] == first_task
    res["fingerprint_dedupe"] = True

    # вложения: data-часть доезжает до пира как data-часть
    hub.handle(msg(GOD, json.dumps({"jsonrpc": "2.0", "id": "a1", "method": "message/send",
                                    "params": {"message": {"role": "user", "messageId": "att-1",
                                                           "parts": [{"kind": "text", "text": "смотри"},
                                                                     {"kind": "data",
                                                                      "data": {"disk": {"free_gb": 31}}}]},
                                               "to": "aa"}}), is_bot=True, username="god"))
    att_card = [x for x in tg.sent if x["chat"] == OWNER and x["buttons"]][-1]
    att_task = att_card["buttons"][0][0][1].split(":")[2]
    hub.handle(cb(OWNER, f"hub:ok:{att_task}"))
    wire = [json.loads(x["text"]) for x in tg.sent
            if x["chat"] == AA and x["text"].startswith("{") and "method" in json.loads(x["text"])]
    assert wire[-1]["params"]["message"]["parts"][1]["kind"] == "data", wire[-1]
    assert wire[-1]["params"]["metadata"]["protocolVersion"], wire[-1]
    res["data_part_delivered"] = True

    # поллинг: tasks/list по курсору
    since = 0.0
    hub.handle(msg(AA, json.dumps({"jsonrpc": "2.0", "id": "p1", "method": "tasks/list",
                                   "params": {"since": since}}), is_bot=True, username="aa"))
    poll = json.loads(tg.sent[-1]["text"])["result"]
    assert poll["tasks"] and poll["cursor"] > 0, poll
    hub.handle(msg(AA, json.dumps({"jsonrpc": "2.0", "id": "p2", "method": "tasks/list",
                                   "params": {"since": poll["cursor"]}}), is_bot=True, username="aa"))
    assert json.loads(tg.sent[-1]["text"])["result"]["tasks"] == []
    res["tasks_list_polling"] = True

    # здоровье пира: 3 неудачи → доставка блокируется
    hub.db.pair_bot(bot_id=999000777, username="dead_bot", alias="dead", owner=OWNER)
    for _ in range(3):
        hub.db.peer_failed("dead", "boom")
    assert hub.db.peer_blocked("dead") > hub.clock()
    hub.db.create_task(task_id="dead1", owner=OWNER, direction="relay", ctx="x", hops=1,
                       src="god", dst="dead", text="в пустоту", status="working")
    assert hub.deliver("dead1") is False
    assert hub.db.task("dead1")["status"] == "failed"
    assert hub.db.q("SELECT 1 FROM events WHERE kind='peer_blocked'")
    hub.db.peer_ok("dead")
    assert hub.db.peer_blocked("dead") == 0
    res["peer_health_circuit"] = True

    # владелец: /log по боту, /task, /protocol, RPC agent/protocol
    hub, tg = new_hub()
    pair(hub, tg, bot_id=GOD, alias="god")
    pair(hub, tg, bot_id=AA, alias="aa")
    send_relay(hub, "задача для aa")
    hub.handle(msg(OWNER, "/log aa"))
    log_out = tg.sent[-1]["text"]
    assert "aa" in log_out and "задача для aa" in log_out, log_out
    assert hub.db.q("SELECT 1 FROM events WHERE kind='owner_log'")
    hub.handle(msg(OWNER, "/log god"))
    assert "god→aa" in tg.sent[-1]["text"], tg.sent[-1]["text"]
    task_id = hub.db.q("SELECT task_id FROM tasks ORDER BY created_at DESC LIMIT 1")[0]["task_id"]
    hub.handle(msg(OWNER, f"/task {task_id}"))
    assert task_id in tg.sent[-1]["text"] and "состояние" in tg.sent[-1]["text"]
    hub.handle(msg(OWNER, "/protocol"))
    assert "message/send" in tg.sent[-1]["text"] and "tasks/update" in tg.sent[-1]["text"]
    rpc_proto = rpc_call("pp-1", "agent/protocol", {})
    assert "TG-GATEWAY" in rpc_proto["result"]["protocol"]
    res["owner_log_task_protocol"] = True

    # 10l. автодополнение команд, inline-меню, /describe
    hub, tg = new_hub()
    pair(hub, tg, bot_id=GOD, alias="god")
    assert hub.install_commands() is True
    method, params = tg.calls[-1]
    names = [c["command"] for c in json.loads(params["commands"])]
    assert method == "setMyCommands", method
    assert "menu" in names and "describe" in names and len(names) <= 32, names
    assert hub.db.q("SELECT 1 FROM events WHERE kind='commands_published'")
    res["commands_published"] = True

    hub.handle(msg(OWNER, "/menu"))
    menu = [x for x in tg.sent if x["chat"] == OWNER and x["buttons"]][-1]
    flat = [btn[1] for row in menu["buttons"] for btn in row]
    assert "hub:menu:bots" in flat and "hub:menu:protocol" in flat, flat
    assert "hub:menu:close" in flat, flat

    hub.handle(cb(OWNER, "hub:menu:bots", message_id=5))
    section = [x for x in tg.sent if x["chat"] == OWNER and x["buttons"]][-1]
    assert "Подключённые боты" in section["text"]
    assert section.get("edited") is True, "раздел должен редактировать то же сообщение"
    flat_bots = [b[1] for r in section["buttons"] for b in r]
    assert "hub:card:god" in flat_bots and flat_bots[-2:] == ["hub:menu:main", "hub:menu:close"]

    hub.handle(cb(OWNER, "hub:menu:main", message_id=5))
    back = [x for x in tg.sent if x["chat"] == OWNER and x["buttons"]][-1]
    assert "Главное меню" in back["text"]
    assert "hub:menu:routes" in [b[1] for r in back["buttons"] for b in r]

    hub.handle(cb(OWNER, "hub:menu:close", message_id=5))
    assert ("deleteMessage" in [c[0] for c in tg.calls]), tg.calls[-3:]
    assert hub.db.q("SELECT 1 FROM events WHERE kind='menu_closed'")
    res["inline_menu"] = True

    hub.handle(msg(OWNER, "/describe"))
    assert "Описания" in tg.sent[-1]["text"] or "Ботов нет" in tg.sent[-1]["text"]
    hub.handle(msg(OWNER, "/describe god"))
    assert "«god»:" in tg.sent[-1]["text"] and "Задать" in tg.sent[-1]["text"], tg.sent[-1]["text"]
    hub.handle(msg(OWNER, "/describe god смотрит диск и логи"))
    assert "✅" in tg.sent[-1]["text"], tg.sent[-1]["text"]
    assert (hub.db.bot_by_alias(OWNER, "god")["description"] or "").startswith("смотрит диск")
    hub.handle(msg(OWNER, "/describe god"))
    assert "смотрит диск" in tg.sent[-1]["text"]
    res["describe_three_forms"] = True

    # 10m. меню: agent card из «Ботов», настройка маршрута, детали и отмена задачи
    hub, tg = new_hub()
    pair(hub, tg, bot_id=GOD, alias="god")
    pair(hub, tg, bot_id=AA, alias="aa")
    hub.db.set_route(OWNER, "god", "aa", policy="ask", per_hour=20)

    hub.handle(cb(OWNER, "hub:menu:bots", message_id=7))
    bots_screen = [x for x in tg.sent if x["chat"] == OWNER and x["buttons"] and x.get("edited")][-1]
    assert any(b[1] == "hub:card:god" for r in bots_screen["buttons"] for b in r), bots_screen
    hub.handle(cb(OWNER, "hub:card:god", message_id=7))
    card_screen = tg.sent[-1]
    assert "@god" in card_screen["text"] and "protocol:" in card_screen["text"], card_screen["text"]
    assert "humanApproval=True" in card_screen["text"]
    res["menu_bot_card"] = True

    hub.handle(cb(OWNER, "hub:menu:routes", message_id=7))
    routes_screen = [x for x in tg.sent if x.get("edited")][-1]
    route_btn = [b[1] for r in routes_screen["buttons"] for b in r
                 if b[1].startswith("hub:route:")][0]
    hub.handle(cb(OWNER, route_btn, message_id=7))
    assert "политика: ask" in tg.sent[-1]["text"], tg.sent[-1]["text"]
    rid = route_btn.split(":")[2]
    hub.handle(cb(OWNER, f"hub:rpol:{rid}", message_id=7))
    assert "политика: auto" in tg.sent[-1]["text"], tg.sent[-1]["text"]
    assert hub.db.route(OWNER, "god", "aa")["policy"] == "auto"
    hub.handle(cb(OWNER, f"hub:rlim:{rid}", message_id=7))
    assert hub.db.route(OWNER, "god", "aa")["per_hour"] == 50
    res["menu_route_settings"] = True

    send_relay(hub, "задача для отмены")
    new_task = hub.db.q("SELECT task_id FROM tasks ORDER BY created_at DESC LIMIT 1")[0]["task_id"]
    hub.handle(cb(OWNER, "hub:menu:log", message_id=7))
    log_screen = [x for x in tg.sent if x.get("edited")][-1]
    assert any(b[1] == f"hub:task:{new_task}" for r in log_screen["buttons"] for b in r)
    hub.handle(cb(OWNER, f"hub:task:{new_task}", message_id=7))
    detail = tg.sent[-1]
    assert new_task in detail["text"] and "состояние:" in detail["text"]
    cancel_btn = [b[1] for r in detail["buttons"] for b in r if b[1].startswith("hub:tcancel:")]
    assert cancel_btn == [f"hub:tcancel:{new_task}"], detail["buttons"]
    hub.handle(cb(OWNER, cancel_btn[0], message_id=7))
    assert hub.db.task(new_task)["status"] == "cancelled"
    after = tg.sent[-1]
    assert not any(b[1].startswith("hub:tcancel:") for r in after["buttons"] for b in r), after
    res["menu_task_details_and_cancel"] = True

    # 10n. меню: добавление бота, описание с кнопки, новый маршрут, max_text
    hub, tg = new_hub()
    pair(hub, tg, bot_id=GOD, alias="god")

    hub.handle(cb(OWNER, "hub:menu:bots", message_id=11))
    assert any(b[1] == "hub:new:pair" for r in tg.sent[-1]["buttons"] for b in r)
    hub.handle(cb(OWNER, "hub:new:pair", message_id=11))
    assert "алиас нового бота" in tg.sent[-1]["text"]
    hub.handle(msg(OWNER, "bb"))
    issued = tg.sent[-1]["text"]
    assert "/pair HUB-" in issued, issued
    code_line = [ln for ln in issued.splitlines() if ln.startswith("/pair ")][0]
    hub.handle(msg(555000123, code_line, is_bot=True, username="bb_bot"))
    assert hub.db.bot_by_alias(OWNER, "bb")["username"] == "bb_bot"
    res["menu_add_bot"] = True

    hub.handle(cb(OWNER, "hub:card:bb", message_id=11))
    assert any(b[1] == "hub:desc:bb" for r in tg.sent[-1]["buttons"] for b in r)
    hub.handle(cb(OWNER, "hub:desc:bb", message_id=11))
    assert "Пришлите описание" in tg.sent[-1]["text"]
    hub.handle(msg(OWNER, "смотрит логи и диск"))
    assert "смотрит логи и диск" in tg.sent[-1]["text"]
    assert (hub.db.bot_by_alias(OWNER, "bb")["description"] or "").startswith("смотрит")
    res["menu_describe_input"] = True

    hub.handle(cb(OWNER, "hub:menu:routes", message_id=11))
    assert any(b[1] == "hub:new:route" for r in tg.sent[-1]["buttons"] for b in r)
    hub.handle(cb(OWNER, "hub:new:route", message_id=11))
    step1 = tg.sent[-1]
    assert "Шаг 1/3" in step1["text"], step1["text"]
    flats = [b[1] for r in step1["buttons"] for b in r]
    assert "hub:newr:src:-" in flats and "hub:newr:src:god" in flats, flats
    hub.handle(cb(OWNER, "hub:newr:src:god", message_id=11))
    step2 = tg.sent[-1]
    assert "Шаг 2/3" in step2["text"], step2["text"]
    dst_flat = [b[1] for r in step2["buttons"] for b in r]
    assert "hub:newr:dst:bb" in dst_flat and "hub:newr:dst:god" not in dst_flat, dst_flat
    hub.handle(cb(OWNER, "hub:newr:dst:bb", message_id=11))
    step3 = tg.sent[-1]
    assert "Шаг 3/3" in step3["text"], step3["text"]
    pol_flat = [b[1] for r in step3["buttons"] for b in r]
    assert "hub:newr:pol:ask" in pol_flat and "hub:newr:pol:auto" in pol_flat, pol_flat
    hub.handle(cb(OWNER, "hub:newr:pol:auto", message_id=11))
    assert hub.db.route(OWNER, "god", "bb")["policy"] == "auto"
    assert "политика: auto" in tg.sent[-1]["text"], tg.sent[-1]["text"]
    assert hub.db.get_state(OWNER) is None
    res["menu_new_route"] = True

    rid = hub.db.route(OWNER, "god", "bb")["id"]
    hub.handle(cb(OWNER, f"hub:route:{rid}", message_id=11))
    assert any(b[1] == f"hub:rmax:{rid}" for r in tg.sent[-1]["buttons"] for b in r)
    hub.handle(cb(OWNER, f"hub:rmax:{rid}", message_id=11))
    assert hub.db.route(OWNER, "god", "bb")["max_text"] == 4000
    res["menu_max_text"] = True

    # /cancel сбрасывает ожидание ввода (на примере описания бота)
    hub.handle(cb(OWNER, "hub:desc:bb", message_id=11))
    assert hub.db.get_state(OWNER) is not None
    hub.handle(msg(OWNER, "/cancel"))
    assert "отменён" in tg.sent[-1]["text"]
    assert hub.db.get_state(OWNER) is None
    res["menu_cancel_input"] = True

    # 10o. удаление маршрута: подтверждение и отмена
    hub, tg = new_hub()
    pair(hub, tg, bot_id=GOD, alias="god")
    pair(hub, tg, bot_id=AA, alias="aa")
    hub.db.set_route(OWNER, "god", "aa", policy="ask")
    rid = hub.db.route(OWNER, "god", "aa")["id"]

    hub.handle(cb(OWNER, f"hub:route:{rid}", message_id=13))
    assert any(b[1] == f"hub:rdel:{rid}" for r in tg.sent[-1]["buttons"] for b in r)
    hub.handle(cb(OWNER, f"hub:rdel:{rid}", message_id=13))
    confirm = tg.sent[-1]
    assert "Удалить маршрут" in confirm["text"] and "Задачи и история останутся" in confirm["text"]
    assert any(b[1] == f"hub:rdelok:{rid}" for r in confirm["buttons"] for b in r)

    hub.handle(cb(OWNER, f"hub:route:{rid}", message_id=13))      # отмена
    assert hub.db.route(OWNER, "god", "aa") is not None

    hub.handle(cb(OWNER, f"hub:rdel:{rid}", message_id=13))
    hub.handle(cb(OWNER, f"hub:rdelok:{rid}", message_id=13))
    assert hub.db.route(OWNER, "god", "aa") is None
    assert "удалён" in tg.sent[-1]["text"], tg.sent[-1]["text"]
    assert hub.db.q("SELECT 1 FROM events WHERE kind='route_deleted'")
    res["menu_route_delete"] = True

    # 10p. карточка задачи показывает суть, вложения и полный текст
    hub, tg = new_hub()
    pair(hub, tg, bot_id=GOD, alias="god")
    pair(hub, tg, bot_id=AA, alias="aa")
    long_text = "Сколько свободно на диске? " + ("подробности " * 200)
    hub.db.create_task(task_id="card1", owner=OWNER, direction="relay", ctx="ctxc", hops=1,
                       src="god", dst="aa", text=long_text, status="working",
                       attachments=json.dumps([{"kind": "data", "data": {"free_gb": 31}},
                                               {"kind": "file", "file": {"name": "df.txt",
                                                                         "uri": "s3://r/df.txt",
                                                                         "mimeType": "text/plain"}}],
                                              ensure_ascii=False))
    hub.handle(cb(OWNER, "hub:task:card1", message_id=17))
    card = tg.sent[-1]["text"]
    assert "📄 Задача:" in card and "Сколько свободно на диске?" in card, card
    assert "📎 Вложения:" in card and "free_gb" in card and "df.txt" in card, card
    assert "ещё" in card and "симв." in card, card
    flat = [b[1] for r in tg.sent[-1]["buttons"] for b in r]
    assert "hub:tfull:card1" in flat and "hub:tcancel:card1" in flat, flat

    hub.handle(cb(OWNER, "hub:tfull:card1", message_id=17))
    full = [x["text"] for x in tg.sent if x["text"].startswith("📄 card1")]
    assert full and long_text[-40:].strip() in "\n".join(full), "полный текст не пришёл"
    assert hub.db.q("SELECT 1 FROM events WHERE kind='menu_task_full'")
    res["menu_task_body_and_full"] = True

    # 10q. A2A-прокси: стандартный клиент по HTTP/JSON-RPC
    import urllib.request
    from hub import a2a_http

    hub, tg = new_hub()
    pair(hub, tg, bot_id=GOD, alias="god")
    pair(hub, tg, bot_id=AA, alias="aa")

    sent_task, code = hub.http_rpc("message/send", {
        "to": "aa", "message": {"parts": [{"kind": "text", "text": "сколько места на /?"}]}})
    assert code == 0, sent_task
    assert sent_task["task"]["status"]["state"] == "auth-required", sent_task
    task_id = sent_task["task"]["id"]
    assert any(x["chat"] == OWNER and x["buttons"] for x in tg.sent), "карточка владельцу"

    got, code = hub.http_rpc("tasks/get", {"id": task_id})
    assert code == 0 and got["task"]["status"]["state"] == "auth-required"

    hub.handle(cb(OWNER, f"hub:ok:{task_id}"))
    assert any(json.loads(x["text"]).get("method") == "message/send"
               for x in tg.sent if x["chat"] == AA and x["text"].startswith("{")), "не доставлено боту"

    hub.handle(msg(AA, json.dumps({"jsonrpc": "2.0", "id": "u-a", "method": "tasks/update",
                                   "params": {"task": task_id,
                                              "status": {"state": "completed"},
                                              "artifact": {"parts": [{"kind": "text",
                                                                      "text": "31 ГБ"}]}}}),
                   is_bot=True, username="aa"))
    got, _ = hub.http_rpc("tasks/get", {"id": task_id})
    assert got["task"]["status"]["state"] == "completed", got
    assert "31 ГБ" in json.dumps(got["task"], ensure_ascii=False)
    assert hub.db.q("SELECT 1 FROM events WHERE kind='a2a_answered'")
    # отмена задачи внешнего клиента
    t2, _ = hub.http_rpc("message/send", {"to": "aa", "message": {"parts": [{"kind": "text",
                                                                            "text": "вторая"}]}})
    cancelled, code = hub.http_rpc("tasks/cancel", {"id": t2["task"]["id"]})
    assert code == 0 and cancelled["task"]["status"]["state"] == "canceled", cancelled
    res["a2a_http_rpc"] = True

    # реальный HTTP: карточка discovery + токен
    server = a2a_http.start(hub, port=0, token="secret-token")
    try:
        port = server.server_address[1]
        base = f"http://127.0.0.1:{port}"
        card_req = urllib.request.Request(base + "/.well-known/agent-card.json",
                                          headers={"Authorization": "Bearer secret-token"})
        with urllib.request.urlopen(card_req, timeout=5) as r:
            card = json.load(r)
        assert card["protocolVersion"] and "skills" in card, card
        assert card["capabilities"]["streaming"] is False

        req = urllib.request.Request(base + "/", method="POST",
                                     data=json.dumps({"jsonrpc": "2.0", "id": 1,
                                                      "method": "message/send",
                                                      "params": {"to": "aa",
                                                                 "message": {"parts": [
                                                                     {"kind": "text",
                                                                      "text": "http?"}]}}}).encode(),
                                     headers={"Content-Type": "application/json",
                                              "Authorization": "Bearer secret-token"})
        with urllib.request.urlopen(req, timeout=5) as r:
            reply = json.load(r)
        assert reply["result"]["task"]["status"]["state"] == "auth-required", reply

        bad = urllib.request.Request(base + "/", method="POST", data=b"{}",
                                     headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(bad, timeout=5)
            raise AssertionError("без токена должно быть 401")
        except urllib.error.HTTPError as exc:
            assert exc.code == 401, exc.code
    finally:
        server.shutdown()
    res["a2a_http_transport"] = True

    # 10r. совместимость с клиентом Hermes: SendMessage, supportedInterfaces, ожидание
    import threading
    import time as _time
    hub, tg = new_hub()
    pair(hub, tg, bot_id=AA, alias="aa")
    hub._a2a_public_url = lambda: "http://127.0.0.1:18765"

    card, _ = hub.http_rpc("agent/card", {})
    ifaces = card["card"]["supportedInterfaces"]
    assert ifaces and ifaces[0]["protocolBinding"] == "JSONRPC", ifaces
    assert ifaces[0]["url"] == "http://127.0.0.1:18765"
    assert card["card"]["url"] == "http://127.0.0.1:18765"

    send, code = hub.http_rpc("SendMessage", {"to": "aa",
                                             "message": {"parts": [{"kind": "text",
                                                                    "text": "сколько места?"}]}})
    assert code == 0, send
    task_id = send["task"]["id"]
    assert send["task"]["status"]["state"] == "auth-required"
    assert "ждёт подтверждения" in send["task"]["status"]["message"]["parts"][0]["text"]

    # без подтверждения tasks/get остаётся auth-required (алиас GetTask)
    got, _ = hub.http_rpc("GetTask", {"id": task_id})
    assert got["task"]["status"]["state"] == "auth-required"

    # с wait_seconds клиент дожидается ответа, если владелец успел нажать
    result: dict = {}

    def call_with_wait() -> None:
        result["value"] = hub.http_rpc("SendMessage", {
            "to": "aa", "wait_seconds": 8,
            "message": {"parts": [{"kind": "text", "text": "второй вопрос"}]}})

    thread = threading.Thread(target=call_with_wait)
    thread.start()
    _time.sleep(1.0)
    waiting = [t for t in hub.db.q("SELECT * FROM tasks WHERE status='submitted' ORDER BY created_at DESC")]
    second_id = waiting[0]["task_id"]
    hub.handle(cb(OWNER, f"hub:ok:{second_id}"))
    hub.handle(msg(AA, json.dumps({"jsonrpc": "2.0", "id": "u-w", "method": "tasks/update",
                                   "params": {"task": second_id,
                                              "status": {"state": "completed"},
                                              "artifact": {"parts": [{"kind": "text",
                                                                      "text": "28 ГБ"}]}}}),
                   is_bot=True, username="aa"))
    thread.join(timeout=10)
    reply, code = result["value"]
    assert code == 0 and reply["task"]["status"]["state"] in ("completed", "working"), reply
    res["a2a_hermes_client_compat"] = True

    # 10s. путь /bot/<alias> = отдельный A2A-агент (клиент Hermes не умеет params.to)
    import urllib.request
    from hub import a2a_http
    hub, tg = new_hub()
    pair(hub, tg, bot_id=AA, alias="aa")
    hub._a2a_public_url = lambda: "http://127.0.0.1:18765"
    server = a2a_http.start(hub, port=0, token="tok")
    try:
        port = server.server_address[1]
        hdr = {"Authorization": "Bearer tok"}
        req = urllib.request.Request(f"http://127.0.0.1:{port}/bot/aa/.well-known/agent-card.json", headers=hdr)
        with urllib.request.urlopen(req, timeout=5) as r:
            bot_card = json.load(r)
        assert bot_card["supportedInterfaces"][0]["url"].endswith("/bot/aa"), bot_card
        assert "aa" in json.dumps(bot_card)

        req = urllib.request.Request(f"http://127.0.0.1:{port}/bot/aa/", method="POST",
                                     data=json.dumps({"jsonrpc": "2.0", "id": 2,
                                                      "method": "SendMessage",
                                                      "params": {"message": {"parts": [
                                                          {"kind": "text",
                                                           "text": "по пути /bot/aa"}]}}}).encode(),
                                     headers={**hdr, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            out = json.load(r)
        assert out["result"]["task"]["status"]["state"] == "auth-required", out
        dst = hub.db.q("SELECT dst_alias FROM tasks ORDER BY created_at DESC LIMIT 1")[0]["dst_alias"]
        assert dst == "aa", dst

        bad = urllib.request.Request(f"http://127.0.0.1:{port}/bot/zz/.well-known/agent-card.json",
                                     headers=hdr)
        try:
            urllib.request.urlopen(bad, timeout=5)
            raise AssertionError("неизвестный алиас должен давать 404")
        except urllib.error.HTTPError as exc:
            assert exc.code == 404, exc.code
    finally:
        server.shutdown()
    res["a2a_path_routing"] = True

    # 10t. политика A2A из конфига хаба: ask (карточка) / auto (доставка сразу)
    hub, tg = new_hub()
    pair(hub, tg, bot_id=AA, alias="aa")
    hub._a2a_public_url = lambda: "http://127.0.0.1:18765"

    # по умолчанию — ask: карточка владельцу, состояние auth-required
    out, _ = hub.http_rpc("SendMessage", {"to": "aa",
                                          "message": {"parts": [{"kind": "text", "text": "раз"}]}})
    assert out["task"]["status"]["state"] == "auth-required", out
    assert any(x["chat"] == OWNER and x["buttons"] for x in tg.sent)

    # переключаем в auto через меню и повторяем
    hub.handle(cb(OWNER, "hub:a2apol:aa", message_id=40))
    assert hub.db.bot_by_alias(OWNER, "aa")["a2a_policy"] == "auto"
    assert hub.db.q("SELECT 1 FROM events WHERE kind='a2a_policy_changed'")
    cards_before = len([x for x in tg.sent if x["chat"] == OWNER and x["buttons"]])
    out, _ = hub.http_rpc("SendMessage", {"to": "aa",
                                          "message": {"parts": [{"kind": "text", "text": "два"}]}})
    assert out["task"]["status"]["state"] in ("working", "completed"), out
    assert len([x for x in tg.sent if x["chat"] == OWNER and x["buttons"]]) == cards_before
    delivered = [x for x in tg.sent if x["chat"] == AA and x["text"].startswith("{")]
    assert delivered and json.loads(delivered[-1]["text"])["method"] == "message/send"
    assert hub.db.q("SELECT 1 FROM events WHERE kind='a2a_auto_delivered'")

    # env-значение работает как значение по умолчанию для ботов без своей политики
    hub.db.set_a2a_policy(OWNER, "aa", "")
    hub.db.run("UPDATE bots SET a2a_policy='' WHERE alias='aa'")
    os.environ["TG_GATEWAY_A2A_POLICY"] = "auto"
    try:
        out, _ = hub.http_rpc("SendMessage", {"to": "aa",
                                              "message": {"parts": [{"kind": "text", "text": "три"}]}})
        assert out["task"]["status"]["state"] in ("working", "completed"), out
    finally:
        os.environ.pop("TG_GATEWAY_A2A_POLICY", None)
    res["a2a_policy_from_config"] = True

    # 10u. контекст ветки (A2A contextId): выжимка в parts, historyLength, ListTasks
    hub, tg = new_hub()
    pair(hub, tg, bot_id=AA, alias="aa")
    pair(hub, tg, bot_id=GOD, alias="god")
    hub._a2a_public_url = lambda: "http://127.0.0.1:18765"
    hub.db.set_route(OWNER, "aa", "god", policy="auto")

    # первый ход ветки — контекста ещё нет
    t1, _ = hub.http_rpc("SendMessage", {"to": "aa",
                                         "message": {"contextId": "ctx-flow-1",
                                                     "parts": [{"kind": "text",
                                                                "text": "первый вопрос"}]}})
    first_id = t1["task"]["id"]
    hub.handle(cb(OWNER, f"hub:ok:{first_id}"))
    first_payload = json.loads([x for x in tg.sent if x["chat"] == AA][-1]["text"])
    assert "context" not in [p.get("kind") for p in first_payload["params"]["message"]["parts"]]

    hub.handle(msg(AA, json.dumps({"jsonrpc": "2.0", "id": "u-ctx1", "method": "tasks/update",
                                   "params": {"task": first_id,
                                              "status": {"state": "completed"},
                                              "artifact": {"parts": [{"kind": "text",
                                                                      "text": "первый ответ"}]}}}),
                   is_bot=True, username="aa"))

    # второй ход в той же ветке: боту приходит выжимка первого
    t2, _ = hub.http_rpc("SendMessage", {"to": "aa",
                                         "message": {"contextId": "ctx-flow-1",
                                                     "parts": [{"kind": "text",
                                                                "text": "второй вопрос"}]}})
    hub.handle(cb(OWNER, f"hub:ok:{t2['task']['id']}"))
    second_payload = json.loads([x for x in tg.sent if x["chat"] == AA][-1]["text"])
    parts = second_payload["params"]["message"]["parts"]
    digest = [p for p in parts if p.get("kind") == "data" and "context" in (p.get("data") or {})]
    assert digest, parts
    assert digest[0]["data"]["context"][0]["question"] == "первый вопрос"
    assert digest[0]["data"]["context"][0]["answer"] == "первый ответ"
    assert second_payload["params"]["metadata"]["contextTurns"] == 1

    # historyLength: 0 — без истории, 1 — только последнее, None — оба
    got0, _ = hub.http_rpc("GetTask", {"id": first_id, "historyLength": 0})
    assert "history" not in got0["task"], got0
    got1, _ = hub.http_rpc("GetTask", {"id": first_id, "historyLength": 1})
    assert len(got1["task"]["history"]) == 1 and got1["task"]["history"][0]["role"] == "agent"
    gotall, _ = hub.http_rpc("GetTask", {"id": first_id})
    roles = [m["role"] for m in gotall["task"]["history"]]
    assert roles == ["user", "agent"], roles

    # ListTasks по contextId
    listed, _ = hub.http_rpc("ListTasks", {"contextId": "ctx-flow-1"})
    ids = {t["id"] for t in listed["tasks"]}
    assert {first_id, t2["task"]["id"]} <= ids, ids
    assert all(t["contextId"] == "ctx-flow-1" for t in listed["tasks"])

    # выключение контекста на маршруте убирает выжимку
    hub.db.set_route(OWNER, "", "aa", policy="ask")
    route_id = hub.db.route(OWNER, "", "aa")["id"]
    hub.handle(cb(OWNER, f"hub:rctx:{route_id}", message_id=55))
    assert hub.db.route(OWNER, "", "aa")["context_mode"] == "off"
    assert hub.db.q("SELECT 1 FROM events WHERE kind='route_context_changed'")
    t3, _ = hub.http_rpc("SendMessage", {"to": "aa",
                                         "message": {"contextId": "ctx-flow-1",
                                                     "parts": [{"kind": "text", "text": "третий"}]}})
    hub.handle(cb(OWNER, f"hub:ok:{t3['task']['id']}"))
    third_payload = json.loads([x for x in tg.sent if x["chat"] == AA][-1]["text"])
    kinds = [x.get("kind") for x in third_payload["params"]["message"]["parts"]]
    assert "data" not in kinds, kinds
    res["a2a_context_thread"] = True

    # 10v. внешние клиенты: токены, права, лимиты, изоляция задач
    import urllib.error
    import urllib.request
    from hub import a2a_http

    hub, tg = new_hub()
    pair(hub, tg, bot_id=AA, alias="aa")
    pair(hub, tg, bot_id=GOD, alias="god")

    # мастер в меню: имя → клиент + одноразовый токен
    hub.handle(cb(OWNER, "hub:new:client", message_id=60))
    assert hub.db.get_state(OWNER)["action"] == "client_new"
    hub.handle(msg(OWNER, "langchain-prod"))
    tokens = [x["text"] for x in tg.sent if "показывается один раз" in x["text"]]
    assert tokens and "tgw_" in tokens[-1], tokens[-5:]
    row = hub.db.clients(OWNER)[0]
    assert row["name"] == "langchain-prod" and row["token_prefix"].startswith("tgw_")
    assert hub.db.q("SELECT 1 FROM events WHERE kind='client_created'")

    # права: клиенту разрешён только aa
    hub.db.update_client(row["client_id"], bots='["aa"]')
    cl = clientlib.as_client(hub.db.client(OWNER, row["client_id"]))
    out, code = hub.http_rpc("SendMessage", {"to": "god",
                                             "message": {"parts": [{"kind": "text",
                                                                    "text": "влезть к god"}]}},
                             client=cl)
    assert code == rpc.ERR_FORBIDDEN_BOT, (code, out)
    assert hub.db.q("SELECT 1 FROM events WHERE kind='client_denied'")

    out, code = hub.http_rpc("SendMessage", {"to": "aa",
                                             "message": {"parts": [{"kind": "text",
                                                                    "text": "первый"}]}},
                             client=cl)
    assert code == 0, out
    task_id = out["task"]["id"]
    assert hub.db.task(task_id)["client_id"] == row["client_id"]
    assert hub.db.q("SELECT 1 FROM events WHERE kind='a2a_parked'")

    # изоляция: другой клиент этой же задачи не видит
    row2, _ = hub.db.create_client(owner=OWNER, name="other", bots="*")
    cl2 = clientlib.as_client(hub.db.client(OWNER, row2["client_id"]))
    got, code = hub.http_rpc("GetTask", {"id": task_id}, client=cl2)
    assert code == rpc.ERR_TASK_NOT_FOUND, got
    listed, _ = hub.http_rpc("ListTasks", {}, client=cl2)
    assert all(t["id"] != task_id for t in listed["tasks"])
    # legacy-токен владельца видит всё
    legacy, _ = hub.http_rpc("GetTask", {"id": task_id})
    assert legacy["task"]["id"] == task_id

    # лимит частоты: per_min=2 → третий запрос отклонён
    hub.db.update_client(row["client_id"], per_min=2, per_hour=100)
    cl = clientlib.as_client(hub.db.client(OWNER, row["client_id"]))
    hub.http_rpc("SendMessage", {"to": "aa", "message": {"parts": [{"kind": "text",
                                                                   "text": "второй"}]}}, client=cl)
    out, code = hub.http_rpc("SendMessage", {"to": "aa",
                                             "message": {"parts": [{"kind": "text",
                                                                    "text": "третий"}]}},
                             client=cl)
    assert code == rpc.ERR_RATE_LIMITED, (code, out)
    assert hub.db.q("SELECT 1 FROM events WHERE kind='client_rate_limited'")

    # политика клиента переопределяет бота: auto → доставка без карточки
    hub.db.update_client(row["client_id"], policy="auto", per_min=10)
    cl = clientlib.as_client(hub.db.client(OWNER, row["client_id"]))
    cards_before = len([x for x in tg.sent if x["chat"] == OWNER and x["buttons"]])
    out, code = hub.http_rpc("SendMessage", {"to": "aa",
                                             "message": {"parts": [{"kind": "text",
                                                                    "text": "авто"}]}}, client=cl)
    assert code == 0 and out["task"]["status"]["state"] in ("working", "completed"), out
    assert len([x for x in tg.sent if x["chat"] == OWNER and x["buttons"]]) == cards_before

    # HTTP: токен клиента работает, отозванный и старый — 401
    row3, token3 = hub.db.create_client(owner=OWNER, name="http-client", bots="*", policy="auto")
    server = a2a_http.start(hub, port=0, token="legacy-token")
    try:
        port = server.server_address[1]

        def post(token_value):
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/bot/aa/", method="POST",
                data=json.dumps({"jsonrpc": "2.0", "id": 7, "method": "SendMessage",
                                 "params": {"message": {"parts": [{"kind": "text",
                                                                   "text": "из http"}]}}}).encode(),
                headers={"Authorization": f"Bearer {token_value}",
                         "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=5) as r:
                return json.load(r)

        reply = post(token3)
        assert reply["result"]["task"]["id"], reply
        assert hub.db.client(OWNER, row3["client_id"])["last_seen_at"]

        new_token = hub.db.rotate_client_token(OWNER, row3["client_id"])
        try:
            post(token3)
            raise AssertionError("старый токен должен давать 401")
        except urllib.error.HTTPError as exc:
            assert exc.code == 401, exc.code
        assert post(new_token)["result"]["task"]["id"]

        hub.db.revoke_client(OWNER, row3["client_id"])
        try:
            post(new_token)
            raise AssertionError("отозванный клиент должен давать 401")
        except urllib.error.HTTPError as exc:
            assert exc.code == 401, exc.code
        assert hub.db.q("SELECT 1 FROM events WHERE kind='client_denied' AND detail LIKE '%revoked%'")
    finally:
        server.shutdown()
    # 10w. срок действия клиента из меню: 7 → 30 → 90 → бессрочно, истёкший → 401
    import time as _t
    from hub import a2a_http as _a2a_http
    hub, tg = new_hub()
    pair(hub, tg, bot_id=AA, alias="aa")
    row, token = hub.db.create_client(owner=OWNER, name="expiring", bots="*")
    cid = row["client_id"]

    hub.handle(cb(OWNER, f"hub:cexp:{cid}", message_id=70))
    exp = hub.db.client(OWNER, cid)["expires_at"]
    assert exp and 6.9 * 86400 < (exp - _t.time()) < 7.1 * 86400, exp
    card = [x for x in tg.sent if x["text"].startswith("🔑 expiring")][-1]["text"]
    assert "до " in card and "7 дн" in card or "6 дн" in card, card

    hub.handle(cb(OWNER, f"hub:cexp:{cid}", message_id=70))
    exp30 = hub.db.client(OWNER, cid)["expires_at"]
    assert 29.9 * 86400 < (exp30 - _t.time()) < 30.1 * 86400, exp30
    hub.handle(cb(OWNER, f"hub:cexp:{cid}", message_id=70))
    exp90 = hub.db.client(OWNER, cid)["expires_at"]
    assert 89.9 * 86400 < (exp90 - _t.time()) < 90.1 * 86400, exp90
    hub.handle(cb(OWNER, f"hub:cexp:{cid}", message_id=70))
    assert hub.db.client(OWNER, cid)["expires_at"] is None
    assert hub.db.q("SELECT 1 FROM events WHERE kind='client_expiry_changed' LIMIT 1")

    # истёкший клиент не проходит авторизацию
    hub.db.update_client(cid, expires_at=_t.time() - 60)
    server = _a2a_http.start(hub, port=0, token="legacy-exp")
    try:
        import urllib.error
        import urllib.request
        port = server.server_address[1]
        req = urllib.request.Request(f"http://127.0.0.1:{port}/bot/aa/", method="POST",
                                     data=b'{"jsonrpc":"2.0","id":1,"method":"GetTask","params":{"id":"x"}}',
                                     headers={"Authorization": f"Bearer {token}",
                                              "Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=5)
            raise AssertionError("истёкший клиент должен получать 401")
        except urllib.error.HTTPError as exc:
            assert exc.code == 401, exc.code
        assert hub.db.q("SELECT 1 FROM events WHERE kind='client_denied' AND detail LIKE '%expired%'")
    finally:
        server.shutdown()
    # 10x. discovery: адреса ботов в карточке + публичная карточка без токена
    import urllib.error
    import urllib.request
    from hub import a2a_http as _a2a

    hub, tg = new_hub()
    pair(hub, tg, bot_id=AA, alias="aa")
    pair(hub, tg, bot_id=GOD, alias="god")
    hub._a2a_public_url = lambda: "https://a2a.example.com"
    card, _ = hub.http_rpc("agent/card", {})
    agents = card["card"]["metadata"]["agents"]
    assert {a["alias"] for a in agents} == {"aa", "god"}, agents
    aa = next(a for a in agents if a["alias"] == "aa")
    assert aa["url"] == "https://a2a.example.com/bot/aa"
    assert aa["card"].endswith("/bot/aa/.well-known/agent-card.json")
    assert next(s for s in card["card"]["skills"] if s["id"] == "aa")["url"].endswith("/bot/aa")

    os.environ.pop("TG_GATEWAY_A2A_PUBLIC_CARD", None)
    server = _a2a.start(hub, port=0, token="tok")
    try:
        port = server.server_address[1]

        def card_get(headers):
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/.well-known/agent-card.json", headers=headers)
            with urllib.request.urlopen(req, timeout=5) as r:
                return json.load(r)

        try:
            card_get({})
            raise AssertionError("без токена карточка должна быть закрыта")
        except urllib.error.HTTPError as exc:
            assert exc.code == 401, exc.code

        os.environ["TG_GATEWAY_A2A_PUBLIC_CARD"] = "1"
        public = card_get({})
        # аноним не видит ни одного бота — только факт наличия шлюза
        assert public["metadata"]["agents"] == [], public["metadata"]
        assert public["skills"] == [] and public["metadata"]["discovery"] == "auth-required"
        assert "god" not in json.dumps(public, ensure_ascii=False)
        # бот-карточка анониму закрыта
        try:
            urllib.request.urlopen(
                urllib.request.Request(
                    f"http://127.0.0.1:{port}/bot/aa/.well-known/agent-card.json"), timeout=5)
            raise AssertionError("карточка бота анониму недоступна")
        except urllib.error.HTTPError as exc:
            assert exc.code == 404, exc.code

        # с токеном клиента видно только его ботов
        row, token = hub.db.create_client(owner=OWNER, name="scoped", bots='["aa"]')
        scoped = card_get({"Authorization": f"Bearer {token}"})
        assert {a["alias"] for a in scoped["metadata"]["agents"]} == {"aa"}, scoped
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/bot/aa/.well-known/agent-card.json",
            headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=5) as r:
            bot_card = json.load(r)
        assert bot_card["supportedInterfaces"][0]["url"].endswith("/bot/aa")
        # чужой бот виден как 404
        try:
            urllib.request.urlopen(urllib.request.Request(
                f"http://127.0.0.1:{port}/bot/god/.well-known/agent-card.json",
                headers={"Authorization": f"Bearer {token}"}), timeout=5)
            raise AssertionError("чужой бот должен быть 404")
        except urllib.error.HTTPError as exc:
            assert exc.code == 404, exc.code
        post = urllib.request.Request(
            f"http://127.0.0.1:{port}/bot/aa/", method="POST",
            data=b'{"jsonrpc":"2.0","id":1,"method":"GetTask","params":{"id":"x"}}',
            headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(post, timeout=5)
            raise AssertionError("POST без токена должен быть 401")
        except urllib.error.HTTPError as exc:
            assert exc.code == 401, exc.code
        # health открыт без токена, но не раскрывает данных
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as r:
            assert json.load(r) == {"ok": True}
        os.environ.pop("TG_GATEWAY_A2A_PUBLIC_CARD", None)
    finally:
        server.shutdown()
    res["a2a_discovery_public_card"] = True

    res["client_expiry_menu"] = True

    res["clients_tokens_limits"] = True

    # 10y. auto-политика: message/send ждёт ответ агента (нативный клиент не поллит)
    import threading as _th
    import time as _t2
    hub, tg = new_hub()
    pair(hub, tg, bot_id=AA, alias="aa")
    hub._a2a_public_url = lambda: "https://a2a.example.com"
    row, _token = hub.db.create_client(owner=OWNER, name="native", bots='["aa"]',
                                       policy="auto")
    cl = clientlib.as_client(hub.db.client(OWNER, row["client_id"]))
    answer: dict = {}

    def slow_call() -> None:
        answer["v"] = hub.http_rpc("SendMessage", {
            "to": "aa", "wait_seconds": 8,
            "message": {"parts": [{"kind": "text", "text": "ответь OK"}]}}, client=cl)

    thread = _th.Thread(target=slow_call)
    thread.start()
    _t2.sleep(0.7)
    sent = [x for x in tg.sent if x["chat"] == AA and x["text"].startswith("{")]
    assert sent, "задача должна уйти боту без карточки"
    task_id = json.loads(sent[-1]["text"])["params"]["metadata"]["task"]
    hub.handle(msg(AA, json.dumps({"jsonrpc": "2.0", "id": "u-wait", "method": "tasks/update",
                                   "params": {"task": task_id,
                                              "status": {"state": "completed"},
                                              "artifact": {"parts": [{"kind": "text",
                                                                      "text": "OK"}]}}}),
                   is_bot=True, username="aa"))
    thread.join(timeout=10)
    result, code = answer["v"]
    assert code == 0 and result["task"]["status"]["state"] == "completed", result
    assert result["task"]["history"][-1]["parts"][0]["text"] == "OK"
    res["a2a_native_wait"] = True

    # старый самодельный конверт больше не принимается
    hub, tg = new_hub()
    pair(hub, tg, bot_id=GOD, alias="god")
    pair(hub, tg, bot_id=AA, alias="aa")
    before_cards = len([x for x in tg.sent if x["chat"] == OWNER and x["buttons"]])
    hub.handle(msg(GOD, json.dumps({"t": "relay", "to": "aa", "text": "старый формат"}),
                   is_bot=True, username="god"))
    assert len([x for x in tg.sent if x["chat"] == OWNER and x["buttons"]]) == before_cards
    parsed = []
    for item in [x for x in tg.sent if x["chat"] == GOD]:
        try:
            parsed.append(json.loads(item["text"]))
        except ValueError:
            continue
    assert any("error" in item for item in parsed), parsed[-2:]
    res["legacy_envelope_rejected"] = True

    # 11. policy-функции
    assert policy.needs_approval(policy="ask", ctx="", hops=1, direction="relay") is True
    assert policy.needs_approval(policy="auto", ctx="ab12", hops=2, direction="reply") is False
    assert policy.needs_approval(policy="ask", ctx="ab12", hops=2, direction="reply") is True
    assert policy.hop_allowed(3) and not policy.hop_allowed(4)
    assert policy.scan_text("пароль: 123") and not policy.scan_text("обычный текст")
    res["policy_units"] = True

    return res


def policy_now() -> float:
    import time
    return time.time()


def main() -> int:
    res = run()
    print("hub: " + json.dumps(res, ensure_ascii=False))
    print(f"OK: {len(res)} hub groups passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
