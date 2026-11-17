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
from typing import Any, Dict, Optional, Tuple

CODE_TTL_S = 900           # approval window
MAX_TEXT = 3000            # characters per relayed message
PER_HOUR = 20              # relays per rolling hour
SSRF_HOST_ALLOW = re.compile(r"^https?://(127\.0\.0\.1|localhost|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)", re.I)

_LOCK = threading.RLock()

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


def _routes_path() -> Path:
    return _home() / "gateway-routes.json"


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
    data = _read_json(_state_path(), {})
    if not isinstance(data, dict):
        data = {}
    data.setdefault("pending", {})
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
    """Park the relay and ask the OWNER in the approvals topic with inline buttons.

    Nothing is sent from here: the button click is the approval and the callback handler
    performs the delivery, so neither this model nor a subagent can self-approve.
    """
    target = str(args.get("target") or "").strip().lower()
    text = str(args.get("text") or "").strip()
    data = _routes()
    route = (data.get("bots") or {}).get(target)
    if not isinstance(route, dict) or route.get("enabled") is False:
        return json.dumps({"error": f"маршрут '{target}' не зарегистрирован",
                           "known": sorted((data.get("bots") or {}).keys())}, ensure_ascii=False)
    if not text:
        return json.dumps({"error": "пустой текст"}, ensure_ascii=False)
    if len(text) > int((data.get("limits") or {}).get("max_text", MAX_TEXT)):
        return json.dumps({"error": f"слишком длинный текст ({len(text)} симв.)"}, ensure_ascii=False)
    reason = scan_text(text)
    if reason:
        _audit("refused", target=target, reason=reason, digest=_digest(text), length=len(text))
        return json.dumps({"error": "передача отклонена", "reason": reason}, ensure_ascii=False)

    with _LOCK:
        st = _state()
        _prune(st)
        limit = int((data.get("limits") or {}).get("per_hour", PER_HOUR))
        ok, used = _rate_ok(st, limit)
        used += len(st.get("pending_out") or {})
        if not ok or used >= limit:
            _audit("rate_limited", target=target, used=used)
            return json.dumps({"error": f"лимит {limit}/час исчерпан (в очереди и отправлено: {used})"},
                              ensure_ascii=False)
        req = secrets.token_hex(3)
        st.setdefault("pending_out", {})[req] = {
            "target": target, "text": text, "created": time.time(),
            "chat_id": route.get("chat_id") or data.get("chat_id"),
            "thread_id": route.get("thread_id"),
            "bot_username": route.get("bot_username"), "bot_id": route.get("bot_id"),
        }
        _write_json(_state_path(), st)

    title = route.get("title") or target
    preview = text.replace("\n", " ")[:300]
    mid = _send_card(f"Переслать боту {title}?\n\n{preview}",
                     [("Отправить", f"gwout:ok:{req}"), ("Отклонить", f"gwout:no:{req}")])
    _audit("card_sent", req=req, target=target, card=mid, digest=_digest(text), length=len(text))
    if not mid:
        return json.dumps({"error": "не удалось отправить карточку владельцу"}, ensure_ascii=False)
    return json.dumps({"status": "pending_owner_approval", "request": req,
                       "card_message_id": mid, "expires_in_s": _CARD_TTL_S}, ensure_ascii=False)


# --- approval cards in the dedicated topic -----------------------------------
_CARD_MARK = "\U0001f6e1 gateway \u00b7 "      # every card starts with this marker
_CARD_TTL_S = 1200                             # parked card lives 20 minutes
_WATCH: dict[str, bool] = {}


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


