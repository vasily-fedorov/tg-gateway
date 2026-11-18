#!/usr/bin/env python3
"""Схема и доступ к SQLite хаба: пользователи, боты, маршруты, задачи, аудит.

Одна БД на инсталляцию; мультитенантность — через owner_tg в bots/routes/tasks.
"""
from __future__ import annotations

import json
import pathlib
import sqlite3
import time
from typing import Any, Dict, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS owners (
    tg_id       INTEGER PRIMARY KEY,
    name        TEXT,
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS bots (
    bot_id      INTEGER PRIMARY KEY,
    username    TEXT,
    alias       TEXT NOT NULL,
    owner_tg    INTEGER NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'agent',   -- agent | default
    enabled     INTEGER NOT NULL DEFAULT 1,
    paired_at   REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS bots_alias ON bots(owner_tg, alias);
CREATE TABLE IF NOT EXISTS pair_codes (
    code        TEXT PRIMARY KEY,
    owner_tg    INTEGER NOT NULL,
    alias       TEXT NOT NULL,
    created_at  REAL NOT NULL,
    used_at     REAL
);
CREATE TABLE IF NOT EXISTS routes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_tg    INTEGER NOT NULL,
    src_alias   TEXT NOT NULL,           -- от кого ('' = владелец)
    dst_alias   TEXT NOT NULL,
    policy      TEXT NOT NULL DEFAULT 'ask',   -- ask | auto
    max_text    INTEGER NOT NULL DEFAULT 3000,
    per_hour    INTEGER NOT NULL DEFAULT 20,
    created_at  REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS routes_pair ON routes(owner_tg, src_alias, dst_alias);
CREATE TABLE IF NOT EXISTS tasks (
    task_id     TEXT PRIMARY KEY,
    owner_tg    INTEGER NOT NULL,
    direction   TEXT NOT NULL,           -- out (владелец→бот) | in (бот→бот) | reply
    ctx         TEXT NOT NULL,
    hops        INTEGER NOT NULL DEFAULT 1,
    src_alias   TEXT,
    dst_alias   TEXT,
    text        TEXT,
    status      TEXT NOT NULL,           -- submitted | working | completed | rejected
                                         -- | failed | cancelled | input-required
    question    TEXT,
    answer      TEXT,
    card_chat   INTEGER,
    card_msg    INTEGER,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS tasks_status ON tasks(status, owner_tg);
CREATE TABLE IF NOT EXISTS invites (
    code        TEXT PRIMARY KEY,
    bot_id      INTEGER NOT NULL,
    owner_tg    INTEGER NOT NULL,
    created_at  REAL NOT NULL,
    used_at     REAL
);
CREATE TABLE IF NOT EXISTS links (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    src_bot_id  INTEGER NOT NULL,
    dst_bot_id  INTEGER NOT NULL,
    policy_src  TEXT NOT NULL DEFAULT 'ask',
    policy_dst  TEXT NOT NULL DEFAULT 'ask',
    created_at  REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS links_pair ON links(src_bot_id, dst_bot_id);
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    kind        TEXT NOT NULL,
    task_id     TEXT,
    owner_tg    INTEGER,
    detail      TEXT
);
"""

TERMINAL = frozenset({"completed", "rejected", "failed", "cancelled", "skipped"})
CARD_TTL_S = 20 * 60
TASK_TTL_S = 24 * 3600


def now() -> float:
    return time.time()


class Db:
    def __init__(self, path: pathlib.Path | str):
        self.path = pathlib.Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), timeout=10, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        cols = {r["name"] for r in self.q("PRAGMA table_info(bots)")}
        if "description" not in cols:
            self.conn.execute("ALTER TABLE bots ADD COLUMN description TEXT NOT NULL DEFAULT ''")
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS inbox_msgs (message_id TEXT PRIMARY KEY, task_id TEXT, "
            "created_at REAL NOT NULL)")

    # ── helpers ──────────────────────────────────────────────────────────────
    def q(self, sql: str, args: tuple = ()) -> List[sqlite3.Row]:
        return list(self.conn.execute(sql, args))

    def one(self, sql: str, args: tuple = ()) -> Optional[sqlite3.Row]:
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def event(self, kind: str, *, task_id: str | None = None, owner: int | None = None,
              **detail: Any) -> None:
        self.conn.execute(
            "INSERT INTO events (ts, kind, task_id, owner_tg, detail) VALUES (?,?,?,?,?)",
            (now(), kind, task_id, owner, json.dumps(detail, ensure_ascii=False)[:2000]))

    # ── owners / bots ────────────────────────────────────────────────────────
    def ensure_owner(self, tg_id: int, name: str = "") -> None:
        self.conn.execute("INSERT OR IGNORE INTO owners (tg_id, name, created_at) VALUES (?,?,?)",
                          (tg_id, name, now()))

    def owner(self, tg_id: int) -> Optional[sqlite3.Row]:
        return self.one("SELECT * FROM owners WHERE tg_id=?", (tg_id,))

    def add_pair_code(self, owner: int, alias: str, code: str) -> None:
        self.conn.execute("INSERT INTO pair_codes (code, owner_tg, alias, created_at) "
                          "VALUES (?,?,?,?)", (code.upper(), owner, alias, now()))

    def take_pair_code(self, code: str) -> Optional[sqlite3.Row]:
        row = self.one("SELECT * FROM pair_codes WHERE code=? AND used_at IS NULL", (code.upper(),))
        if row:
            self.conn.execute("UPDATE pair_codes SET used_at=? WHERE code=?", (now(), code.upper()))
        return row

    def bot_owner(self, bot_id: int) -> int | None:
        row = self.one("SELECT owner_tg FROM bots WHERE bot_id=?", (bot_id,))
        return int(row["owner_tg"]) if row else None

    def set_enabled(self, bot_id: int, enabled: bool) -> None:
        self.conn.execute("UPDATE bots SET enabled=? WHERE bot_id=?", (1 if enabled else 0, bot_id))

    def pair_bot(self, *, bot_id: int, username: str, alias: str, owner: int,
                 kind: str = "agent") -> bool:
        """False — если бот уже принадлежит другому владельцу (нужен /unpair)."""
        self.conn.execute(
            "INSERT INTO bots (bot_id, username, alias, owner_tg, kind, enabled, paired_at) "
            "VALUES (?,?,?,?,?,1,?) "
            "ON CONFLICT(bot_id) DO UPDATE SET username=excluded.username, alias=excluded.alias, "
            "owner_tg=excluded.owner_tg, kind=excluded.kind, enabled=1, paired_at=excluded.paired_at",
            (bot_id, username, alias, owner, kind, now()))
        return True

    def bot_by_id(self, bot_id: int) -> Optional[sqlite3.Row]:
        return self.one("SELECT * FROM bots WHERE bot_id=? AND enabled=1", (bot_id,))

    def bot_by_alias(self, owner: int, alias: str) -> Optional[sqlite3.Row]:
        return self.one("SELECT * FROM bots WHERE owner_tg=? AND alias=? AND enabled=1",
                        (owner, alias))

    def bots(self, owner: int) -> List[sqlite3.Row]:
        return self.q("SELECT * FROM bots WHERE owner_tg=? ORDER BY alias", (owner,))

    def seen_message(self, message_id: str) -> Optional[str]:
        row = self.one("SELECT task_id FROM inbox_msgs WHERE message_id=?", (message_id,))
        return row["task_id"] if row else None

    def remember_message(self, message_id: str, task_id: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO inbox_msgs (message_id, task_id, created_at) "
                          "VALUES (?,?,?)", (message_id, task_id, now()))

    def set_description(self, owner: int, alias: str, text: str) -> bool:
        bot = self.bot_by_alias(owner, alias)
        if not bot:
            return False
        self.conn.execute("UPDATE bots SET description=? WHERE bot_id=?",
                          (text[:300], int(bot["bot_id"])))
        return True

    # ── межвладельческие связи ───────────────────────────────────────────────
    def create_invite(self, bot_id: int, owner: int, code: str) -> None:
        self.conn.execute("INSERT INTO invites (code, bot_id, owner_tg, created_at) "
                          "VALUES (?,?,?,?)", (code.upper(), bot_id, owner, now()))

    def redeem_invite(self, code: str) -> Optional[sqlite3.Row]:
        row = self.one("SELECT * FROM invites WHERE code=? AND used_at IS NULL", (code.upper(),))
        if row:
            self.conn.execute("UPDATE invites SET used_at=? WHERE code=?", (now(), code.upper()))
        return row

    def add_link(self, src_bot_id: int, dst_bot_id: int, *, policy_src: str = "ask",
                 policy_dst: str = "ask") -> None:
        self.conn.execute(
            "INSERT INTO links (src_bot_id, dst_bot_id, policy_src, policy_dst, created_at) "
            "VALUES (?,?,?,?,?) ON CONFLICT(src_bot_id, dst_bot_id) DO UPDATE SET "
            "policy_src=excluded.policy_src, policy_dst=excluded.policy_dst",
            (src_bot_id, dst_bot_id, policy_src, policy_dst, now()))

    def link_for(self, src_bot_id: int, dst_username: str) -> Optional[sqlite3.Row]:
        return self.one(
            "SELECT l.*, b.bot_id AS dst_id, b.username AS dst_username, b.owner_tg AS dst_owner "
            "FROM links l JOIN bots b ON b.bot_id=l.dst_bot_id "
            "WHERE l.src_bot_id=? AND b.username=? AND b.enabled=1",
            (src_bot_id, dst_username.lstrip("@")))

    def link_by_id(self, link_id: int) -> Optional[sqlite3.Row]:
        return self.one(
            "SELECT l.*, b.bot_id AS dst_id, b.username AS dst_username, b.owner_tg AS dst_owner "
            "FROM links l JOIN bots b ON b.bot_id=l.dst_bot_id WHERE l.id=?", (link_id,))

    def links_for_owner(self, owner: int) -> List[sqlite3.Row]:
        return self.q(
            "SELECT l.*, s.username AS src_username, d.username AS dst_username "
            "FROM links l JOIN bots s ON s.bot_id=l.src_bot_id JOIN bots d ON d.bot_id=l.dst_bot_id "
            "WHERE s.owner_tg=? OR d.owner_tg=? ORDER BY l.id", (owner, owner))

    def drop_link(self, link_id: int) -> None:
        self.conn.execute("DELETE FROM links WHERE id=?", (link_id,))

    def bot_by_username(self, username: str) -> Optional[sqlite3.Row]:
        return self.one("SELECT * FROM bots WHERE username=? AND enabled=1",
                        (username.lstrip("@"),))

    # ── routes ───────────────────────────────────────────────────────────────
    def route(self, owner: int, src: str, dst: str) -> Optional[sqlite3.Row]:
        return self.one("SELECT * FROM routes WHERE owner_tg=? AND src_alias=? AND dst_alias=?",
                        (owner, src, dst))

    def set_route(self, owner: int, src: str, dst: str, *, policy: str = "ask",
                  max_text: int = 3000, per_hour: int = 20) -> None:
        self.conn.execute(
            "INSERT INTO routes (owner_tg, src_alias, dst_alias, policy, max_text, per_hour, "
            "created_at) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(owner_tg, src_alias, dst_alias) DO UPDATE SET policy=excluded.policy, "
            "max_text=excluded.max_text, per_hour=excluded.per_hour",
            (owner, src, dst, policy, max_text, per_hour, now()))

    def routes(self, owner: int) -> List[sqlite3.Row]:
        return self.q("SELECT * FROM routes WHERE owner_tg=? ORDER BY src_alias, dst_alias",
                      (owner,))

    # ── tasks ────────────────────────────────────────────────────────────────
    def create_task(self, *, task_id: str, owner: int, direction: str, ctx: str, hops: int,
                    src: str, dst: str, text: str, status: str = "submitted",
                    card_chat: int | None = None, card_msg: int | None = None) -> None:
        ts = now()
        self.conn.execute(
            "INSERT INTO tasks (task_id, owner_tg, direction, ctx, hops, src_alias, dst_alias, "
            "text, status, card_chat, card_msg, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (task_id, owner, direction, ctx, hops, src, dst, text, status, card_chat, card_msg,
             ts, ts))

    def update_task(self, task_id: str, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE tasks SET {cols}, updated_at=? WHERE task_id=?",
                          (*fields.values(), now(), task_id))

    def task(self, task_id: str) -> Optional[sqlite3.Row]:
        return self.one("SELECT * FROM tasks WHERE task_id=?", (task_id,))

    def open_tasks(self, owner: int) -> List[sqlite3.Row]:
        return self.q("SELECT * FROM tasks WHERE owner_tg=? AND status IN "
                      "('submitted','working','input-required')", (owner,))

    def hours_used(self, owner: int, src: str, dst: str) -> int:
        row = self.one("SELECT COUNT(*) AS n FROM tasks WHERE owner_tg=? AND src_alias=? AND "
                       "dst_alias=? AND created_at > ?", (owner, src, dst, now() - 3600))
        return int(row["n"]) if row else 0

    def expire(self, owner: int) -> List[sqlite3.Row]:
        """Протухшие карточки: submitted старше CARD_TTL_S → failed(card_expired)."""
        stale = self.q("SELECT * FROM tasks WHERE owner_tg=? AND status='submitted' "
                       "AND created_at < ?", (owner, now() - CARD_TTL_S))
        for row in stale:
            self.update_task(row["task_id"], status="failed")
            self.event("card_expired", task_id=row["task_id"], owner=owner)
        return stale
