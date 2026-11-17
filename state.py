"""Task state machine for gateway-relay.

A relayed message is a *task* with an explicit lifecycle, not a set of ad-hoc
buckets. Two clocks: a short one for the approval card (the owner must decide) and
a long one for the task itself (the exchange may take hours).

States and allowed transitions::

    submitted ──► working ──► completed
        │            ├────► input-required ──► working
        ├──► rejected│
        ├──► cancelled
        └──► failed

Everything is persisted in one JSON file next to the routes registry. The file is
shared by the two plugin instances in the same gateway process (ingress copy and
the routed profile), so writes are serialised with an ``fcntl`` lock.
"""
from __future__ import annotations

import fcntl
import json
import os
import secrets
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

STATUSES = (
    "submitted",      # parked, card is waiting for the owner's click
    "working",        # approved; the quiet turn / delivery is in flight
    "input-required",  # our profile asked a question (ASK), owner's answer pending
    "completed",      # delivered, and for inbound tasks — answered
    "failed",         # timeout, delivery error
    "rejected",       # owner clicked "Отклонить"
    "cancelled",      # initiator cancelled after submission
    "skipped",        # nothing to say (SKIP / hop budget)
)

TERMINAL = frozenset({"completed", "failed", "rejected", "cancelled", "skipped"})

ALLOWED: Dict[str, frozenset] = {
    "submitted": frozenset({"working", "rejected", "cancelled", "failed"}),
    "working": frozenset({"completed", "input-required", "failed", "cancelled", "skipped"}),
    "input-required": frozenset({"working", "failed", "cancelled", "skipped"}),
}

CARD_TTL_S = 1200        # the approval card must be answered within 20 minutes
TASK_TTL_S = 86400       # the task itself lives a day


class TransitionError(ValueError):
    """Raised when a status change is not allowed by the state machine."""