def _send_card(text: str, buttons: list) -> str:
    """Post an approval card: the approvals topic of the relay group, else the owner DM.

    Telegram has no ``message_thread_id`` for the General topic (the API answers
    "message thread not found"), so 0/1/"general" means "post to the group itself".
    """
    data = _routes()
    chat = data.get("chat_id")
    thread = data.get("approvals_thread")
    if str(thread).strip().lower() in ("0", "1", "general", "none", ""):
        thread = None
    keyboard = {"inline_keyboard": [[
        {"text": label, "callback_data": payload} for label, payload in buttons]]}
    ok, res = _tg_api("sendMessage", chat_id=chat, message_thread_id=thread,
                      text=_CARD_MARK + text, reply_markup=keyboard,
                      disable_web_page_preview=True)
    if ok and isinstance(res, dict):
        return str(res.get("message_id") or "")
    for owner in (data.get("owner_ids") or []):
        ok, res = _tg_api("sendMessage", chat_id=owner, text=_CARD_MARK + text,
                          reply_markup=keyboard, disable_web_page_preview=True)
        if ok and isinstance(res, dict):
            return str(res.get("message_id") or "")
    return ""


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
        kind, verdict, req = payload.split(":")
    except ValueError:
        _tg_api("answerCallbackQuery", callback_query_id=cbq_id, text="Непонятная карточка")
        return "bad"
    _tg_api("answerCallbackQuery", callback_query_id=cbq_id, text="Принято")
    status = ""

    if kind == "gwout":
        with _LOCK:
            st = _state()
            _prune(st)
            item = (st.get("pending_out") or {}).pop(req, None)
            if item is not None and verdict == "ok":
                st.setdefault("sent", []).append(time.time())
            _write_json(_state_path(), st)
        if item is None:
            status = "\u231b\ufe0f Карточка устарела — запросите заново."
        elif verdict != "ok":
            status = "\u274c Отклонено."
            _audit("card_rejected", req=req, target=item.get("target"))
        else:
            route = (data.get("bots") or {}).get(item.get("target")) or {}
            st = _state()
            last = (st.get("last_bot_msg") or {}).get(str(route.get("bot_id") or ""), {})
            mention = f"@{route['bot_username']} " if route.get("bot_username") else ""
            ok, info = _tg_send(item.get("chat_id") or data.get("chat_id"), item.get("thread_id"),
                                mention + str(item.get("text") or ""), last.get("message_id"))
            _audit("card_sent", req=req, target=item.get("target"), ok=bool(ok), message_id=str(info))
            status = (f"\u2705 Отправлено → {item.get('target')} (msg {info})" if ok
                      else f"\u274c Не доставлено: {info}")
    elif kind == "gwin":
        with _LOCK:
            st = _state()
            _prune(st)
            item = (st.get("pending_in") or {}).pop(req, None)
            if item is not None and verdict == "ok":
                st.setdefault("approved_in", {})[req] = item
            _write_json(_state_path(), st)
        if item is None:
            status = "\u231b\ufe0f Карточка устарела."
        elif verdict != "ok":
            status = "\u274c Приём отклонён."
            _audit("inbound_rejected", req=req, from_bot=item.get("from_bot"))
        else:
            status = f"\u2705 Принято от @{item.get('from_bot')} — передаю в работу."
            _audit("inbound_approved", req=req, from_bot=item.get("from_bot"))
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


