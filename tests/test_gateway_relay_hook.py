#!/usr/bin/env python3
"""Unit tests for gateway-relay (approval cards + hook gates).

Run: python3 tests/test_gateway_relay_hook.py
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
PLUGINS = {
    "gateway-relay": ROOT / "core.py",
}

GROUP = "-1004418865255"
PEER_BOT_ID = "300000003"     # gateway_beta
OTHER_BOT_ID = "1234567890"    # not in the registry
OWNER = "100000001"


def load(path: Path, tag: str):
    spec = importlib.util.spec_from_file_location(f"relay_{tag}", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def make_event(text: str, *, is_bot: bool, user_id: str, chat_id: str = GROUP,
               thread_id: str | None = "2", raw_text: str | None = None):
    src = SimpleNamespace(platform="telegram", user_id=user_id, chat_id=chat_id,
                          thread_id=thread_id, is_bot=is_bot)
    raw = SimpleNamespace(text=raw_text if raw_text is not None else text)
    return SimpleNamespace(text=text, source=src, message_id=77, raw_message=raw)


def run(mod):
    sent, audited, cards, edits = [], [], [], []
    state = {"pending_out": {}, "pending_in": {}, "approved_in": {}, "inbox": [], "sent": []}

    def routes():
        return {
            "chat_id": int(GROUP),
            "approvals_thread": 82,
            "owner_ids": [int(OWNER)],
            "limits": {"max_text": 3000, "per_hour": 20},
            "self": {"bot_username": "gateway_alpha", "bot_id": 200000002},
            "bots": {
                "gateway_beta": {
                    "title": "gateway_beta (example-agent)", "bot_username": "gateway_beta",
                    "bot_id": int(PEER_BOT_ID), "chat_id": int(GROUP),
                    "thread_id": 2, "enabled": True,
                }
            },
        }

    mod._routes = routes
    mod._audit = lambda event, **kw: audited.append((event, kw))
    mod._state = lambda: state
    mod._write_json = lambda *a, **k: None
    mod._read_json = lambda *a, **k: {}
    mod._tg_send = lambda chat, thread, text, reply_to=None, reply_markup=None: (
        sent.append((chat, text)) or (True, "m1"))
    mod._send_card = lambda text, buttons: (cards.append((text, buttons)) or "card1")
    mod._finish_card = lambda chat, msg, status: edits.append(status)
    mod._tg_api = lambda method, **params: (True, {"message_id": 1})

    async def call(ev):
        return await mod.pre_gateway_dispatch(ev)

    results = {}

    # 1. bot from a foreign chat -> skip
    r = asyncio.run(call(make_event("hi", is_bot=True, user_id=PEER_BOT_ID, chat_id="-1009999999999")))
    assert r == {"action": "skip", "reason": "relay:bot-outside-group"}, r
    assert audited[-1][0] == "bot_outside_group"
    results["bot_outside_group"] = True

    # 2. bot outside the registry -> skip + owner notified
    sent.clear()
    r = asyncio.run(call(make_event("hi", is_bot=True, user_id=OTHER_BOT_ID)))
    assert r == {"action": "skip", "reason": "relay:bot-not-allowed"}, r
    assert audited[-1][0] == "bot_not_allowed"
    assert sent and sent[0][0] == int(OWNER), sent
    results["bot_not_allowed"] = True

    # 3. registered peer with @mention -> parked, approval card in the approvals topic
    cards.clear()
    r = asyncio.run(call(make_event("@gateway_alpha передай привет", is_bot=True, user_id=PEER_BOT_ID)))
    assert r == {"action": "skip", "reason": "relay:inbound-parked"}, r
    assert audited[-1][0] == "bot_inbound_parked"
    assert cards and cards[0][1][0][0] == "Принять" and cards[0][1][0][1].startswith("gwin:ok:")
    req = next(iter(state["pending_in"]))
    results["inbound_parked_with_card"] = True

    # 3b. Telegram strips our own @handle for the addressee: the raw message still has it
    r = asyncio.run(call(make_event("передай привет", is_bot=True, user_id=PEER_BOT_ID,
                                    raw_text="@gateway_alpha передай привет")))
    assert r == {"action": "skip", "reason": "relay:inbound-parked"}, r
    results["mention_from_raw_text"] = True

    # 4. bot without our mention -> skip
    r = asyncio.run(call(make_event("просто болтовня", is_bot=True, user_id=PEER_BOT_ID)))
    assert r == {"action": "skip", "reason": "relay:bot-no-mention"}, r
    assert audited[-1][0] == "bot_no_mention"
    results["bot_no_mention"] = True

    # 4b. hop budget: a chain deeper than 3 is never continued
    r = asyncio.run(call(make_event("@gateway_alpha [gw-hop:4] и снова здравствуйте", is_bot=True,
                                    user_id=PEER_BOT_ID)))
    assert r == {"action": "skip", "reason": "relay:hop-limit"}, r
    assert audited[-1][0] == "hop_limit"
    results["hop_limit"] = True

    # 5. another bot's clarify card -> UI noise
    card = '❓ Принять сообщение?\n\n1. Принять (Recommended)\n\nReply "skip" to skip this question.'
    r = asyncio.run(call(make_event(card, is_bot=True, user_id=PEER_BOT_ID)))
    assert r == {"action": "skip", "reason": "relay:bot-ui-noise"}, r
    results["bot_ui_noise"] = True

    # 5b. our own card echoed back -> UI noise
    r = asyncio.run(call(make_event(mod._CARD_MARK + "Принять?", is_bot=True, user_id=PEER_BOT_ID)))
    assert r == {"action": "skip", "reason": "relay:bot-ui-noise"}, r
    results["own_card_noise"] = True

    # 6. human in the group without a mention -> not ours; with a mention -> ours
    r = asyncio.run(call(make_event("пинг", is_bot=False, user_id=OWNER)))
    assert r == {"action": "skip", "reason": "relay:no-mention"}, r
    assert audited[-1][0] == "no_mention"
    r = asyncio.run(call(make_event("@gateway_alpha пинг", is_bot=False, user_id=OWNER)))
    assert r is None, r
    results["mention_rule"] = True

    # 7. human DM -> falls through to the agent
    r = asyncio.run(call(make_event("привет", is_bot=False, user_id=OWNER,
                                    chat_id=OWNER, thread_id=None)))
    assert r is None, r
    results["human_dm_passthrough"] = True

    # 8. outbound: relay_send parks and posts an "Отправить" card, nothing sent yet
    cards.clear()
    sent.clear()
    out = json.loads(mod.tool_send({"target": "gateway_beta", "text": "Сколько времени?"}))
    assert out["status"] == "pending_owner_approval", out
    assert cards and cards[0][1][0][1].startswith("gwout:ok:"), cards
    assert not sent, sent
    out_req = out["request"]
    results["outbound_parked"] = True

    # 9. owner clicks "Отправить" -> delivered now, card edited
    edits.clear()
    status = mod.resolve_card_click(f"gwout:ok:{out_req}", OWNER, "cb1", str(GROUP), "card1")
    assert "Отправлено" in status, status
    assert sent and "@gateway_beta" in sent[0][1], sent
    assert edits, edits
    assert state["pending_out"] == {}, state
    results["outbound_card_click_sends"] = True

    # 10. non-owner click -> denied, nothing sent
    sent.clear()
    status = mod.resolve_card_click("gwout:ok:deadbeef", OTHER_BOT_ID, "cb2", str(GROUP), "card1")
    assert status == "denied", status
    assert not sent
    results["callback_owner_only"] = True

    # 11. inbound accept moves the parked item to approved_in (the watcher injects)
    status = mod.resolve_card_click(f"gwin:ok:{req}", OWNER, "cb3", str(GROUP), "card1")
    assert "Принято" in status, status
    assert req in state["approved_in"], state
    results["inbound_accept_queues_injection"] = True

    # 11b. the quiet profile turn's answer is parsed out of the CLI banner
    raw = "────\n\u2624 Hermes\nГотово: 42\n────\n\nSession: local-x\n"
    assert mod._extract_reply(raw) == "Готово: 42", mod._extract_reply(raw)
    assert mod._extract_reply("\x1b[36m\u2624 Hermes\x1b[0m\n\x1b[1mok\x1b[0m\n────\n") == "ok"
    results["cli_reply_parsing"] = True

    # 12. inbound reject drops it
    state["pending_in"]["zzz"] = {"text": "x", "created": mod.time.time()}
    status = mod.resolve_card_click("gwin:no:zzz", OWNER, "cb4", str(GROUP), "card1")
    assert "отклонён" in status.lower(), status
    assert "zzz" not in state["pending_in"] and "zzz" not in state["approved_in"]
    results["inbound_reject_drops"] = True

    return results


def main() -> int:
    total = 0
    for tag, path in PLUGINS.items():
        assert path.is_file(), path
        mod = load(path, tag)
        res = run(mod)
        print(f"{tag}: {json.dumps(res, ensure_ascii=False)}")
        total += len(res)
    print(f"OK: {total} assertion groups passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
