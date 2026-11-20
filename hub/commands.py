#!/usr/bin/env python3
"""Единственный источник списка команд хаба.

Отсюда берутся: подсказки Telegram (`setMyCommands`), `/help` и inline-меню.
Меняешь команду — меняешь только здесь.
"""
from __future__ import annotations

from typing import List, Tuple

# (команда без слэша, короткое описание) — порядок важен, так их увидит Telegram
COMMANDS: List[Tuple[str, str]] = [
    ("menu", "меню с кнопками"),
    ("pair", "код привязки нового бота"),
    ("bots", "подключённые боты"),
    ("describe", "что умеет бот (для agent card)"),
    ("routes", "маршруты и политика подтверждений"),
    ("route", "изменить политику маршрута"),
    ("log", "последние задачи по боту"),
    ("task", "детали задачи"),
    ("protocol", "протокол для агента"),
    ("card", "A2A-карточка хаба или бота"),
    ("cancel", "отменить задачу"),
    ("unpair", "отключить бота"),
    ("help", "справка"),
]

# inline-меню: (подпись кнопки, callback_data)
MENU_ROWS: List[List[Tuple[str, str]]] = [
    [("🤖 Боты", "hub:menu:bots"), ("🔀 Маршруты", "hub:menu:routes")],
    [("📜 Задачи", "hub:menu:log"), ("📘 Протокол", "hub:menu:protocol")],
    [("❓ Помощь", "hub:menu:help")],
]


def bot_commands() -> List[dict]:
    """Формат Bot API для setMyCommands (≤32 команд, ≤256 байт описание)."""
    return [{"command": name, "description": desc[:256]} for name, desc in COMMANDS]


def help_text() -> str:
    lines = ["TG Gateway — шлюз между вашими ботами.", ""]
    lines += [f"/{name} — {desc}" for name, desc in COMMANDS]
    return "\n".join(lines)