def drain_approved_in(ctx: Any = None) -> int:
    """Owner-approved inbound -> one quiet turn of this profile -> reply to the peer."""
    with _LOCK:
        st = _state()
        _prune(st)
        items = dict(st.get("approved_in") or {})
    done = 0
    for req, item in items.items():
        text = str(item.get("text") or "")
        peer = str(item.get("from_bot") or "")
        hop_match = re.search(r"\[gw-hop:(\d+)\]", text)
        hop = int(hop_match.group(1)) if hop_match else 1
        body = ("[СЛУЖЕБНЫЙ РЕЖИМ gateway: интерактивного канала нет, clarify НЕДОСТУПЕН. "
                "Это сообщение уже принято владельцем через карточку — подтверждать приём не нужно. "
                "Если в сообщении нет вопроса или просьбы (статус, отчёт, подтверждение, болтовня) — "
                "ответь РОВНО одним словом: SKIP. Иначе — короткий ответ по существу, без "
                f"мета-рассуждений.] [gw-hop:{hop}] {text}")
        reply = _run_profile_turn(body)
        if not reply:
            _audit("inbound_turn_failed", req=req, reason="empty reply")
            continue
        if reply.strip().upper().startswith("SKIP"):
            _audit("inbound_skipped", req=req, hop=hop)
            with _LOCK:
                st = _state()
                st.get("approved_in", {}).pop(req, None)
                _write_json(_state_path(), st)
            continue
        _low = reply.lower()
        if any(m in _low for m in ("карточка не доставлена", "undelivered", "подтверждения нет")):
            _audit("inbound_meta_dropped", req=req, hop=hop)
            with _LOCK:
                st = _state()
                st.get("approved_in", {}).pop(req, None)
                _write_json(_state_path(), st)
            continue
        mention = f"@{peer} " if peer else ""
        ok, info = _tg_send(item.get("chat_id"), item.get("thread_id"),
                            f"[gw-hop:{hop + 1}] {mention}{reply}", item.get("message_id"))
        _audit("inbound_answered", req=req, ok=bool(ok), message_id=str(info))
        if not ok:
            continue
        with _LOCK:
            st = _state()
            st.get("approved_in", {}).pop(req, None)
            st.setdefault("inbox", []).append({
                "req": req, "from_bot": peer, "text": text[:2000], "reply": reply[:2000],
                "thread_id": item.get("thread_id"), "ts": time.time(), "read": False})
            st["inbox"] = st["inbox"][-30:]
            _write_json(_state_path(), st)
        done += 1
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
_ROUTE_RE = re.compile(r"^/?ROUTE\s+(DEL\s+)?(\S+)(?:\s+(\d+))?(?:\s+(.+))?$", re.I)
_TOPIC_LINK_RE = re.compile(r"t\.me/(?:c/\d+|[A-Za-z0-9_]+)/(\d+)", re.I)


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
        if (not getattr(src, "is_bot", False) and _in_group and text
                and not _mentions_self and not _clarify_pending()
                and not text.startswith("/")
                and not text.lstrip("/").upper().startswith("ROUTE ")):
            _audit("no_mention", user_id=uid, thread_id=thread_id,
                   digest=_digest(text), length=len(text))
            return {"action": "skip", "reason": "relay:no-mention"}

        if getattr(src, "is_bot", False):
            # Bot-authored inbound: only from this group, only from bots in the registry,
            # and only with an explicit @mention. The text never reaches the model until the
            # OWNER approves the card in the approvals topic.
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
            if _self_name and not _mentions_self:
                _audit("bot_no_mention", user_id=uid, thread_id=thread_id,
                       digest=_digest(text), length=len(text))
                return {"action": "skip", "reason": "relay:bot-no-mention"}
            _hop_match = re.search(r"\[gw-hop:(\d+)\]", text)
            _hop = int(_hop_match.group(1)) if _hop_match else 1
            if _hop > 3:
                _audit("hop_limit", user_id=uid, thread_id=thread_id, hop=_hop)
                return {"action": "skip", "reason": "relay:hop-limit"}
            with _LOCK:
                st = _state()
                _prune(st)
                st.setdefault("last_bot_msg", {})[uid] = {
                    "message_id": getattr(event, "message_id", None),
                    "chat_id": chat_id, "thread_id": thread_id, "ts": time.time()}
                req = secrets.token_hex(3)
                st.setdefault("pending_in", {})[req] = {
                    "text": text, "chat_id": chat_id, "thread_id": thread_id,
                    "message_id": getattr(event, "message_id", None),
                    "from_bot": peer.get("bot_username") or uid, "bot_id": uid,
                    "owner_id": (data.get("owner_ids") or [None])[0],
                    "created": time.time()}
                _write_json(_state_path(), st)
            _audit("bot_inbound_parked", user_id=uid, thread_id=thread_id, req=req,
                   digest=_digest(text), length=len(text))
            name = str(peer.get("bot_username") or peer.get("title") or uid)
            preview = text.replace("\n", " ")[:300]
            _send_card(f"Принять сообщение от @{name}?\n\n{preview}",
                       [("Принять", f"gwin:ok:{req}"), ("Отклонить", f"gwin:no:{req}")])
            return {"action": "skip", "reason": "relay:inbound-parked"}

        if uid not in owner_ids:
            # Unauthorized sender: let core auth/pairing handle it, but log the attempt
            # when it looks like an approval (probing / privilege escalation attempt).
            if text.lstrip("/").upper().startswith("ROUTE "):
                _audit("unauthorized_control_attempt", user_id=uid, chat_id=chat_id,
                       text_digest=_digest(text))
            return None

        m = _ROUTE_RE.match(text)
        if m:
            delete, name, thread, title = m.group(1), m.group(2), m.group(3), m.group(4)
            if thread is None and title:
                # accept a topic link instead of a bare id: t.me/c/<chat>/<thread>
                lm = _TOPIC_LINK_RE.search(title)
                if lm:
                    thread = lm.group(1)
                    before = re.sub(r"https?://\s*$", "", title[:lm.start()]).strip()
                    title = before or None
            name = name.lower()
            with _LOCK:
                data = _routes()
                if delete:
                    data["bots"].pop(name, None)
                    _write_json(_routes_path(), data)
                    _reply(chat_id, thread_id, f"Маршрут {name} удалён.")
                    _audit("route_deleted", name=name, user_id=uid)
                elif thread:
                    data["bots"][name] = {
                        "title": title or name,
                        "thread_id": int(thread),
                        "chat_id": data.get("chat_id"),
                        "transport": "telegram",
                        "enabled": True,
                    }
                    _write_json(_routes_path(), data)
                    _reply(chat_id, thread_id, f"Маршрут {name} → топик {thread} сохранён.")
                    _audit("route_added", name=name, thread_id=thread, user_id=uid)
                else:
                    listing = "\n".join(f"• {k} → топик {v.get('thread_id')} ({v.get('title')})"
                                        for k, v in data["bots"].items()) or "пусто"
                    _reply(chat_id, thread_id, "Маршруты:\n" + listing)
            return {"action": "skip", "reason": "relay:route-admin"}
    except Exception as exc:  # never break the gateway
        _audit("hook_error", error=str(exc)[:300])
    return None
