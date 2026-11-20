#!/usr/bin/env python3
"""Схема и доступ к SQLite хаба.

Хаб — A2A-сервер: задачи живут здесь, исполнитель лишь сообщает о прогрессе.
Мультитенантность — через owner_tg в bots/routes/tasks.
"""
from __future__ import annotations

import json
import pathlib
import sqlite3
import threading
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
    paired_at   REAL NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    a2a_policy  TEXT NOT NULL DEFAULT '',        -- '' (наследовать) | ask | auto
    routes_mode TEXT NOT NULL DEFAULT 'explicit'
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
    context_mode TEXT NOT NULL DEFAULT '',  -- on | off | '' (по умолчанию on)
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
    status      TEXT NOT NULL,           -- submitted | working | input-required
                                         -- | completed | rejected | failed | cancelled
    a2a_state   TEXT NOT NULL DEFAULT '',  -- spec-состояние, как его видит клиент
    progress    TEXT NOT NULL DEFAULT '',  -- «выполняю, 3 мин» и прочие промежуточные
    question    TEXT,
    answer      TEXT,
    attempts    INTEGER NOT NULL DEFAULT 0,
    card_chat   INTEGER,
    card_msg    INTEGER,
    attachments TEXT,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    heartbeat_at REAL
);
CREATE INDEX IF NOT EXISTS tasks_status ON tasks(status, owner_tg);
CREATE INDEX IF NOT EXISTS tasks_ctx ON tasks(owner_tg, ctx, updated_at);
CREATE TABLE IF NOT EXISTS task_messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT NOT NULL,
    role        TEXT NOT NULL,           -- user | agent
    text        TEXT NOT NULL,
    ts          REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS task_messages_task ON task_messages(task_id, ts);
CREATE TABLE IF NOT EXISTS owner_state (
    owner_tg    INTEGER PRIMARY KEY,
    action      TEXT NOT NULL,
    payload     TEXT,
    created_at  REAL NOT NULL
);
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

# Внутреннее состояние → A2A TaskState. Клиент видит только spec-значения.
A2A_STATES = {
    "submitted": "TASK_STATE_AUTH_REQUIRED",   # ждёт подтверждения владельца
    "working": "TASK_STATE_WORKING",
    "input-required": "TASK_STATE_INPUT_REQUIRED",
    "completed": "TASK_STATE_COMPLETED",
    "rejected": "TASK_STATE_REJECTED",
    "failed": "TASK_STATE_FAILED",
    "cancelled": "TASK_STATE_CANCELED",
    "skipped": "TASK_STATE_COMPLETED",
}


def now() -> float:
    return time.time()


def a2a_state(status: str) -> str:
    return A2A_STATES.get(str(status or ""), "TASK_STATE_UNKNOWN")


