#!/usr/bin/env python3
"""Тесты хаба: pairing, маршруты, карточки, ответы, лимиты, TTL, политика.

Run: python3 tests/test_hub.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hub import policy  # noqa: E402
from hub.app import Hub  # noqa: E402

OWNER = 100000001
GOD = 200000002
AA = 300000003


class FakeTg:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.edits: list[tuple] = []
        self.callbacks: list[tuple] = []

    def send(self, chat_id, text, buttons=None, **kwargs):
        self.sent.append({"chat": chat_id, "text": text, "buttons": buttons})
        return True, {"message_id": len(self.sent)}

    def edit(self, chat_id, message_id, text):
        self.edits.append((chat_id, message_id, text))
        return True, True

    def answer_callback(self, callback_id, text=""):
        self.callbacks.append((callback_id, text))

    def get_updates(self, offset, timeout=30):
        return []


def msg(uid: int, text: str, *, is_bot: bool = False, chat_type: str = "private",
        username: str | None = None):
    return {"message": {"text": text, "from": {"id": uid, "is_bot": is_bot, "username": username},
                        "chat": {"id": uid, "type": chat_type}, "message_id": 1}}


def cb(uid: int, data: str, *, message_id: int = 1):
    return {"callback_query": {"id": "cb1", "from": {"id": uid}, "data": data,
                               "message": {"message_id": message_id, "chat": {"id": OWNER}}}}


def new_hub() -> tuple[Hub, FakeTg]:
    tmp = Path(tempfile.mkdtemp(prefix="hub-"))
    tg = FakeTg()
    hub = Hub("TOKEN", tmp / "hub.sqlite3", [OWNER], tg=tg)
    hub.db.ensure_owner(OWNER)
    return hub, tg


def pair(hub: Hub, tg: FakeTg, *, bot_id: int, alias: str) -> None:
    hub.handle(msg(OWNER, f"/pair {alias}"))
    code = [ln for ln in tg.sent[-1]["text"].splitlines() if ln.startswith("/pair ")][0]
    hub.handle(msg(bot_id, code, is_bot=True, username=alias))


def envelopes(tg: FakeTg, chat_id: int) -> list[dict]:
    out = []
    for item in tg.sent:
        if item["chat"] == chat_id and item["text"].startswith("{"):
            out.append(json.loads(item["text"]))
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
    hub.handle(msg(GOD, json.dumps({"t": "relay", "to": "aa", "text": "привет, aa"}),
                   is_bot=True, username="god"))
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
    hub.handle(msg(AA, json.dumps({"t": "reply", "task": task_id, "verdict": "done",
                                   "text": "и тебе привет"}), is_bot=True, username="aa"))
    got = envelopes(tg, GOD)
    assert len(got) == before + 1 and got[-1]["text"] == "и тебе привет", got
    assert got[-1]["hops"] == 3
    res["reply_auto_continuation"] = True

    # 6. skip -> задача закрыта, доставки нет
    before = len(envelopes(tg, AA))
    hub.handle(msg(AA, json.dumps({"t": "reply", "task": task_id, "verdict": "skip"}),
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
    hub.handle(msg(GOD, json.dumps({"t": "relay", "to": "aa", "text": "сколько места на /?"}),
                   is_bot=True, username="god"))
    card = [x for x in tg.sent if x["chat"] == OWNER and x["buttons"]][-1]
    t2 = card["buttons"][0][0][1].split(":")[2]
    hub.handle(cb(OWNER, f"hub:ok:{t2}"))
    assert envelopes(tg, AA)[-1]["task"] == t2
    hub.handle(msg(AA, json.dumps({"t": "reply", "task": t2, "verdict": "ask",
                                   "question": "какой диск имеется в виду?"}),
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
    hub.handle(msg(GOD, json.dumps({"t": "relay", "to": "aa", "hops": 4, "text": "x"}),
                   is_bot=True, username="god"))
    assert any("Предел глубины" in s["text"] for s in tg.sent), tg.sent[-1]
    hub.handle(msg(GOD, json.dumps({"t": "relay", "to": "aa", "text": "a" * 4000}),
                   is_bot=True, username="god"))
    assert any("слишком длинный" in s["text"] for s in tg.sent), tg.sent[-1]
    hub.handle(msg(GOD, json.dumps({"t": "relay", "to": "aa",
                                    "text": "пароль: hunter2"}), is_bot=True, username="god"))
    assert any("фильтр" in s["text"] for s in tg.sent), tg.sent[-1]
    res["limits_and_hops"] = True

    # 9. чужой бот без пары
    hub, tg = new_hub()
    hub.handle(msg(999, json.dumps({"t": "relay", "to": "aa", "text": "x"}), is_bot=True))
    assert any("не подключён" in s["text"] for s in tg.sent), tg.sent
    res["unknown_bot_refused"] = True

    # 10. TTL карточки
    hub, tg = new_hub()
    pair(hub, tg, bot_id=GOD, alias="god")
    pair(hub, tg, bot_id=AA, alias="aa")
    hub.handle(msg(GOD, json.dumps({"t": "relay", "to": "aa", "text": "старое"}),
                   is_bot=True, username="god"))
    task_id = [r for r in hub.db.open_tasks(OWNER)][0]["task_id"]
    hub.db.conn.execute("UPDATE tasks SET created_at=? WHERE task_id=?",
                        (policy_now() - 3600, task_id))
    hub.db.expire(OWNER)
    assert hub.db.task(task_id)["status"] == "failed"
    res["card_ttl"] = True

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
