"""gateway-relay core: allowlisted, human-approved relay between bots.

Security model (defence in depth, none of it depends on the LLM behaving):

1. DESTINATIONS ARE STRUCTURAL. The model can only name a route key that already
   exists in ``gateway-routes.json``. It cannot invent a chat_id, a thread_id or a URL.
2. SENDING IS GATED BY A NATIVE clarify CARD. ``relay_send`` is called only after
   the owner picks «Отправить» in clarify-карточке; the tool itself applies the allowlist,
   the leak/injection filters and the rate limit. There is no code/APPROVE path.
3. CONTENT IS FILTERED before parking: credentials/keys/passwords/long blobs are
   refused outright (leak control), prompt-injection payloads are refused (the
   relay must not become a launderer for instructions aimed at another bot).
4. EVERYTHING IS AUDITED to ``logs/gateway-relay-audit.jsonl`` — hash + preview,
   never the full text.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
import subprocess
import threading
import time
from pathlib import Path

try:  # normal package import (plugin loaded as a package)
    from . import state as task_state
except ImportError:  # loaded as a standalone module (tests, ingress copy)
    import importlib.util as _ilu
    _spec_state = _ilu.spec_from_file_location("gateway_relay_state",
                                               Path(__file__).with_name("state.py"))
    task_state = _ilu.module_from_spec(_spec_state)
    _spec_state.loader.exec_module(task_state)
from typing import Any, Dict, Optional, Tuple

CODE_TTL_S = 900           # approval window
MAX_TEXT = 3000            # characters per relayed message
PER_HOUR = 20              # relays per rolling hour
SSRF_HOST_ALLOW = re.compile(r"^https?://(127\.0\.0\.1|localhost|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)", re.I)

_LOCK = threading.RLock()

# --- approval cards in the approvals topic -----------------------------------
_CARD_MARK = "\U0001f6e1 gateway \u00b7 "      # every card starts with this marker
_CARD_TTL_S = task_state.CARD_TTL_S            # the card clock lives in state.py
_WATCH: dict[str, bool] = {}

# --- leak / injection filters -------------------------------------------------
_LEAK_PATTERNS: list[tuple[str, str]] = [
    (r"(?<!\d)\d{8,12}:[A-Za-z0-9_-]{30,}(?![A-Za-z0-9_-])", "Telegram bot token"),
    (r"(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{16,}", "API-ключ (sk-…)"),
    (r"(?<![A-Za-z0-9])AKIA[0-9A-Z]{16}(?![A-Z0-9])", "AWS access key"),
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "приватный ключ"),
    (r"(?i)\b(password|passwd|пароль)\b\s*[:=]\s*\S+", "пароль в открытом виде"),
    (r"(?i)\b(api[_-]?key|secret|client[_-]?secret|access[_-]?token)\b\s*[:=]\s*[A-Za-z0-9_\-]{12,}", "секрет в открытом виде"),
    (r"(?i)\bbearer\s+[A-Za-z0-9_\-\.]{16,}", "bearer-токен"),
    (r"(?i)\b[a-z][a-z0-9+.-]*://[^\s:/@]+:[^\s@/]+@", "креды в URL (user:pass@host)"),
    (r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{200,}={0,2}(?![A-Za-z0-9+/=])", "длинный base64-блок"),
    (r"(?<![0-9a-fA-F])[0-9a-fA-F]{64,}(?![0-9a-fA-F])", "длинный hex-ключ"),
]

_INJECTION_PATTERNS: list[tuple[str, str]] = [
    (r"(?i)ignore\s+(all\s+)?(previous|prior|above)\s+instructions", "попытка перехвата инструкций"),
    (r"(?i)disregard\s+(all\s+)?(previous|prior|above)", "попытка перехвата инструкций"),
    (r"(?i)\bnew\s+instructions?\s*:", "подмена инструкций"),
    (r"(?i)\b(system|developer)\s*:\s*", "подмена роли (system/developer:)"),
    (r"(?i)(покажи|выведи|раскрой|скажи)\s+(свой\s+)?(системн\w*\s+)?промпт", "запрос системного промпта"),
    (r"(?i)ты\s+теперь\s+(не|другой|другая)", "подмена роли"),
    (r"(?i)\b(reveal|print|show)\s+(your\s+)?(system\s+)?prompt", "запрос системного промпта"),
]


def _home() -> Path:
    """Data home for the relay: the `gateway` PROFILE home, whoever is asking.

    The approval hook runs at gateway ingress under the DEFAULT profile's plugin
    manager, while the tools run inside the routed `gateway` profile's turn. Both
    must read the same routes/pending files, so resolve to the gateway profile
    explicitly (GATEWAY_RELAY_HOME overrides, used by tests)."""
    override = os.environ.get("GATEWAY_RELAY_HOME")
    if override:
        return Path(override)
    candidates: list[Path] = []
    try:
        from hermes_constants import get_hermes_home
        here = Path(get_hermes_home())
        candidates += [here / "profiles" / "gateway", here]
    except Exception:
        pass
    candidates.append(Path(__file__).resolve().parents[2])
    for cand in candidates:
        if (cand / "gateway-routes.json").is_file():
            return cand
    for cand in candidates:
        if (cand / "plugins" / "gateway-relay").is_dir():
            return cand
    return candidates[0] if candidates else Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))


def _store() -> "task_state.TaskStore":
    return task_state.TaskStore(_state_path())


def _routes_path() -> Path:
    return _home() / "gateway-routes.json"


def _hub_cfg() -> Dict[str, Any]:
    """{"bot_id": int, "username": str} если профиль работает как endpoint хаба."""
    data = _routes()
    if str(data.get("mode") or "").strip().lower() != "endpoint":
        return {}
    hub = data.get("hub") or {}
    if not hub.get("bot_id"):
        return {}
    return {"bot_id": str(hub.get("bot_id")), "username": str(hub.get("username") or "")}


def _to_hub(text: str) -> bool:
    """Отправить JSON-конверт хабу в ЛС. True, если доставлено."""
    hub = _hub_cfg()
    if not hub:
        return False
    chat = int(hub["bot_id"]) if str(hub["bot_id"]).isdigit() else hub["bot_id"]
    ok, info = _tg_send(chat, None, text, None)
    if not ok:
        _audit("hub_send_failed", error=str(info)[:200])
    return bool(ok)


def _hub_rpc(method: str, params: Dict[str, Any], *, timeout: float = 8.0) -> Dict[str, Any]:
    """JSON-RPC вызов хаба по Telegram: отправляем запрос и ждём ответ в state["rpc"].

    Хук этого же процесса принимает ответ от хаба и складывает его в state, поэтому
    ожидание — короткий поллинг файла состояния, без сети и без блокировки модели.
    """
    hub = _hub_cfg()
    if not hub:
        return {"error": "профиль не в endpoint-режиме"}
    rpc_id = "r-" + secrets.token_hex(4)
    payload = {"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params}
    if not _to_hub(json.dumps(payload, ensure_ascii=False)):
        return {"error": "хаб недоступен"}
    deadline = time.time() + timeout
    while time.time() < deadline:
        got = (_store().read().get("rpc") or {}).get(rpc_id)
        if isinstance(got, dict):
            if "result" in got:
                return got["result"] if isinstance(got["result"], dict) else {"result": got["result"]}
            return {"error": (got.get("error") or {}).get("message", "ошибка хаба")}
        time.sleep(0.4)
    return {"error": "хаб не ответил вовремя"}


def _state_path() -> Path:
    return _home() / "gateway-relay-state.json"


def _audit_path() -> Path:
    p = _home() / "logs" / "gateway-relay-audit.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _read_json(path: Path, default: Any) -> Any:
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


def _write_json(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _routes() -> Dict[str, Any]:
    data = _read_json(_routes_path(), {})
    if not isinstance(data, dict) or "bots" not in data:
        data = {"version": 1, "chat_id": None, "owner_ids": [], "bots": {}, "limits": {}}
    data.setdefault("bots", {})
    data.setdefault("owner_ids", [])
    return data


def _state() -> Dict[str, Any]:
    """Whole persisted document: tasks + rate-limit stamps + last bot ids."""
    data = _store().read()
    data.setdefault("tasks", {})
    data.setdefault("sent", [])
    return data


def _audit(event: str, **fields: Any) -> None:
    row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "event": event, **fields}
    try:
        with _audit_path().open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def scan_text(text: str) -> Optional[str]:
    """Return a refusal reason, or None when the text is safe to relay."""
    for pattern, label in _LEAK_PATTERNS:
        if re.search(pattern, text):
            return f"похоже на утечку секрета: {label}"
    for pattern, label in _INJECTION_PATTERNS:
        if re.search(pattern, text):
            return f"отклонено фильтром: {label}"
    return None


def _rate_ok(state: Dict[str, Any], limit: int) -> Tuple[bool, int]:
    now = time.time()
    sent = [t for t in state.get("sent", []) if isinstance(t, (int, float)) and now - t < 3600]
    state["sent"] = sent
    return len(sent) < limit, len(sent)


def _secret(name: str) -> Optional[str]:
    try:
        from agent.secret_scope import get_secret
        value = get_secret(name)
        if value:
            return value
    except Exception:
        pass
    value = os.environ.get(name)
    if value:
        return value
    # last resort: the profile's own .env
    try:
        for line in (_home() / ".env").read_text(encoding="utf-8").splitlines():
            if line.strip().startswith(name + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return None


def _tg_send(chat_id: Any, thread_id: Any, text: str, reply_to: Any = None,
             reply_markup: Optional[dict] = None) -> Tuple[bool, str]:
    token = _secret("TELEGRAM_BOT_TOKEN")
    if not token:
        return False, "TELEGRAM_BOT_TOKEN не задан в профиле gateway"
    cmd = ["curl", "-sS", "--max-time", "30",
           "https://api.telegram.org/bot" + token + "/sendMessage",
           "--data-urlencode", f"chat_id={chat_id}",
           "--data-urlencode", f"text={text}",
           "--data-urlencode", "disable_web_page_preview=true"]
    if thread_id not in (None, "", 0, "0"):
        cmd += ["--data-urlencode", f"message_thread_id={thread_id}"]
    if reply_to not in (None, "", 0, "0"):
        cmd += ["--data-urlencode", f"reply_to_message_id={reply_to}"]
    if reply_markup:
        cmd += ["--data-urlencode", "reply_markup=" + json.dumps(reply_markup, ensure_ascii=False)]
    proxy = _secret("TELEGRAM_PROXY")
    if proxy:
        # insert AFTER "--max-time 30" — before it, "--proxy" becomes the timeout value
        cmd[4:4] = ["--proxy", proxy]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
        payload = json.loads(out.stdout or "{}")
        if payload.get("ok"):
            return True, str(payload.get("result", {}).get("message_id", ""))
        return False, str(payload.get("description") or out.stderr or "telegram error")[:300]
    except Exception as exc:  # pragma: no cover - network
        return False, f"send failed: {exc}"


def _api_send(route: Dict[str, Any], text: str) -> Tuple[bool, str]:
    """Deliver to another Hermes agent's api_server (OpenAI-compatible /v1/chat/completions)."""
    url = str(route.get("api_url") or "")
    if not url or not SSRF_HOST_ALLOW.match(url):
        return False, "api_url должен указывать на локальную/приватную сеть"
    key = _secret(str(route.get("api_key_env") or "GATEWAY_RELAY_API_KEY")) or ""
    body = json.dumps({
        "model": route.get("api_model") or "hermes",
        "messages": [{"role": "user", "content": text}],
        "stream": False,
    }).encode("utf-8")
    cmd = ["curl", "-sS", "--max-time", "180", "-X", "POST", url,
           "-H", "Content-Type: application/json",
           "-H", "Authorization: Bearer " + key,
           "--data-binary", "@-"]
    # private/loopback targets are called directly, never through an egress proxy
    try:
        out = subprocess.run(cmd, input=body, capture_output=True, timeout=200)
        payload = json.loads(out.stdout or "{}")
        content = (
            payload.get("choices", [{}])[0].get("message", {}).get("content")
            or payload.get("output_text")
            or payload.get("reply")
        )
        if content:
            return True, str(content)
        return False, "пустой ответ агента: " + (out.stdout or out.stderr or "")[:200]
    except Exception as exc:  # pragma: no cover - network
        return False, f"api send failed: {exc}"