def new_id(n: int = 3) -> str:
    return secrets.token_hex(n)


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    """Cross-instance lock: two plugin copies live in one gateway process."""
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class TaskStore:
    """Small, testable store: JSON file + status machine + two clocks."""

    def __init__(self, path: Path, *, card_ttl: int = CARD_TTL_S, task_ttl: int = TASK_TTL_S):
        self.path = Path(path)
        self.card_ttl = int(card_ttl)
        self.task_ttl = int(task_ttl)

    # ── persistence ──────────────────────────────────────────────────────────
    def read(self) -> Dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8") or "{}")
        except Exception:
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        if raw and not raw.get("_migrated_v2"):
            self._backup_once()
        return self._migrate(raw)

    def _backup_once(self) -> None:
        """Копия 0.1.x-документа перед первой конвертацией в задачи."""
        try:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            target = self.path.with_suffix(self.path.suffix + f".bak-{stamp}")
            if not target.exists():
                target.write_text(self.path.read_text(encoding="utf-8"), encoding="utf-8")
                os.chmod(target, 0o600)
        except OSError:
            pass

    def write(self, data: Dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    @contextmanager
    def mutate(self) -> Iterator[Dict[str, Any]]:
        """Read-modify-write under the cross-instance lock."""
        with _locked(self.path):
            data = self.read()
            yield data
            self.write(data)

    # ── state machine ────────────────────────────────────────────────────────
    @staticmethod
    def create(
        data: Dict[str, Any], *, direction: str, peer: str, text: str,
        chat_id: Any = None, thread_id: Any = None, message_id: Any = None,
        context_id: Optional[str] = None, hops: int = 1,
        card_chat: Any = None, card_msg: Any = None,
    ) -> Dict[str, Any]:
        now = time.time()
        task_id = new_id()
        ctx = context_id or new_id(4)
        task = {
            "task_id": task_id,
            "context_id": ctx,
            "direction": direction,          # "out" (we send) | "in" (we receive)
            "peer": peer,
            "status": "submitted",
            "text": text,
            "question": None,
            "answer": None,
            "chat_id": chat_id,
            "thread_id": thread_id,
            "message_id": message_id,
            "card_chat": card_chat,
            "card_msg": card_msg,
            "hops": int(hops),
            "created": now,
            "updated": now,
            "last_error": None,
        }
        data.setdefault("tasks", {})[task_id] = task
        return task

    @staticmethod
    def get(data: Dict[str, Any], task_id: str) -> Optional[Dict[str, Any]]:
        return (data.get("tasks") or {}).get(task_id)

    @staticmethod
    def transition(data: Dict[str, Any], task_id: str, status: str, **fields: Any) -> Dict[str, Any]:
        task = TaskStore.get(data, task_id)
        if task is None:
            raise TransitionError(f"unknown task {task_id}")
        current = task.get("status")
        if status != current:
            allowed = ALLOWED.get(current, frozenset())
            if status not in allowed:
                raise TransitionError(f"{current} -> {status} not allowed")
        task["status"] = status
        task["updated"] = time.time()
        for key, value in fields.items():
            task[key] = value
        return task

    @staticmethod
    def is_terminal(task: Dict[str, Any]) -> bool:
        return task.get("status") in TERMINAL

    @staticmethod
    def active(data: Dict[str, Any], direction: Optional[str] = None) -> List[Dict[str, Any]]:
        tasks = list((data.get("tasks") or {}).values())
        if direction:
            tasks = [t for t in tasks if t.get("direction") == direction]
        return [t for t in tasks if t.get("status") not in TERMINAL]

    # ── clocks ───────────────────────────────────────────────────────────────
    def expire(self, data: Dict[str, Any], *, now: Optional[float] = None) -> List[str]:
        """Fail tasks whose clock ran out. Returns the ids that changed."""
        now = time.time() if now is None else now
        changed: List[str] = []
        for task in (data.get("tasks") or {}).values():
            if task.get("status") in TERMINAL:
                continue
            age = now - float(task.get("created") or now)
            status = task.get("status")
            reason = None
            if age > self.task_ttl:
                reason = "task_timeout"
            elif status == "submitted" and age > self.card_ttl:
                reason = "card_expired"
            if reason is None:
                continue
            try:
                self.transition(data, task["task_id"], "failed", last_error=reason)
            except TransitionError:
                continue
            changed.append(task["task_id"])
        return changed

    # ── legacy ───────────────────────────────────────────────────────────────
    @staticmethod
    def _migrate(raw: Dict[str, Any]) -> Dict[str, Any]:
        """Convert 0.1.x buckets (pending_out/pending_in/approved_in) into tasks."""
        if raw.get("_migrated_v2"):
            return raw
        tasks = raw.get("tasks") if isinstance(raw.get("tasks"), dict) else {}
        now = time.time()
        for bucket, direction in (("pending_out", "out"), ("pending_in", "in"), ("approved_in", "in")):
            items = raw.get(bucket) or {}
            if not isinstance(items, dict):
                continue
            for req, item in items.items():
                if not isinstance(item, dict):
                    continue
                task_id = f"legacy-{req}"
                if task_id in tasks:
                    continue
                status = "working" if bucket == "approved_in" else "submitted"
                tasks[task_id] = {
                    "task_id": task_id,
                    "context_id": req,
                    "direction": direction,
                    "peer": item.get("from_bot") or item.get("target") or "unknown",
                    "status": status,
                    "text": item.get("text") or "",
                    "question": None,
                    "answer": None,
                    "chat_id": item.get("chat_id"),
                    "thread_id": item.get("thread_id"),
                    "message_id": item.get("message_id"),
                    "card_chat": None,
                    "card_msg": None,
                    "hops": 1,
                    "created": float(item.get("created") or now),
                    "updated": now,
                    "last_error": None,
                }
        raw["tasks"] = tasks
        for bucket in ("pending_out", "pending_in", "approved_in"):
            raw.pop(bucket, None)
        raw["_migrated_v2"] = True
        return raw