class Db:
    def __init__(self, path: pathlib.Path | str):
        self.path = pathlib.Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.path), timeout=10, isolation_level=None,
                                    check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.run("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Существующая БД: добавить недостающие колонки, убрать снятые таблицы."""
        tcols = {r["name"] for r in self.q("PRAGMA table_info(tasks)")}
        for name, ddl in (
            ("attachments", "ALTER TABLE tasks ADD COLUMN attachments TEXT"),
            ("progress", "ALTER TABLE tasks ADD COLUMN progress TEXT NOT NULL DEFAULT ''"),
            ("a2a_state", "ALTER TABLE tasks ADD COLUMN a2a_state TEXT NOT NULL DEFAULT ''"),
            ("attempts", "ALTER TABLE tasks ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0"),
            ("heartbeat_at", "ALTER TABLE tasks ADD COLUMN heartbeat_at REAL"),
        ):
            if name not in tcols:
                self.run(ddl)
        cols = {r["name"] for r in self.q("PRAGMA table_info(bots)")}
        for name, ddl in (
            ("description", "ALTER TABLE bots ADD COLUMN description TEXT NOT NULL DEFAULT ''"),
            ("a2a_policy", "ALTER TABLE bots ADD COLUMN a2a_policy TEXT NOT NULL DEFAULT ''"),
            ("routes_mode", "ALTER TABLE bots ADD COLUMN routes_mode TEXT NOT NULL DEFAULT 'explicit'"),
        ):
            if name not in cols:
                self.run(ddl)
        rcols = {r["name"] for r in self.q("PRAGMA table_info(routes)")}
        if "context_mode" not in rcols:
            self.run("ALTER TABLE routes ADD COLUMN context_mode TEXT NOT NULL DEFAULT ''")
        self.run(
            "CREATE TABLE IF NOT EXISTS inbox_msgs (message_id TEXT PRIMARY KEY, task_id TEXT, "
            "created_at REAL NOT NULL)")
        cols = {r["name"] for r in self.q("PRAGMA table_info(inbox_msgs)")}
        if "fingerprint" not in cols:
            self.run("ALTER TABLE inbox_msgs ADD COLUMN fingerprint TEXT")
        self.run(
            "CREATE TABLE IF NOT EXISTS peer_health (alias TEXT PRIMARY KEY, failures INTEGER NOT NULL "
            "DEFAULT 0, blocked_until REAL NOT NULL DEFAULT 0, last_error TEXT, updated_at REAL NOT NULL)")
        # Внешние A2A-клиенты и межвладельческие связи сняты: только владелец и его боты.
        for dropped in ("clients", "links", "invites"):
            self.run(f"DROP TABLE IF EXISTS {dropped}")

    # ── helpers ──────────────────────────────────────────────────────────────
    def run(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        """Запись/DDL под замком."""
        with self._lock:
            return self.conn.execute(sql, args)

    def q(self, sql: str, args: tuple = ()) -> List[sqlite3.Row]:
        with self._lock:
            return list(self.run(sql, args))

    def one(self, sql: str, args: tuple = ()) -> Optional[sqlite3.Row]:
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def event(self, kind: str, *, task_id: str | None = None, owner: int | None = None,
              **detail: Any) -> None:
        self.run(
            "INSERT INTO events (ts, kind, task_id, owner_tg, detail) VALUES (?,?,?,?,?)",
            (now(), kind, task_id, owner, json.dumps(detail, ensure_ascii=False)[:2000]))

    # ── owners / bots ────────────────────────────────────────────────────────
    def ensure_owner(self, tg_id: int, name: str = "") -> None:
        self.run("INSERT OR IGNORE INTO owners (tg_id, name, created_at) VALUES (?,?,?)",
                 (tg_id, name, now()))

    def owner(self, tg_id: int) -> Optional[sqlite3.Row]:
        return self.one("SELECT * FROM owners WHERE tg_id=?", (tg_id,))

    def add_pair_code(self, owner: int, alias: str, code: str) -> None:
        self.run("INSERT INTO pair_codes (code, owner_tg, alias, created_at) VALUES (?,?,?,?)",
                 (code.upper(), owner, alias, now()))

    def take_pair_code(self, code: str) -> Optional[sqlite3.Row]:
        row = self.one("SELECT * FROM pair_codes WHERE code=? AND used_at IS NULL", (code.upper(),))
        if row:
            self.run("UPDATE pair_codes SET used_at=? WHERE code=?", (now(), code.upper()))
        return row

    def bot_owner(self, bot_id: int) -> int | None:
        row = self.one("SELECT owner_tg FROM bots WHERE bot_id=?", (bot_id,))
        return int(row["owner_tg"]) if row else None

    def set_enabled(self, bot_id: int, enabled: bool) -> None:
        self.run("UPDATE bots SET enabled=? WHERE bot_id=?", (1 if enabled else 0, bot_id))

    def pair_bot(self, *, bot_id: int, username: str, alias: str, owner: int,
                 kind: str = "agent") -> bool:
        self.run(
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

    def bot_by_username(self, username: str) -> Optional[sqlite3.Row]:
        return self.one("SELECT * FROM bots WHERE username=? AND enabled=1",
                        (username.lstrip("@"),))

    def bots(self, owner: int) -> List[sqlite3.Row]:
        return self.q("SELECT * FROM bots WHERE owner_tg=? ORDER BY alias", (owner,))

    def set_description(self, owner: int, alias: str, text: str) -> bool:
        bot = self.bot_by_alias(owner, alias)
        if not bot:
            return False
        self.run("UPDATE bots SET description=? WHERE bot_id=?", (text[:300], int(bot["bot_id"])))
        return True

    def set_routes_mode(self, owner: int, alias: str, mode: str) -> None:
        self.run("UPDATE bots SET routes_mode=? WHERE owner_tg=? AND alias=?",
                 ("explicit" if mode == "explicit" else "all", owner, alias))

    def set_a2a_policy(self, owner: int, alias: str, policy: str) -> None:
        self.run("UPDATE bots SET a2a_policy=? WHERE owner_tg=? AND alias=?",
                 (policy if policy in ("ask", "auto") else "ask", owner, alias))

    def seen_message(self, message_id: str) -> Optional[str]:
        row = self.one("SELECT task_id FROM inbox_msgs WHERE message_id=?", (message_id,))
        return row["task_id"] if row else None

    def remember_message(self, message_id: str, task_id: str, fingerprint: str = "") -> None:
        self.run("INSERT OR REPLACE INTO inbox_msgs (message_id, task_id, created_at, "
                 "fingerprint) VALUES (?,?,?,?)", (message_id, task_id, now(), fingerprint))

    def seen_fingerprint(self, fingerprint: str, window_s: float = 300.0) -> Optional[str]:
        row = self.one("SELECT task_id FROM inbox_msgs WHERE fingerprint=? AND created_at > ?",
                       (fingerprint, now() - window_s))
        return row["task_id"] if row else None

    # ── здоровье пиров ───────────────────────────────────────────────────────
    def peer_state(self, alias: str) -> sqlite3.Row:
        row = self.one("SELECT * FROM peer_health WHERE alias=?", (alias,))
        if row is None:
            self.run("INSERT INTO peer_health (alias, updated_at) VALUES (?,?)", (alias, now()))
            row = self.one("SELECT * FROM peer_health WHERE alias=?", (alias,))
        return row

    def peer_ok(self, alias: str) -> None:
        self.run("UPDATE peer_health SET failures=0, blocked_until=0, last_error=NULL, "
                 "updated_at=? WHERE alias=?", (now(), alias))

    def peer_failed(self, alias: str, error: str, *, threshold: int = 3,
                    block_s: float = 300.0) -> bool:
        row = self.peer_state(alias)
        failures = int(row["failures"]) + 1
        blocked = 0.0
        if failures >= threshold:
            blocked = now() + block_s
            failures = 0
        self.run("UPDATE peer_health SET failures=?, blocked_until=?, last_error=?, "
                 "updated_at=? WHERE alias=?",
                 (failures, blocked, error[:200], now(), alias))
        return bool(blocked)

    def peer_blocked(self, alias: str) -> float:
        row = self.peer_state(alias)
        return float(row["blocked_until"] or 0.0)

    # ── routes ───────────────────────────────────────────────────────────────
    def route(self, owner: int, src: str, dst: str) -> Optional[sqlite3.Row]:
        return self.one("SELECT * FROM routes WHERE owner_tg=? AND src_alias=? AND dst_alias=?",
                        (owner, src, dst))

    def set_route(self, owner: int, src: str, dst: str, *, policy: str = "ask",
                  max_text: int = 3000, per_hour: int = 20) -> None:
        self.run(
            "INSERT INTO routes (owner_tg, src_alias, dst_alias, policy, max_text, per_hour, "
            "created_at) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(owner_tg, src_alias, dst_alias) DO UPDATE SET policy=excluded.policy, "
            "max_text=excluded.max_text, per_hour=excluded.per_hour",
            (owner, src, dst, policy, max_text, per_hour, now()))

    def delete_route(self, owner: int, route_id: int) -> bool:
        cur = self.run("DELETE FROM routes WHERE id=? AND owner_tg=?", (route_id, owner))
        return bool(cur.rowcount)

    def routes(self, owner: int) -> List[sqlite3.Row]:
        return self.q("SELECT * FROM routes WHERE owner_tg=? ORDER BY src_alias, dst_alias",
                      (owner,))

    def set_context_mode(self, owner: int, route_id: int, mode: str) -> None:
        self.run("UPDATE routes SET context_mode=? WHERE id=? AND owner_tg=?",
                 (mode if mode in ("on", "off") else "", int(route_id), owner))

    def context_turns(self, owner: int, ctx: str, *, before: float, limit: int = 5) -> list:
        return self.q(
            "SELECT src_alias, dst_alias, text, answer, status, created_at FROM tasks "
            "WHERE owner_tg=? AND ctx=? AND created_at < ? AND status NOT IN ('failed','rejected') "
            "ORDER BY created_at DESC LIMIT ?", (owner, ctx, before, int(limit)))

    # ── состояние диалога (inline-кнопки не умеют вводить текст) ─────────────
    def set_state(self, owner: int, action: str, payload: str = "") -> None:
        self.run("INSERT INTO owner_state (owner_tg, action, payload, created_at) "
                 "VALUES (?,?,?,?) ON CONFLICT(owner_tg) DO UPDATE SET "
                 "action=excluded.action, payload=excluded.payload, "
                 "created_at=excluded.created_at", (owner, action, payload, now()))

    def get_state(self, owner: int, ttl_s: float = 600) -> Optional[sqlite3.Row]:
        row = self.one("SELECT * FROM owner_state WHERE owner_tg=?", (owner,))
        if row and float(row["created_at"]) + ttl_s < now():
            self.clear_state(owner)
            return None
        return row

    def clear_state(self, owner: int) -> None:
        self.run("DELETE FROM owner_state WHERE owner_tg=?", (owner,))
        self.run("DELETE FROM owner_state WHERE created_at < ?", (now() - 3600,))

    # ── tasks ────────────────────────────────────────────────────────────────
    def create_task(self, *, task_id: str, owner: int, direction: str, ctx: str, hops: int,
                    src: str, dst: str, text: str, status: str = "submitted",
                    card_chat: int | None = None, card_msg: int | None = None,
                    attachments: str | None = None) -> None:
        ts = now()
        self.run(
            "INSERT INTO tasks (task_id, owner_tg, direction, ctx, hops, src_alias, dst_alias, "
            "text, status, a2a_state, card_chat, card_msg, created_at, updated_at, "
            "heartbeat_at, attachments) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (task_id, owner, direction, ctx, hops, src, dst, text, status, a2a_state(status),
             card_chat, card_msg, ts, ts, ts, attachments))
        self.add_message(task_id, "user", text)

    def update_task(self, task_id: str, **fields: Any) -> None:
        if not fields:
            return
        if "status" in fields and "a2a_state" not in fields:
            fields["a2a_state"] = a2a_state(str(fields["status"]))
        cols = ", ".join(f"{k}=?" for k in fields)
        self.run(f"UPDATE tasks SET {cols}, updated_at=? WHERE task_id=?",
                 (*fields.values(), now(), task_id))

    def heartbeat(self, task_id: str, progress: str = "") -> None:
        self.update_task(task_id, heartbeat_at=now(), progress=progress[:300])

    def task(self, task_id: str) -> Optional[sqlite3.Row]:
        return self.one("SELECT * FROM tasks WHERE task_id=?", (task_id,))

    def open_tasks(self, owner: int) -> List[sqlite3.Row]:
        return self.q("SELECT * FROM tasks WHERE owner_tg=? AND status IN "
                      "('submitted','working','input-required')", (owner,))

    def hours_used(self, owner: int, src: str, dst: str) -> int:
        row = self.one("SELECT COUNT(*) AS n FROM tasks WHERE owner_tg=? AND src_alias=? AND "
                       "dst_alias=? AND created_at > ?", (owner, src, dst, now() - 3600))
        return int(row["n"]) if row else 0

    def add_message(self, task_id: str, role: str, text: str) -> None:
        if not (text or "").strip():
            return
        self.run("INSERT INTO task_messages (task_id, role, text, ts) VALUES (?,?,?,?)",
                 (task_id, role, text, now()))

    def messages(self, task_id: str, limit: int = 20) -> List[sqlite3.Row]:
        rows = self.q("SELECT * FROM task_messages WHERE task_id=? ORDER BY ts DESC LIMIT ?",
                      (task_id, int(limit)))
        return list(reversed(rows))

    def list_tasks(self, owner: int, *, status: str = "", ctx: str = "", since: float = 0.0,
                   cursor: float = 0.0, limit: int = 50) -> List[sqlite3.Row]:
        """Сортировка по updated_at DESC; cursor — updated_at последней записи прошлой страницы."""
        sql = "SELECT * FROM tasks WHERE owner_tg=?"
        args: list = [owner]
        if status:
            sql += " AND status=?"
            args.append(status)
        if ctx:
            sql += " AND ctx=?"
            args.append(ctx)
        if since:
            sql += " AND updated_at > ?"
            args.append(float(since))
        if cursor:
            sql += " AND updated_at < ?"
            args.append(float(cursor))
        sql += " ORDER BY updated_at DESC LIMIT ?"
        args.append(max(1, min(int(limit), 100)))
        return self.q(sql, tuple(args))

    def expire(self, owner: int) -> List[sqlite3.Row]:
        """Протухшие карточки: submitted старше CARD_TTL_S → failed(card_expired)."""
        stale = self.q("SELECT * FROM tasks WHERE owner_tg=? AND status='submitted' "
                       "AND created_at < ?", (owner, now() - CARD_TTL_S))
        for row in stale:
            self.update_task(row["task_id"], status="failed", progress="карточка истекла")
            self.event("card_expired", task_id=row["task_id"], owner=owner)
        return stale