# --- model-facing tools -------------------------------------------------------
def tool_routes(args: Dict[str, Any], **kwargs: Any) -> str:
    data = _routes()
    bots = {
        key: {
            "title": cfg.get("title") or key,
            "bot_username": cfg.get("bot_username"),
            "transport": cfg.get("transport") or "telegram",
            "thread_id": cfg.get("thread_id"),
            "purpose": cfg.get("purpose") or "",
        }
        for key, cfg in (data.get("bots") or {}).items()
        if isinstance(cfg, dict) and cfg.get("enabled", True)
    }
    return json.dumps({"routes": bots, "count": len(bots),
                       "note": "Только эти адресаты доступны для relay."}, ensure_ascii=False)


def tool_send(args: Dict[str, Any], **kwargs: Any) -> str:
    """Park the relay as a TASK and ask the OWNER with an inline-button card.

    Nothing is sent here: the click moves submitted -> working -> completed
    (or rejected), and the callback handler performs the delivery.
    """
    target = str(args.get("target") or "").strip().lower()
    text = str(args.get("text") or "").strip()
    data = _routes()

    hub = _hub_cfg()
    if hub:
        # A2A-форма: JSON-RPC 2.0 message/send, транспорт — Telegram (ЛС с хабом).
        # Маршруты и лимиты проверяет хаб: у него реестр всех ботов и владельцев.
        if not text:
            return json.dumps({"error": "пустой текст"}, ensure_ascii=False)
        if len(text) > MAX_TEXT:
            return json.dumps({"error": f"слишком длинный текст ({len(text)} симв.)"},
                              ensure_ascii=False)
        reason = scan_text(text)
        if reason:
            _audit("refused", target=target, reason=reason, digest=_digest(text), length=len(text))
            return json.dumps({"error": "передача отклонена", "reason": reason}, ensure_ascii=False)
        rpc_id = "r-" + secrets.token_hex(4)
        payload = {
            "jsonrpc": "2.0", "id": rpc_id, "method": "message/send",
            "params": {
                "message": {"role": "user", "messageId": "m-" + secrets.token_hex(6),
                            "parts": [{"kind": "text", "text": text}]},
                "to": target,
                "metadata": {"hops": 1, "transport": "telegram"},
            },
        }
        ok = _to_hub(json.dumps(payload, ensure_ascii=False))
        _audit("rpc_message_send", target=target, rpc=rpc_id, ok=ok,
               digest=_digest(text), length=len(text))
        if not ok:
            return json.dumps({"error": "хаб недоступен"}, ensure_ascii=False)
        return json.dumps({"status": "submitted_to_hub", "rpc_id": rpc_id,
                           "target": target}, ensure_ascii=False)

    route = (data.get("bots") or {}).get(target)
    if len(text) > int((data.get("limits") or {}).get("max_text", MAX_TEXT)):
        return json.dumps({"error": f"слишком длинный текст ({len(text)} симв.)"}, ensure_ascii=False)
    reason = scan_text(text)
    if reason:
        _audit("refused", target=target, reason=reason, digest=_digest(text), length=len(text))
        return json.dumps({"error": "передача отклонена", "reason": reason}, ensure_ascii=False)


    store = _store()
    with store.mutate() as state:
        limit = int((data.get("limits") or {}).get("per_hour", PER_HOUR))
        ok, used = _rate_ok(state, limit)
        used += len([t for t in (state.get("tasks") or {}).values()
                     if t.get("direction") == "out" and t.get("status") == "submitted"])
        if not ok or used >= limit:
            _audit("rate_limited", target=target, used=used)
            return json.dumps({"error": f"лимит {limit}/час исчерпан (в очереди и отправлено: {used})"},
                              ensure_ascii=False)
        task = task_state.TaskStore.create(
            state, direction="out", peer=target, text=text,
            chat_id=route.get("chat_id") or data.get("chat_id"),
            thread_id=route.get("thread_id"))
        task_id, context_id = task["task_id"], task["context_id"]

    title = route.get("title") or target
    preview = text.replace("\n", " ")[:300]
    mid = _send_card(f"Переслать боту {title}?\n\n{preview}\n\n· задача {task_id} · ctx {context_id}",
                     [("Отправить", f"gwout:ok:{task_id}"),
                      ("Отклонить", f"gwout:no:{task_id}")])
    _audit("card_sent", task=task_id, ctx=context_id, target=target, card=mid,
           digest=_digest(text), length=len(text))
    if not mid:
        return json.dumps({"error": "не удалось отправить карточку владельцу"}, ensure_ascii=False)
    return json.dumps({"status": "pending_owner_approval", "task_id": task_id,
                       "context_id": context_id, "card_message_id": mid,
                       "expires_in_s": task_state.CARD_TTL_S}, ensure_ascii=False)


