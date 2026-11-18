#!/usr/bin/env python3
"""Правила хаба: когда спрашивать владельца, лимиты, глубина цепочки, фильтры.

Чистые функции — без сети и БД, чтобы их можно было тестировать отдельно.
"""
from __future__ import annotations

import re
from typing import Optional

MAX_HOPS = 3
MAX_TEXT = 3000
PER_HOUR = 20

# Секреты и команды не пересылаем между агентами.
SECRET_PATTERNS = (
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("token_like", re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{30,}\b")),      # telegram bot token
    ("api_key", re.compile(r"\b(sk|pk|ghp|xoxb)-[A-Za-z0-9_-]{16,}\b")),
    ("password_line", re.compile(r"(?i)\b(password|passwd|пароль)\s*[:=]\s*\S+")),
)

SHELL_PATTERNS = (
    ("shell_pipe", re.compile(r"[|;`]\s*(rm|curl|wget|nc|bash|sh)\b")),
)


def scan_text(text: str) -> Optional[str]:
    """Причина отказа или None."""
    for name, rx in SECRET_PATTERNS:
        if rx.search(text):
            return name
    for name, rx in SHELL_PATTERNS:
        if rx.search(text):
            return name
    return None


def check_limits(text: str, *, max_text: int = MAX_TEXT, used_hour: int = 0,
                 per_hour: int = PER_HOUR) -> Optional[str]:
    if not text or not text.strip():
        return "пустой текст"
    if len(text) > int(max_text):
        return f"слишком длинный текст ({len(text)} > {max_text})"
    if used_hour >= int(per_hour):
        return f"лимит {per_hour}/час исчерпан"
    reason = scan_text(text)
    if reason:
        return f"фильтр: {reason}"
    return None


def is_continuation(*, ctx: str, hops: int) -> bool:
    """Продолжение уже начатой цепочки — не требует новой карточки."""
    return bool(ctx) and int(hops) > 1


def needs_approval(*, policy: str, ctx: str, hops: int, direction: str) -> bool:
    """Спрашивать владельца?

    - новый исходящий запрос (нет ctx) → да, всегда;
    - ответ/продолжение в рамках ctx → только если маршрут помечен policy=ask;
    - policy=auto разрешает продолжения без карточки.
    """
    if not is_continuation(ctx=ctx, hops=hops):
        return True
    if direction == "reply":
        return policy != "auto"
    return policy == "ask"


def next_hops(hops: int) -> int:
    return int(hops) + 1


def hop_allowed(hops: int) -> bool:
    return int(hops) <= MAX_HOPS
