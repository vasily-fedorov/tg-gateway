#!/usr/bin/env python3
"""Unit tests for gateway-relay 0.2 (tasks + approval cards + hook gates).

Run: python3 tests/test_gateway_relay_hook.py
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "core.py"
sys.path.insert(0, str(ROOT))

GROUP = "-1004418865255"
PEER_BOT_ID = "300000003"
OTHER_BOT_ID = "1234567890"
OWNER = "100000001"


def load():
    spec = importlib.util.spec_from_file_location("relay_under_test", CORE)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def make_event(text: str, *, is_bot: bool, user_id: str, chat_id: str = GROUP,
               thread_id: str | None = "2", raw_text: str | None = None,
               reply_to: int | None = None):
    src = SimpleNamespace(platform="telegram", user_id=user_id, chat_id=chat_id,
                          thread_id=thread_id, is_bot=is_bot)
    raw = SimpleNamespace(text=raw_text if raw_text is not None else text)
    if reply_to is not None:
        raw.reply_to_message = SimpleNamespace(message_id=reply_to)
    return SimpleNamespace(text=text, source=src, message_id=77, raw_message=raw)


def run() -> dict:
    mod = load()
    tmp = Path(tempfile.mkdtemp(prefix="gwrelay-"))
    store = mod.task_state.TaskStore(tmp / "state.json")
    sent, audited, cards, edits = [], [], [], []

    def routes():
        return {
            "chat_id": int(GROUP),
            "approvals_thread": 1,
            "owner_ids": [int(OWNER)],
            "limits": {"max_text": 3000, "per_hour": 20},
            "self": {"bot_username": "gateway_alpha", "bot_id": 200000002},
            "bots": {"gateway_beta": {"title": "peer", "bot_username": "gateway_beta",
                                    "bot_id": int(PEER_BOT_ID), "chat_id": int(GROUP),
                                    "thread_id": 2, "enabled": True}},
        }

    mod._routes = routes
    mod._audit = lambda event, **kw: audited.append((event, kw))
    mod._store = lambda: store
    mod._tg_send = lambda chat, thread, text, reply_to=None, reply_markup=None: (
        sent.append((chat, text)) or (True, "m1"))
    mod._send_card = lambda text, buttons: (cards.append((text, buttons)) or "card1")
    mod._send_card_ex = lambda text, buttons: (cards.append((text, buttons)) or (str(GROUP), "card1"))
    mod._finish_card = lambda chat, msg, status: edits.append(status)
    mod._tg_api = lambda method, **params: (True, {"message_id": 1})

    async def call(ev):
        return await mod.pre_gateway_dispatch(ev)

    def tasks(direction=None, status=None):
        data = store.read()
        out = list((data.get("tasks") or {}).values())
        if direction:
            out = [t for t in out if t.get("direction") == direction]
        if status:
            out = [t for t in out if t.get("status") == status]
        return out

    res = {}

    # 1. bot from a foreign chat
    r = asyncio.run(call(make_event("hi", is_bot=True, user_id=PEER_BOT_ID, chat_id="-1009999999999")))
    assert r == {"action": "skip", "reason": "relay:bot-outside-group"}, r
    assert audited[-1][0] == "bot_outside_group"
    res["bot_outside_group"] = True

    # 2. bot outside the registry
    sent.clear()
    r = asyncio.run(call(make_event("hi", is_bot=True, user_id=OTHER_BOT_ID)))
    assert r == {"action": "skip", "reason": "relay:bot-not-allowed"}, r
    assert sent and sent[0][0] == int(OWNER)
    res["bot_not_allowed"] = True

    # 3. peer with @mention -> task in `submitted` + card
    cards.clear()
    r = asyncio.run(call(make_event("@gateway_alpha передай привет", is_bot=True, user_id=PEER_BOT_ID)))
    assert r == {"action": "skip", "reason": "relay:inbound-parked"}, r
    assert audited[-1][0] == "bot_inbound_parked"
    inbound = tasks("in", "submitted")
    assert len(inbound) == 1, inbound
    task = inbound[0]
    assert cards and cards[0][1][0][1] == f"gwin:ok:{task['task_id']}"
    res["inbound_parked_as_task"] = True

    # 3b. own @handle stripped by Telegram -> raw_message still has it
    r = asyncio.run(call(make_event("передай привет", is_bot=True, user_id=PEER_BOT_ID,
                                    raw_text="@gateway_alpha передай привет")))
    assert r == {"action": "skip", "reason": "relay:inbound-parked"}, r
    res["mention_from_raw_text"] = True

    # 3c. cancel notice from a peer -> no task, no card
    cards.clear()
    r = asyncio.run(call(make_event("@gateway_alpha [gw ctx:ab12 #cancel] ↩️ задача отменена",
                                    is_bot=True, user_id=PEER_BOT_ID)))
    assert r == {"action": "skip", "reason": "relay:cancel-notice"}, r
    assert audited[-1][0] == "cancel_notice"
    assert not cards, cards
    res["cancel_notice_skipped"] = True

    # 4. no mention
    r = asyncio.run(call(make_event("просто болтовня", is_bot=True, user_id=PEER_BOT_ID)))
    assert r == {"action": "skip", "reason": "relay:bot-no-mention"}, r
    res["bot_no_mention"] = True

    # 4b. hop budget via the context marker
    r = asyncio.run(call(make_event("@gateway_alpha [gw ctx:ab12 #4] ещё раз", is_bot=True,
                                    user_id=PEER_BOT_ID)))
    assert r == {"action": "skip", "reason": "relay:hop-limit"}, r
    assert audited[-1][0] == "hop_limit"
    res["hop_limit"] = True

    # 5. foreign clarify card / own card echo
    card = '❓ Принять сообщение?\n\n1. Принять (Recommended)\n\nReply "skip" to skip this question.'
    assert asyncio.run(call(make_event(card, is_bot=True, user_id=PEER_BOT_ID)))["reason"] == "relay:bot-ui-noise"
    assert asyncio.run(call(make_event(mod._CARD_MARK + "x", is_bot=True,
                                       user_id=PEER_BOT_ID)))["reason"] == "relay:bot-ui-noise"
    res["bot_ui_noise"] = True

    # 6. mention rule for humans
    assert asyncio.run(call(make_event("пинг", is_bot=False, user_id=OWNER)))["reason"] == "relay:no-mention"
    assert asyncio.run(call(make_event("@gateway_alpha пинг", is_bot=False, user_id=OWNER))) is None
    res["mention_rule"] = True

    # 7. human DM passes through
    assert asyncio.run(call(make_event("привет", is_bot=False, user_id=OWNER,
                                       chat_id=OWNER, thread_id=None))) is None
    res["human_dm_passthrough"] = True

    # 8. outbound: task submitted, card with "Отправить", nothing sent
    cards.clear()
    sent.clear()
    out = json.loads(mod.tool_send({"target": "gateway_beta", "text": "Сколько времени?"}))
    assert out["status"] == "pending_owner_approval", out
    assert tasks("out", "submitted"), tasks()
    assert cards and cards[0][1][0][1] == f"gwout:ok:{out['task_id']}"
    assert not sent
    res["outbound_parked_as_task"] = True

    # 9. click "Отправить" -> working -> completed, delivered with the context marker
    edits.clear()
    status = mod.resolve_card_click(f"gwout:ok:{out['task_id']}", OWNER, "cb1", str(GROUP), "card1")
    assert "Отправлено" in status, status
    assert sent and "@gateway_beta" in sent[0][1] and "gw ctx:" in sent[0][1], sent
    assert tasks("out", "completed"), tasks("out")
    res["outbound_card_click_completes"] = True

    # 10. non-owner click refused
    sent.clear()
    assert mod.resolve_card_click(f"gwout:ok:{out['task_id']}", OTHER_BOT_ID, "cb2",
                                  str(GROUP), "card1") == "denied"
    assert not sent
    res["callback_owner_only"] = True

    # 11. inbound accept -> working (the drain picks it up)
    status = mod.resolve_card_click(f"gwin:ok:{task['task_id']}", OWNER, "cb3", str(GROUP), "card1")
    assert "Принято" in status, status
    assert tasks("in", "working"), tasks("in")
    res["inbound_accept_working"] = True

    # 12. inbound reject -> rejected, nothing sent
    mod._send_card_ex = lambda text, buttons: (cards.append((text, buttons)) or (str(GROUP), "card2"))
    asyncio.run(call(make_event("@gateway_alpha второй", is_bot=True, user_id=PEER_BOT_ID)))
    second = tasks("in", "submitted")[0]
    sent.clear()
    status = mod.resolve_card_click(f"gwin:no:{second['task_id']}", OWNER, "cb4", str(GROUP), "card2")
    assert "отклонён" in status.lower(), status
    assert not sent
    res["inbound_reject"] = True

    # 13. cancel card -> cancelled + notification to the peer
    mod._send_card_ex = lambda text, buttons: (cards.append((text, buttons)) or (str(GROUP), "card3"))
    third = json.loads(mod.tool_send({"target": "gateway_beta", "text": "отмени меня"}))
    sent.clear()
    status = mod.resolve_card_click(f"gwcancel:ok:{third['task_id']}", OWNER, "cb5", str(GROUP), "card3")
    assert "отменена" in status, status
    assert sent and "отменена инициатором" in sent[0][1], sent
    assert any(t["status"] == "cancelled" for t in tasks("out")), tasks("out")
    res["cancel_with_notice"] = True

    # 14. ASK answer via a reply to the ASK card
    mod._send_card_ex = lambda text, buttons: (cards.append((text, buttons)) or (str(GROUP), "card4"))
    asyncio.run(call(make_event("@gateway_alpha вопрос про время", is_bot=True, user_id=PEER_BOT_ID)))
    ask_task = tasks("in", "submitted")[-1]
    with store.mutate() as data:
        mod.task_state.TaskStore.transition(data, ask_task["task_id"], "working")
        mod.task_state.TaskStore.transition(data, ask_task["task_id"], "input-required",
                                            question="какой город?", card_chat=str(GROUP),
                                            card_msg="9")
    mod._reply = lambda chat, thread, text: sent.append((chat, text))
    r = asyncio.run(call(make_event("Москва", is_bot=False, user_id=OWNER, thread_id="1",
                                    reply_to=9)))
    assert r == {"action": "skip", "reason": "relay:ask-answered"}, r
    assert any(t["status"] == "working" and t.get("answer") == "Москва"
               for t in tasks("in")), tasks("in")
    res["ask_answer_by_reply"] = True

    # 15. drain: ASK -> свой default-профиль -> DONE -> ответ пиру
    with store.mutate() as data:
        for t in (data.get("tasks") or {}).values():
            if t.get("status") == "working" and t.get("direction") == "in":
                mod.task_state.TaskStore.transition(data, t["task_id"], "skipped")
    asyncio.run(call(make_event("@gateway_alpha сколько свободно на диске / ?", is_bot=True,
                                user_id=PEER_BOT_ID)))
    disk = tasks("in", "submitted")[-1]
    mod.resolve_card_click(f"gwin:ok:{disk['task_id']}", OWNER, "cb6", str(GROUP), "cardA")
    turns = ["ASK: доступа к диску нет, нужен владелец", "DONE: свободно 30 ГБ"]
    mod._run_profile_turn = lambda body: turns.pop(0) if turns else ""
    mod._ask_own_default = lambda q: "на / свободно 30 ГБ"
    sent.clear()
    done = mod.drain_approved_in()
    assert done == 1, done
    assert sent and "30 ГБ" in sent[0][1], sent
    assert any(t["task_id"] == disk["task_id"] and t["status"] == "completed" for t in tasks("in"))
    res["drain_ask_via_default"] = True

    # 16. drain: ASK, default не смог -> карточка владельцу, input-required
    asyncio.run(call(make_event("@gateway_alpha кому принадлежит домен?", is_bot=True,
                                user_id=PEER_BOT_ID)))
    stuck = tasks("in", "submitted")[-1]
    mod.resolve_card_click(f"gwin:ok:{stuck['task_id']}", OWNER, "cb7", str(GROUP), "cardA")
    turns = ["ASK: не знаю, нужен владелец", "ASK: без владельца не ответить"]
    mod._ask_own_default = lambda q: ""
    cards.clear()
    sent.clear()
    mod.drain_approved_in()
    assert not sent, sent
    assert cards, "no owner card"
    state = store.read()
    assert state["tasks"][stuck["task_id"]]["status"] == "input-required"
    assert state["tasks"][stuck["task_id"]]["card_msg"]
    res["drain_ask_escalates_to_owner"] = True

    # 17. the quiet-turn reply parsing still works
    raw = "────\n\u2624 Hermes\nDONE: 42\n────\n\nSession: local-x\n"
    assert mod._extract_reply(raw) == "DONE: 42", mod._extract_reply(raw)
    res["cli_reply_parsing"] = True

    return res


def main() -> int:
    res = run()
    print("gateway-relay: " + json.dumps(res, ensure_ascii=False))
    print(f"OK: {len(res)} assertion groups passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
