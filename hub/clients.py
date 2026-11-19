#!/usr/bin/env python3
"""Токены внешних A2A-клиентов: генерация, хеширование, права.

Plaintext токена показывается владельцу один раз и в БД не хранится — только
sha256 и короткий префикс для опознания в логах.
"""
from __future__ import annotations

import hashlib
import json
import secrets
from typing import Any, Iterable, Optional, Set

TOKEN_PREFIX = "tgw"
ALL = "*"


def new_client_id() -> str:
    return "cl_" + secrets.token_hex(3)


def new_token(client_id: str) -> str:
    return f"{TOKEN_PREFIX}_{client_id}_{secrets.token_urlsafe(24)}"


def token_hash(token: str) -> str:
    return hashlib.sha256(token.strip().encode("utf-8")).hexdigest()


def token_prefix(token: str) -> str:
    token = token.strip()
    return token[:14] + "…" if len(token) > 14 else token


def dump_bots(bots: Iterable[str] | str) -> str:
    if isinstance(bots, str):
        return bots
    return json.dumps(sorted({str(b).strip() for b in bots if str(b).strip()}), ensure_ascii=False)


def parse_bots(field: Any) -> Optional[Set[str]]:
    """None = все боты (None или '*'), иначе множество алиасов."""
    raw = str(field or ALL).strip()
    if raw == ALL or not raw:
        return None
    try:
        items = json.loads(raw)
    except ValueError:
        items = [raw]
    if isinstance(items, str):
        items = [items]
    return {str(x).strip() for x in items if str(x).strip()}


def as_client(row: Any) -> dict:
    """Строка БД → словарь клиента для проверок."""
    c = dict(row)
    c["bots_set"] = parse_bots(c.get("bots"))
    return c