def _tg_api(method: str, **params: Any) -> Tuple[bool, Any]:
    """Call the Bot API directly (no PTB objects, no model input in the URL)."""
    token = _secret("TELEGRAM_BOT_TOKEN")
    if not token:
        return False, "TELEGRAM_BOT_TOKEN не задан"
    cmd = ["curl", "-sS", "--max-time", "30", "https://api.telegram.org/bot" + token + "/" + method]
    proxy = _secret("TELEGRAM_PROXY")
    if proxy:
        cmd += ["--proxy", proxy]
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, bool):
            value = "true" if value else "false"
        elif isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False)
        cmd += ["--data-urlencode", f"{key}={value}"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
        payload = json.loads(out.stdout or "{}")
        if payload.get("ok"):
            return True, payload.get("result")
        return False, str(payload.get("description") or out.stderr or "")[:300]
    except Exception as exc:  # pragma: no cover - network
        return False, f"telegram api failed: {exc}"


def _send_card_ex(text: str, buttons: list):
    """(chat_id, message_id) карточки; ("", "") если не отправилась."""
    return _send_card(text, buttons, _with_coords=True)


def _send_card(text: str, buttons: list, _with_coords: bool = False):
    """Post an approval card: the approvals topic of the relay group, else the owner DM.

    Telegram has no ``message_thread_id`` for the General topic (the API answers
    "message thread not found"), so 0/1/"general" means "post to the group itself".
    """
    data = _routes()
    # approvals_chat = DM с владельцем (опция 0.2); иначе — топик группы
    dm_chat = data.get("approvals_chat")
    chat = dm_chat or data.get("chat_id")
    thread = None if dm_chat else data.get("approvals_thread")
    if str(thread).strip().lower() in ("0", "1", "general", "none", ""):
        thread = None
    keyboard = {"inline_keyboard": [[
        {"text": label, "callback_data": payload} for label, payload in buttons]]}
    ok, res = _tg_api("sendMessage", chat_id=chat, message_thread_id=thread,
                      text=_CARD_MARK + text, reply_markup=keyboard,
                      disable_web_page_preview=True)
    if ok and isinstance(res, dict):
        mid = str(res.get("message_id") or "")
        return (str(chat), mid) if _with_coords else mid
    for owner in (data.get("owner_ids") or []):
        ok, res = _tg_api("sendMessage", chat_id=owner, text=_CARD_MARK + text,
                          reply_markup=keyboard, disable_web_page_preview=True)
        if ok and isinstance(res, dict):
            mid = str(res.get("message_id") or "")
            return (str(owner), mid) if _with_coords else mid
    return ("", "") if _with_coords else ""


def _finish_card(card_chat: str, card_msg: str, status: str) -> None:
    if not card_msg:
        return
    _tg_api("editMessageText", chat_id=card_chat, message_id=card_msg,
            text=_CARD_MARK + status, reply_markup={"inline_keyboard": []})


def _prune(state: Dict[str, Any]) -> None:
    now = time.time()
    for bucket in ("pending_out", "pending_in", "approved_in"):
        items = state.get(bucket) or {}
        state[bucket] = {k: v for k, v in items.items()
                         if now - float(v.get("created", 0)) < _CARD_TTL_S}


def resolve_card_click(payload: str, uid: str, cbq_id: str, card_chat: str, card_msg: str) -> str:
    """Owner's button click. Runs outside the model: the card IS the approval."""
    data = _routes()
    owners = {str(x) for x in (data.get("owner_ids") or [])}
    if uid not in owners:
        _tg_api("answerCallbackQuery", callback_query_id=cbq_id, text="Нет прав")
        _audit("card_unauthorized", user_id=uid, digest=_digest(payload))
        return "denied"
    try:
        kind, verdict, task_id = payload.split(":")
    except ValueError:
        _tg_api("answerCallbackQuery", callback_query_id=cbq_id, text="Непонятная карточка")
        return "bad"
    _tg_api("answerCallbackQuery", callback_query_id=cbq_id, text="Принято")

    store = _store()
    status = ""
    if kind == "gwout":
        task = None
        with store.mutate() as state:
            task = task_state.TaskStore.get(state, task_id)
            if task is None or task_state.TaskStore.is_terminal(task):
                status = "\u231b\ufe0f Задача неактивна — запросите заново."
            elif verdict != "ok":
                task_state.TaskStore.transition(state, task_id, "rejected")
                status = "\u274c Отклонено."
                _audit("card_rejected", task=task_id, peer=task.get("peer"))
            else:
                task_state.TaskStore.transition(state, task_id, "working")
        if not status:
            route = (data.get("bots") or {}).get(task.get("peer") or "") or {}
            last = (_state().get("last_bot_msg") or {}).get(str(route.get("bot_id") or ""), {})
            mention = f"@{route['bot_username']} " if route.get("bot_username") else ""
            ok, info = _tg_send(task.get("chat_id") or data.get("chat_id"), task.get("thread_id"),
                                f"[gw ctx:{task.get('context_id')} #1] " + mention + str(task.get("text") or ""),
                                last.get("message_id"))
            with store.mutate() as state:
                task_state.TaskStore.transition(state, task_id, "completed" if ok else "failed",
                                                last_error=None if ok else str(info)[:200])
                if ok:
                    state.setdefault("sent", []).append(time.time())
            _audit("card_sent", task=task_id, peer=task.get("peer"), ok=bool(ok), message_id=str(info))
            status = (f"\u2705 Отправлено → {task.get('peer')} (msg {info})" if ok
                      else f"\u274c Не доставлено: {info}")
    elif kind == "gwin":
        with store.mutate() as state:
            task = task_state.TaskStore.get(state, task_id)
            if task is None or task_state.TaskStore.is_terminal(task):
                status = "\u231b\ufe0f Задача неактивна."
            elif verdict != "ok":
                task_state.TaskStore.transition(state, task_id, "rejected")
                status = "\u274c Приём отклонён."
                _audit("inbound_rejected", task=task_id, peer=task.get("peer"))
            else:
                task_state.TaskStore.transition(state, task_id, "working")
                status = f"\u2705 Принято от @{task.get('peer')} — передаю в работу."
                _audit("inbound_approved", task=task_id, peer=task.get("peer"))
    elif kind == "gwcancel":
        cancelled = False
        task = None
        with store.mutate() as state:
            task = task_state.TaskStore.get(state, task_id)
            if task is None or task_state.TaskStore.is_terminal(task):
                status = "\u231b\ufe0f Задача уже завершена."
            elif verdict != "ok":
                status = "\u21a9\ufe0f Отмена не нужна, задача остаётся."
            else:
                task_state.TaskStore.transition(state, task_id, "cancelled")
                cancelled = True
        if cancelled:
            route = (data.get("bots") or {}).get(task.get("peer") or "") or {}
            mention = f"@{route['bot_username']} " if route.get("bot_username") else ""
            _tg_send(task.get("chat_id") or data.get("chat_id"), task.get("thread_id"),
                     f"[gw ctx:{task.get('context_id')} #cancel] {mention}"
                     f"\u21a9\ufe0f задача {task.get('peer')} отменена инициатором.", None)
            status = f"\u21a9\ufe0f Задача {task_id} отменена; уведомление отправлено."
            _audit("task_cancelled", task=task_id, peer=task.get("peer"))
    else:
        status = "\u2753 Неизвестная карточка."

    _finish_card(card_chat, card_msg, status)
    return status


def _extract_reply(stdout: str) -> str:
    """Pull the answer out of a one-shot CLI turn (ANSI and the banner stripped)."""
    clean = re.sub(r"\x1b\[[0-9;]*m", "", stdout or "").replace("\r", "")
    lines = clean.splitlines()
    start = None
    for i, line in enumerate(lines):
        if "\u2624" in line:
            start = i + 1
            break
    if start is None:
        return ""
    out = []
    for line in lines[start:]:
        if line.strip() and set(line.strip()) <= set("\u2500- "):
            break
        out.append(line)
    return "\n".join(out).strip()


def _run_profile_turn(body: str) -> str:
    """Run one quiet turn of THIS profile and return its reply text."""
    try:
        out = subprocess.run(["hermes", "-p", _home().name, "chat", "-q", body],
                             capture_output=True, text=True, timeout=300)
    except Exception as exc:
        _audit("profile_turn_error", error=str(exc)[:200])
        return ""
    reply = _extract_reply(out.stdout or "")
    _audit("profile_turn", rc=out.returncode, reply_len=len(reply))
    return reply


def _ask_own_default(question: str) -> str:
    """Спросить собственный default-профиль (тихий ход CLI). '' если ответа нет."""
    try:
        res = json.loads(tool_ask_default({"question": question}))
    except Exception as exc:  # noqa: BLE001
        _audit("ask_default_error", error=str(exc)[:200])
        return ""
    if res.get("ok"):
        return str(res.get("answer") or "").strip()
    _audit("ask_default_failed", error=str(res.get("error") or "")[:200])
    return ""


def _a2a_update(item: Dict[str, Any], verdict: str, *, text: str = "") -> bool:
    """A2A-апдейт задачи в хаб: status.state + message/artifact."""
    state = {"done": "completed", "ask": "input-required", "skip": "completed"}[verdict]
    params: Dict[str, Any] = {
        "task": item.get("hub_task") or "",
        "contextId": item.get("context_id") or "",
        "status": {"state": state, "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
    }
    if verdict == "ask":
        params["status"]["message"] = {"role": "agent", "parts": [{"kind": "text", "text": text}]}
    elif verdict == "done":
        params["artifact"] = {"artifactId": (item.get("hub_task") or "a") + "-out",
                              "parts": [{"kind": "text", "text": text}]}
    else:
        params["metadata"] = {"skipped": True}
    payload = {"jsonrpc": "2.0", "id": "u-" + secrets.token_hex(4),
               "method": "tasks/update", "params": params}
    _audit("rpc_tasks_update", task=params["task"], verdict=verdict, length=len(text))
    return _to_hub(json.dumps(payload, ensure_ascii=False))


def drain_approved_in(ctx: Any = None) -> int:
    """Approved inbound tasks -> one quiet turn of this profile -> DONE / ASK / SKIP."""
    store = _store()
    with store.mutate() as state:
        store.expire(state)
        working = [dict(t) for t in (state.get("tasks") or {}).values()
                   if t.get("direction") == "in" and t.get("status") == "working"]
    done = 0
    for item in working:
        task_id = item["task_id"]
        text = str(item.get("text") or "")
        peer = str(item.get("peer") or "")
        hop = int(item.get("hops") or 1)
        body = ("[СЛУЖЕБНЫЙ РЕЖИМ gateway: интерактивного канала нет, clarify НЕДОСТУПЕН. "
                "Сообщение уже принято владельцем через карточку. Если нужен факт из системы, "
                "которого у тебя нет, — вызови инструмент ask_default(<вопрос>) у своего "
                "default-профиля (он умеет ходить в систему) и используй его ответ. "
                "Финальный ответ — РОВНО одной строкой: `DONE: <ответ пиру>`; "
                "`ASK: <вопрос>` только если и ask_default не помог; "
                "`SKIP` если в сообщении нет вопроса или просьбы.] "
                f"[gw ctx:{item.get('context_id')} #{hop}] {text}")
        if item.get("answer"):
            body += f"\n[ответ владельца на вопрос «{item.get('question') or ''}»] {item['answer']}"
        reply = _run_profile_turn(body)
        if not reply:
            _audit("task_turn_failed", task=task_id)
            continue
        upper = reply.strip().upper()
        if upper.startswith("SKIP"):
            with store.mutate() as state:
                task_state.TaskStore.transition(state, task_id, "skipped")
            if _hub_cfg():
                _a2a_update(item, "skip")
            _audit("inbound_skipped", task=task_id, hop=hop)
            continue
        if upper.startswith("ASK:"):
            question = reply.strip()[4:].strip()[:1500]
            # Своя сторона отвечает сама: спрашиваем свой default-профиль, прежде чем
            # тревожить владельца карточкой.
            local = _ask_own_default(question)
            if local:
                second = (body + f"\n[ответ твоего default-профиля на вопрос «{question}»] {local}"
                                 "\nТеперь финальный ответ одной строкой: DONE: <ответ пиру> или SKIP.")
                reply = _run_profile_turn(second) or ""
                upper = reply.strip().upper()
            if not reply.strip() or upper.startswith("ASK:"):
                if upper.startswith("ASK:"):
                    question = reply.strip()[4:].strip()[:1500] or question
                card_chat, card_msg = _send_card_ex(
                    f"Нужен ответ пиру @{peer}.\n\n{question}\n\n"
                    f"Ответьте реплаем на это сообщение.\n\n· задача {task_id}",
                    [("Жду ответа", f"gwin:no:{task_id}")])
                with store.mutate() as state:
                    task_state.TaskStore.transition(state, task_id, "input-required",
                                                    question=question, card_chat=card_chat,
                                                    card_msg=card_msg)
                if _hub_cfg():
                    _a2a_update(item, "ask", text=question)
                _audit("task_input_required", task=task_id, peer=peer)
                continue
        payload = reply.strip()
        if payload.upper().startswith("DONE:"):
            payload = payload[5:].strip()
        if _hub_cfg():
            ok = _a2a_update(item, "done", text=payload)
            info = "hub"
        else:
            mention = f"@{peer} " if peer else ""
            ok, info = _tg_send(item.get("chat_id"), item.get("thread_id"),
                                f"[gw ctx:{item.get('context_id')} #{hop + 1}] {mention}{payload}",
                                item.get("message_id"))
        with store.mutate() as state:
            task_state.TaskStore.transition(state, task_id, "completed" if ok else "failed",
                                            last_error=None if ok else str(info)[:200])
            if not ok:
                # Доставленные ответы в инбокс не дублируем — они уже в топике.
                # В инбокс попадает только то, что владелец ещё не видел: недоставленное.
                state.setdefault("inbox", []).append({
                    "task": task_id, "peer": peer, "text": text[:2000], "reply": payload[:2000],
                    "thread_id": item.get("thread_id"), "ts": time.time(), "read": False,
                    "undelivered": True})
                state["inbox"] = state["inbox"][-30:]
        _audit("inbound_answered" if ok else "inbound_answer_failed", task=task_id, peer=peer,
               ok=bool(ok), message_id=str(info))
        done += 1 if ok else 0
    return done


def start_watcher(ctx: Any) -> None:
    """Background pump: approved inbound cards -> this profile's session (own chat only)."""
    if _WATCH.get("started"):
        return
    _WATCH["started"] = True

    def _loop() -> None:
        while True:
            time.sleep(3)
            try:
                drain_approved_in()
            except Exception:
                pass

    threading.Thread(target=_loop, name="gw-inbound-watcher", daemon=True).start()


def tool_ask_default(args: Dict[str, Any], **kwargs: Any) -> str:
    """Ask this agent's own default profile a question (quiet one-shot CLI turn)."""
    question = str(args.get("question") or "").strip()
    if not question:
        return json.dumps({"error": "пустой вопрос"}, ensure_ascii=False)
    if len(question) > 2000:
        return json.dumps({"error": "слишком длинный вопрос"}, ensure_ascii=False)
    reason = scan_text(question)
    if reason:
        return json.dumps({"error": "отклонено фильтром", "reason": reason}, ensure_ascii=False)
    prompt = "[Внешний запрос через gateway от другого агента]\n" + question
    try:
        out = subprocess.run(["hermes", "-p", "default", "chat", "-q", prompt],
                             capture_output=True, text=True, timeout=240)
    except Exception as exc:
        return json.dumps({"error": f"default-профиль не ответил: {exc}"[:300]}, ensure_ascii=False)
    answer = (out.stdout or "").strip()
    _audit("ask_default", question_len=len(question), answer_len=len(answer), rc=out.returncode)
    if not answer:
        return json.dumps({"error": "пустой ответ", "stderr": (out.stderr or "")[:300]},
                          ensure_ascii=False)
    return json.dumps({"ok": True, "answer": answer[:6000]}, ensure_ascii=False)


def tool_inbox(args: Dict[str, Any], **kwargs: Any) -> str:
    """Owner-approved inbound messages, for the default profile to pick up."""
    with _LOCK:
        st = _state()
        items = list(st.get("inbox") or [])
        if args.get("ack") and items:
            st["inbox"] = []
            _write_json(_state_path(), st)
    return json.dumps({"count": len(items), "items": items[-10:]}, ensure_ascii=False)


_UI_NOISE = ('Reply "skip" to skip this question',)


def tool_cancel(args: Dict[str, Any], **kwargs: Any) -> str:
    """Ask the owner to cancel an already submitted task (initiator only)."""
    task_id = str(args.get("task_id") or "").strip()
    store = _store()
    with store.mutate() as state:
        task = task_state.TaskStore.get(state, task_id)
        if task is None:
            return json.dumps({"error": "задача не найдена"}, ensure_ascii=False)
        if task.get("direction") != "out":
            return json.dumps({"error": "отменять можно только свои задачи"}, ensure_ascii=False)
        if task_state.TaskStore.is_terminal(task):
            return json.dumps({"error": f"задача уже в статусе {task.get('status')}"},
                              ensure_ascii=False)
        peer, text = task.get("peer"), str(task.get("text") or "")[:200]
    mid = _send_card(f"Отменить задачу {task_id} для {peer}?\n\n{text}",
                     [("Отменить", f"gwcancel:ok:{task_id}"),
                      ("Оставить", f"gwcancel:no:{task_id}")])
    _audit("cancel_card_sent", task=task_id, peer=peer, card=mid)
    if not mid:
        return json.dumps({"error": "не удалось отправить карточку"}, ensure_ascii=False)
    return json.dumps({"status": "cancel_pending", "task_id": task_id}, ensure_ascii=False)


def tool_protocol(args: Dict[str, Any], **kwargs: Any) -> str:
    """Протокол в формате для агента (хаб — единственный источник истины)."""
    if _hub_cfg():
        res = _hub_rpc("agent/protocol", {})
        if "protocol" in res:
            return str(res["protocol"])
        return json.dumps(res, ensure_ascii=False)
    return ("Профиль не в endpoint-режиме: реле ведёт карточки в супергруппе, "
            "см. README плагина.")


def tool_status(args: Dict[str, Any], **kwargs: Any) -> str:
    """Статус задачи у хаба (A2A tasks/get)."""
    task_id = str(args.get("task_id") or "").strip()
    if not task_id:
        return json.dumps({"error": "нужен task_id"}, ensure_ascii=False)
    res = _hub_rpc("tasks/get", {"id": task_id})
    _audit("rpc_tasks_get", task=task_id, ok="error" not in res)
    return json.dumps(res, ensure_ascii=False)


def _is_bot_ui_noise(text: str) -> bool:
    """True for an actual clarify/approval card echoed into the relay group.

    A card starts with the question mark; ordinary prose that merely *mentions* the
    words from a card must not be filtered (that used to drop real answers).
    """
    stripped = (text or "").strip()
    if stripped.startswith("\u2753"):
        return True
    return any(marker in stripped for marker in _UI_NOISE)


def _clarify_pending() -> bool:
    """True while this gateway is blocked on an owner clarify answer."""
    try:
        from tools import clarify_gateway as _cg
        return bool(getattr(_cg, "_entries", None))
    except Exception:
        return False


# --- gateway hook -------------------------------------------------------------


def _reply(chat_id: Any, thread_id: Any, text: str) -> None:
    _tg_send(chat_id, thread_id, text)


async def pre_gateway_dispatch(event, gateway=None, session_store=None, **kwargs: Any):
    """Owner-only control plane. Approval never passes through the model."""
    try:
        src = getattr(event, "source", None)
        if src is None:
            return None
        platform = str(getattr(src, "platform", "") or "")
        if platform.lower() not in ("telegram", "platform.telegram"):
            return None
        text = (event.text or "").strip()
        uid = str(getattr(src, "user_id", "") or "")
        chat_id = str(getattr(src, "chat_id", "") or "")
        thread_id = getattr(src, "thread_id", None)
        data = _routes()
        owner_ids = {str(x) for x in (data.get("owner_ids") or [])}

        # Mention rule: an unaddressed message is not ours. Hermes' require_mention is
        # resolved from the LAUNCH profile, not the routed one, so the gate lives here,
        # where it is per-chat and per-profile.
        _self = data.get("self") or {}
        _self_name = str(_self.get("bot_username") or "").lstrip("@")
        _raw = getattr(event, "raw_message", None)
        _raw_text = str(getattr(_raw, "text", None) or getattr(_raw, "caption", None) or "")
        _mentions_self = bool(_self_name) and ("@" + _self_name).lower() in (text + " " + _raw_text).lower()
        _in_group = str(chat_id) == str(data.get("chat_id"))
        _raw_msg = getattr(event, "raw_message", None)
        _reply_id = getattr(getattr(_raw_msg, "reply_to_message", None), "message_id", None)
        if (not getattr(src, "is_bot", False) and _in_group and text
                and not _mentions_self and not _clarify_pending()
                and not _reply_id
                and not text.startswith("/")):
            _audit("no_mention", user_id=uid, thread_id=thread_id,
                   digest=_digest(text), length=len(text))
            return {"action": "skip", "reason": "relay:no-mention"}

        hub = _hub_cfg()
        if hub and str(uid) == hub["bot_id"]:
            # Endpoint-режим: сообщения принимаем ТОЛЬКО от хаба, карточку не рисуем —
            # подтверждение уже получено владельцем на стороне хаба.
            try:
                env = json.loads(text)
            except ValueError:
                _audit("hub_bad_json", length=len(text))
                return {"action": "skip", "reason": "relay:hub-bad-json"}
            if "jsonrpc" in env and ("result" in env or "error" in env):
                store = _store()
                with store.mutate() as state:
                    rpc_id = str(env.get("id") or "")
                    bucket = state.setdefault("rpc", {})
                    bucket[rpc_id] = env
                    for stale in list(bucket)[:-20]:
                        bucket.pop(stale, None)
                _audit("hub_rpc_result", rpc=env.get("id"),
                       ok="result" in env, length=len(text))
                return {"action": "skip", "reason": "relay:hub-rpc"}

            method = str(env.get("method") or "")
            params = env.get("params") or {}
            if method == "tasks/cancel":
                hub_task = str(params.get("id") or (params.get("metadata") or {}).get("task") or "")
                store = _store()
                with store.mutate() as state:
                    for task in (state.get("tasks") or {}).values():
                        if task.get("hub_task") == hub_task and not task_state.TaskStore.is_terminal(task):
                            task_state.TaskStore.transition(state, task["task_id"], "cancelled")
                _audit("hub_cancel", hub_task=hub_task)
                return {"action": "skip", "reason": "relay:hub-cancel"}

            if method == "message/send":
                kind = "inbound"
                meta = params.get("metadata") or {}
                _parts = ((params.get("message") or {}).get("parts") or [])
                _chunks, _files = [], []
                for _p in _parts:
                    _kind = str(_p.get("kind") or "text")
                    if _kind == "text":
                        _chunks.append(str(_p.get("text") or ""))
                    elif _kind == "data":
                        _chunks.append(json.dumps(_p.get("data") or {}, ensure_ascii=False))
                    elif _kind == "file":
                        _info = _p.get("file") or {}
                        _files.append(str(_info.get("name") or _info.get("uri") or "file"))
                        _chunks.append(f"[файл: {_files[-1]}]")
                body = "\n".join(x.strip() for x in _chunks if x.strip()).strip()
                env = {"t": "inbound", "text": body,
                       "task": meta.get("task"), "ctx": meta.get("from_ctx") or
                       (params.get("message") or {}).get("contextId"),
                       "hops": meta.get("hops") or 1, "from": meta.get("from")}
            else:
                _audit("hub_unknown_method", method=method)
                return {"action": "skip", "reason": "relay:hub-unknown"}

            if kind in ("inbound", "answer"):
                body = str(env.get("text") or "").strip()
                store = _store()
                with store.mutate() as state:
                    store.expire(state)
                    task = task_state.TaskStore.create(
                        state, direction="in", peer=str(env.get("from") or "hub"), text=body,
                        chat_id=hub["bot_id"], thread_id=None,
                        message_id=None, context_id=str(env.get("ctx") or ""),
                        hops=int(env.get("hops") or 1))
                    task["hub_task"] = str(env.get("task") or "")
                    if kind == "answer":
                        task["answer"] = body
                    task_state.TaskStore.transition(state, task["task_id"], "working")
                    task_id = task["task_id"]
                _audit("hub_inbound", task=task_id, ctx=env.get("ctx"), hop=env.get("hops"),
                       kind=kind, length=len(body))
                return {"action": "skip", "reason": "relay:hub-inbound"}
            _audit("hub_unknown_kind", kind=kind)
            return {"action": "skip", "reason": "relay:hub-unknown"}

        if getattr(src, "is_bot", False):
            if hub:  # endpoint-режим: чужие боты не принимаются вовсе
                _audit("bot_not_allowed", user_id=uid, mode="endpoint")
                return {"action": "skip", "reason": "relay:bot-not-allowed"}
            # Bot-authored inbound: only from this group, only from bots in the registry,
            # and only with an explicit @mention. It becomes a TASK and never reaches the
            # model before the owner approves the card.
            if str(chat_id) != str(data.get("chat_id")):
                _audit("bot_outside_group", user_id=uid, chat_id=chat_id)
                return {"action": "skip", "reason": "relay:bot-outside-group"}
            peers = {str(cfg.get("bot_id")): cfg
                     for cfg in (data.get("bots") or {}).values()
                     if isinstance(cfg, dict) and cfg.get("bot_id")}
            peer = peers.get(uid)
            if peer is None:
                _audit("bot_not_allowed", user_id=uid, chat_id=chat_id,
                       digest=_digest(text), length=len(text))
                for owner in (data.get("owner_ids") or []):
                    _tg_send(owner, None,
                             "⛔ Группа agent-gateway: пишет бот вне реестра "
                             f"(id {uid}). Сообщение отброшено.")
                return {"action": "skip", "reason": "relay:bot-not-allowed"}
            if _is_bot_ui_noise(text) or text.startswith(_CARD_MARK):
                _audit("bot_ui_noise", user_id=uid, thread_id=thread_id,
                       digest=_digest(text), length=len(text))
                return {"action": "skip", "reason": "relay:bot-ui-noise"}
            if "#cancel]" in text:
                # Служебное уведомление об отмене: не задача, карточку владельцу не рисуем.
                _audit("cancel_notice", user_id=uid, thread_id=thread_id,
                       digest=_digest(text), length=len(text))
                return {"action": "skip", "reason": "relay:cancel-notice"}
            if _self_name and not _mentions_self:
                _audit("bot_no_mention", user_id=uid, thread_id=thread_id,
                       digest=_digest(text), length=len(text))
                return {"action": "skip", "reason": "relay:bot-no-mention"}
            _ctx_match = re.search(r"\[gw ctx:(\w+)(?: #(\d+))?\]", text)
            _hop = int(_ctx_match.group(2)) if (_ctx_match and _ctx_match.group(2)) else 1
            if _hop > 3:
                _audit("hop_limit", user_id=uid, thread_id=thread_id, hop=_hop)
                return {"action": "skip", "reason": "relay:hop-limit"}
            store = _store()
            with store.mutate() as state:
                store.expire(state)
                state.setdefault("last_bot_msg", {})[uid] = {
                    "message_id": getattr(event, "message_id", None),
                    "chat_id": chat_id, "thread_id": thread_id, "ts": time.time()}
                task = task_state.TaskStore.create(
                    state, direction="in", peer=peer.get("bot_username") or uid, text=text,
                    chat_id=chat_id, thread_id=thread_id,
                    message_id=getattr(event, "message_id", None),
                    context_id=(_ctx_match.group(1) if _ctx_match else None), hops=_hop)
                task_id, context_id = task["task_id"], task["context_id"]
            _audit("bot_inbound_parked", user_id=uid, thread_id=thread_id, task=task_id,
                   ctx=context_id, hop=_hop, digest=_digest(text), length=len(text))
            name = str(peer.get("bot_username") or peer.get("title") or uid)
            preview = text.replace("\n", " ")[:300]
            card_chat, card_msg = _send_card_ex(
                f"Принять сообщение от @{name}?\n\n{preview}\n\n· задача {task_id} · ctx {context_id}",
                [("Принять", f"gwin:ok:{task_id}"), ("Отклонить", f"gwin:no:{task_id}")])
            if card_msg:
                with store.mutate() as state:
                    got = task_state.TaskStore.get(state, task_id)
                    if got is not None:
                        got["card_chat"], got["card_msg"] = card_chat, card_msg
            return {"action": "skip", "reason": "relay:inbound-parked"}

        if uid not in owner_ids:
            # Unauthorized sender: let core auth/pairing handle it, but log the attempt
            # when it looks like an approval (probing / privilege escalation attempt).
            if text.lstrip("/").upper().startswith("/"):
                _audit("unauthorized_control_attempt", user_id=uid, chat_id=chat_id,
                       text_digest=_digest(text))
            return None

        # Owner answers an ASK card by replying to it in the approvals chat.
        if uid in owner_ids and text:
            replied = _reply_id
            if replied:
                store = _store()
                hit_task = None
                with store.mutate() as state:
                    for task in (state.get("tasks") or {}).values():
                        if (str(task.get("card_msg") or "") == str(replied)
                                and str(task.get("card_chat") or "") == str(chat_id)
                                and task.get("status") == "input-required"):
                            task_state.TaskStore.transition(state, task["task_id"], "working",
                                                            answer=text)
                            hit_task = task["task_id"]
                            break
                if hit_task:
                    _audit("ask_answered", task=hit_task, length=len(text))
                    _reply(chat_id, thread_id, "✅ Ответ принят — передаю пиру.")
                    return {"action": "skip", "reason": "relay:ask-answered"}
    except Exception as exc:  # never break the gateway
        _audit("hook_error", error=str(exc)[:300])
    return None
