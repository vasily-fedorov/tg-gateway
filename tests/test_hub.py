#!/usr/bin/env python3
"""Тесты хаба как A2A-сервера: асинхронные задачи, карточки, прогресс, отмена.

Run: python3 tests/test_hub.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hub import policy  # noqa: E402
from hub.app import Hub  # noqa: E402

OWNER = 100000001
GOD = 200000002
AA = 300000003

RES: dict = {}


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


def new_hub() -> tuple[Hub, FakeTg]:
    tmp = Path(tempfile.mkdtemp(prefix="hub-"))
    tg = FakeTg()
    hub = Hub("TOKEN", tmp / "hub.sqlite3", [OWNER], tg=tg, open_registration=False)
    hub.db.ensure_owner(OWNER)
    return hub, tg


def pair(hub: Hub, tg: FakeTg, *, bot_id: int, alias: str, routes_mode: str = "all") -> None:
    hub.handle(msg(OWNER, f"/pair {alias}"))
    code = [ln for ln in tg.sent[-1]["text"].splitlines() if ln.startswith("/pair ")][0]
    hub.handle(msg(bot_id, code, is_bot=True, username=alias))
    if routes_mode != "explicit":
        hub.db.set_routes_mode(OWNER, alias, routes_mode)


def rpc_call(hub: Hub, tg: FakeTg, bot_id: int, username: str, method: str,
             params: dict) -> dict:
    hub.handle(msg(bot_id, json.dumps({"jsonrpc": "2.0", "id": "x-" + uuid.uuid4().hex[:6],
                                       "method": method, "params": params}),
                   is_bot=True, username=username))
    return json.loads(tg.sent[-1]["text"])


def send(hub: Hub, tg: FakeTg, text: str, *, to: str = "aa", bot_id: int = GOD,
         username: str = "god", ctx: str = "", message_id: str | None = None) -> dict:
    mid = message_id or ("m-" + uuid.uuid4().hex[:8])
    return rpc_call(hub, tg, bot_id, username, "message/send", {
        "message": {"role": "user", "messageId": mid, "contextId": ctx,
                    "parts": [{"kind": "text", "text": text}]},
        "to": to, "metadata": {"hops": 1}})


def delivered(tg: FakeTg, chat_id: int) -> list[dict]:
    out = []
    for item in tg.sent:
        if item["chat"] != chat_id or not str(item["text"]).startswith("{"):
            continue
        data = json.loads(item["text"])
        if "result" in data or "error" in data:
            continue
        params = data.get("params") or {}
        text = "".join(str(x.get("text") or "")
                       for x in ((params.get("message") or {}).get("parts") or []))
        out.append({"task": (params.get("metadata") or {}).get("task"), "text": text})
    return out


def update(hub: Hub, tg: FakeTg, task_id: str, state: str, text: str = "",
           *, artifact: bool = False) -> dict:
    params: dict = {"task": task_id, "status": {"state": state}}
    if text:
        params["status"]["message"] = {"parts": [{"kind": "text", "text": text}]}
        if artifact:
            params["artifact"] = {"parts": [{"kind": "text", "text": text}]}
    return rpc_call(hub, tg, AA, "aa", "tasks/update", params)


def test_pair_and_card():
    hub, tg = new_hub()
    pair(hub, tg, bot_id=GOD, alias="god")
    assert hub.db.bot_by_alias(OWNER, "god")["bot_id"] == GOD
    card = rpc_call(hub, tg, GOD, "god", "agent/card", {})["result"]
    assert card["card"]["metadata"]["alias"] == "god"
    assert [a["alias"] for a in card["agents"]] == ["god"]
    RES["pair_and_card"] = True


def test_send_returns_immediately_auth_required():
    hub, tg = new_hub()
    pair(hub, tg, bot_id=GOD, alias="god")
    pair(hub, tg, bot_id=AA, alias="aa")
    t0 = time.time()
    res = send(hub, tg, "сколько места на диске?", to="aa")["result"]["task"]
    took = time.time() - t0
    assert took < 2, f"message/send должен возвращаться сразу, а не {took:.1f}s"
    assert res["status"]["state"] == "TASK_STATE_AUTH_REQUIRED", res
    assert res["id"]
    cards = [s for s in tg.sent if s["chat"] == OWNER and s["buttons"]]
    assert cards, "ожидалась карточка подтверждения"
    RES["send_immediate"] = True


def test_approve_delivers_and_progress_then_answer():
    hub, tg = new_hub()
    pair(hub, tg, bot_id=GOD, alias="god")
    pair(hub, tg, bot_id=AA, alias="aa")
    task_id = send(hub, tg, "проверь nginx", to="aa")["result"]["task"]["id"]
    hub.handle(cb(OWNER, f"hub:ok:{task_id}"))
    inbox = delivered(tg, AA)
    assert inbox and inbox[-1]["task"] == task_id, inbox
    r = update(hub, tg, task_id, "TASK_STATE_WORKING", "принял, выполняю")["result"]["task"]
    assert r["status"]["state"] == "TASK_STATE_WORKING"
    assert "принял" in json.dumps(r["status"], ensure_ascii=False)
    assert hub.db.task(task_id)["status"] == "working"
    update(hub, tg, task_id, "working", "выполняю, 3 мин")
    assert "3 мин" in (hub.db.task(task_id)["progress"] or "")
    assert hub.db.task(task_id)["status"] == "working"
    r = rpc_call(hub, tg, AA, "aa", "tasks/update", {
        "task": task_id, "status": {"state": "TASK_STATE_COMPLETED"},
        "artifact": {"parts": [{"kind": "text", "text": "nginx active, 2 vhost"}]}})["result"]["task"]
    assert r["status"]["state"] == "TASK_STATE_COMPLETED", r
    assert r["artifacts"][0]["parts"][0]["text"] == "nginx active, 2 vhost"
    RES["progress_then_answer"] = True


def test_input_required_and_history():
    hub, tg = new_hub()
    pair(hub, tg, bot_id=GOD, alias="god")
    pair(hub, tg, bot_id=AA, alias="aa")
    task_id = send(hub, tg, "удали лишние образы", to="aa")["result"]["task"]["id"]
    hub.handle(cb(OWNER, f"hub:ok:{task_id}"))
    update(hub, tg, task_id, "TASK_STATE_INPUT_REQUIRED", "удалять все теги или только dangling?")
    task = hub.db.task(task_id)
    assert task["status"] == "input-required"
    assert "dangling" in (task["question"] or "")
    assert any("спрашивает" in s["text"] for s in tg.sent if s["chat"] == OWNER)
    view = rpc_call(hub, tg, GOD, "god", "tasks/get", {"id": task_id})["result"]["task"]
    assert view["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    assert view["history"], "история должна содержать ходы"
    assert any("удали лишние" in json.dumps(m, ensure_ascii=False) for m in view["history"])
    RES["input_required"] = True


def test_list_cursor_and_sort():
    hub, tg = new_hub()
    pair(hub, tg, bot_id=GOD, alias="god")
    pair(hub, tg, bot_id=AA, alias="aa")
    ids = [send(hub, tg, f"задача {i}", to="aa")["result"]["task"]["id"] for i in range(3)]
    page = rpc_call(hub, tg, GOD, "god", "tasks/list", {"limit": 2})["result"]
    assert len(page["tasks"]) == 2, page
    times = [t["metadata"]["updatedAt"] for t in page["tasks"]]
    assert times == sorted(times, reverse=True), times
    page2 = rpc_call(hub, tg, GOD, "god", "tasks/list",
                     {"limit": 2, "cursor": page["nextCursor"]})["result"]
    got = {t["id"] for t in page["tasks"]} | {t["id"] for t in page2["tasks"]}
    assert set(ids) <= got, (ids, got)
    RES["list_cursor"] = True


def test_cancel_propagates():
    hub, tg = new_hub()
    pair(hub, tg, bot_id=GOD, alias="god")
    pair(hub, tg, bot_id=AA, alias="aa")
    task_id = send(hub, tg, "долгая задача", to="aa")["result"]["task"]["id"]
    hub.handle(cb(OWNER, f"hub:ok:{task_id}"))
    r = rpc_call(hub, tg, GOD, "god", "tasks/cancel", {"id": task_id})["result"]["task"]
    assert r["status"]["state"] == "TASK_STATE_CANCELED"
    notes = [s for s in tg.sent if s["chat"] == AA and "cancel" in str(s["text"])]
    assert notes, "исполнитель должен получить отмену"
    again = rpc_call(hub, tg, GOD, "god", "tasks/cancel", {"id": task_id})
    assert "error" in again, "повторная отмена — ошибка"
    RES["cancel"] = True


def test_reject_card():
    hub, tg = new_hub()
    pair(hub, tg, bot_id=GOD, alias="god")
    pair(hub, tg, bot_id=AA, alias="aa")
    task_id = send(hub, tg, "не надо", to="aa")["result"]["task"]["id"]
    hub.handle(cb(OWNER, f"hub:no:{task_id}"))
    assert hub.db.task(task_id)["status"] == "rejected"
    assert not delivered(tg, AA)
    RES["reject"] = True


def test_auto_policy_skips_card():
    hub, tg = new_hub()
    pair(hub, tg, bot_id=GOD, alias="god")
    pair(hub, tg, bot_id=AA, alias="aa")
    hub.db.set_route(OWNER, "god", "aa", policy="auto")
    res = send(hub, tg, "просто спроси", to="aa")["result"]["task"]
    assert res["status"]["state"] in ("TASK_STATE_WORKING", "TASK_STATE_AUTH_REQUIRED"), res
    assert delivered(tg, AA), "при auto доставка идёт сразу"
    RES["auto_policy"] = True


def test_dedup_by_message_id():
    hub, tg = new_hub()
    pair(hub, tg, bot_id=GOD, alias="god")
    pair(hub, tg, bot_id=AA, alias="aa")
    first = send(hub, tg, "вопрос про диск", to="aa", message_id="m-fixed")["result"]
    second = send(hub, tg, "вопрос про диск", to="aa", message_id="m-fixed")["result"]
    assert second.get("deduplicated") is True
    assert first["task"]["id"] == second["task"]["id"]
    RES["dedup"] = True


def test_card_ttl_expires():
    hub, tg = new_hub()
    pair(hub, tg, bot_id=GOD, alias="god")
    pair(hub, tg, bot_id=AA, alias="aa")
    task_id = send(hub, tg, "протухнет", to="aa")["result"]["task"]["id"]
    hub.db.run("UPDATE tasks SET created_at=created_at-3600 WHERE task_id=?", (task_id,))
    hub.expire_all()
    assert hub.db.task(task_id)["status"] == "failed"
    view = rpc_call(hub, tg, GOD, "god", "tasks/get", {"id": task_id})["result"]["task"]
    assert view["status"]["state"] == "TASK_STATE_FAILED"
    RES["ttl"] = True


def test_menu_sections():
    hub, tg = new_hub()
    pair(hub, tg, bot_id=GOD, alias="god")
    hub.handle(cb(OWNER, "hub:menu:main"))
    assert tg.sent[-1]["buttons"], "главное меню показывает кнопки"
    for section in ("bots", "routes", "log"):
        hub.handle(cb(OWNER, f"hub:menu:{section}"))
        assert tg.sent[-1]["buttons"], f"раздел {section} должен показать кнопки"
    RES["menu"] = True


def test_explicit_routes_hide_bots():
    hub, tg = new_hub()
    pair(hub, tg, bot_id=GOD, alias="god")
    pair(hub, tg, bot_id=AA, alias="aa", routes_mode="explicit")
    res = send(hub, tg, "скрытый адресат", to="god", bot_id=AA, username="aa")
    assert "error" in res, res
    hub.db.set_route(OWNER, "aa", "god", policy="ask")
    res = send(hub, tg, "теперь можно", to="god", bot_id=AA, username="aa")
    assert "result" in res, res
    RES["explicit_routes"] = True


def main() -> int:
    for fn in (test_pair_and_card, test_send_returns_immediately_auth_required,
               test_approve_delivers_and_progress_then_answer, test_input_required_and_history,
               test_list_cursor_and_sort, test_cancel_propagates, test_reject_card,
               test_auto_policy_skips_card, test_dedup_by_message_id, test_card_ttl_expires,
               test_menu_sections, test_explicit_routes_hide_bots):
        fn()
    print("hub:", json.dumps(RES, ensure_ascii=False, sort_keys=True))
    print(f"OK: {len(RES)} групп хаба")
    return 0


if __name__ == "__main__":
    sys.exit(main())
