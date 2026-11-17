#!/usr/bin/env python3
"""Tests for the gateway-relay task state machine (no network, no Hermes)."""
from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from state import (  # noqa: E402
    ALLOWED,
    CARD_TTL_S,
    STATUSES,
    TASK_TTL_S,
    TERMINAL,
    TaskStore,
    TransitionError,
)


def new_store(**kwargs) -> TaskStore:
    tmp = Path(tempfile.mkdtemp(prefix="gwstate-"))
    return TaskStore(tmp / "state.json", **kwargs)


def test_create_and_transitions() -> dict:
    store = new_store()
    with store.mutate() as data:
        task = store.create(data, direction="out", peer="peer", text="привет",
                            chat_id=-100, thread_id=2)
        assert task["status"] == "submitted"
        assert task["task_id"] and task["context_id"]
        store.transition(data, task["task_id"], "working")
        store.transition(data, task["task_id"], "completed")
        assert store.get(data, task["task_id"])["status"] == "completed"
    return {"create_and_transitions": True}


def test_illegal_transitions() -> dict:
    store = new_store()
    with store.mutate() as data:
        task = store.create(data, direction="out", peer="peer", text="x")
        store.transition(data, task["task_id"], "working")
        for target in ("rejected", "submitted"):
            try:
                store.transition(data, task["task_id"], target)
            except TransitionError:
                continue
            raise AssertionError(f"working -> {target} must be refused")
        store.transition(data, task["task_id"], "input-required")
        store.transition(data, task["task_id"], "working")
        store.transition(data, task["task_id"], "completed")
        try:
            store.transition(data, task["task_id"], "working")
        except TransitionError:
            pass
        else:
            raise AssertionError("completed is terminal")
    return {"illegal_transitions": True}


def test_status_vocabulary() -> dict:
    assert {"submitted", "working", "input-required", "completed", "failed",
            "rejected", "cancelled", "skipped"} <= set(ALLOWED) | set(STATUSES)
    assert "completed" in TERMINAL and "working" not in TERMINAL
    return {"status_vocabulary": True}


def test_card_expiry_and_task_expiry() -> dict:
    store = new_store(card_ttl=60, task_ttl=3600)
    now = time.time()
    with store.mutate() as data:
        parked = store.create(data, direction="in", peer="peer", text="a")
        parked["created"] = now - 120          # старше карточки
        working = store.create(data, direction="out", peer="peer", text="b")
        store.transition(data, working["task_id"], "working")
        working["created"] = now - 120          # карточка уже не нужна
        old = store.create(data, direction="out", peer="peer", text="c")
        store.transition(data, old["task_id"], "working")
        old["created"] = now - 4000             # старше задачи
        changed = store.expire(data)
        assert parked["task_id"] in changed and old["task_id"] in changed, changed
        assert working["task_id"] not in changed, changed
        assert store.get(data, parked["task_id"])["last_error"] == "card_expired"
        assert store.get(data, old["task_id"])["last_error"] == "task_timeout"
        assert store.get(data, working["task_id"])["status"] == "working"
    return {"card_and_task_expiry": True}


def test_active_filter() -> dict:
    store = new_store()
    with store.mutate() as data:
        a = store.create(data, direction="out", peer="p", text="1")
        b = store.create(data, direction="in", peer="p", text="2")
        store.transition(data, b["task_id"], "working")
        store.transition(data, b["task_id"], "completed")
        assert [t["task_id"] for t in store.active(data)] == [a["task_id"]]
        assert len(store.active(data, "in")) == 0
    return {"active_filter": True}


def test_legacy_migration() -> dict:
    store = new_store()
    legacy = {
        "pending_out": {"abc123": {"target": "peer", "text": "out-text", "created": time.time()}},
        "pending_in": {"def456": {"from_bot": "peer", "text": "in-text", "created": time.time()}},
        "approved_in": {"ghi789": {"from_bot": "peer", "text": "approved", "created": time.time()}},
        "sent": [time.time()],
    }
    store.path.write_text(json.dumps(legacy))
    data = store.read()
    assert "_migrated_v2" in data and not data.get("pending_out") and not data.get("pending_in")
    tasks = data["tasks"]
    assert len(tasks) == 3, tasks
    statuses = sorted(t["status"] for t in tasks.values())
    assert statuses == ["submitted", "submitted", "working"], statuses
    # миграция идемпотентна
    again = store.read()
    assert len(again["tasks"]) == 3
    return {"legacy_migration": True}


def test_working_to_skipped() -> dict:
    """SKIP в drain — штатный исход working-задачи."""
    store = new_store()
    with store.mutate() as data:
        task = store.create(data, direction="in", peer="peer", text="без вопроса")
        store.transition(data, task["task_id"], "working")
        store.transition(data, task["task_id"], "skipped")
    assert store.get(store.read(), task["task_id"])["status"] == "skipped"
    return {"working_to_skipped": True}


def test_persistence_roundtrip() -> dict:
    store = new_store()
    with store.mutate() as data:
        task = store.create(data, direction="out", peer="peer", text="persist")
        store.transition(data, task["task_id"], "working")
        tid = task["task_id"]
    reopened = TaskStore(store.path)
    data = reopened.read()
    assert reopened.get(data, tid)["status"] == "working"
    assert reopened.get(data, tid)["text"] == "persist"
    mode = oct(store.path.stat().st_mode & 0o777)
    assert mode == "0o600", mode
    return {"persistence_roundtrip": True}


def test_migration_backup() -> dict:
    """Перед первой конвертацией 0.1.x остаётся копия .bak-<ts>, и только одна."""
    store = new_store()
    store.path.write_text(json.dumps({"pending": {"a": {"text": "x"}}, "sent": [], "inbox": []}),
                          encoding="utf-8")
    data = store.read()
    assert data.get("_migrated_v2") is True
    baks = sorted(store.path.parent.glob(store.path.name + ".bak-*"))
    assert len(baks) == 1, baks
    assert json.loads(baks[0].read_text(encoding="utf-8"))["pending"], "backup is not the 0.1 doc"
    store.read()
    assert len(sorted(store.path.parent.glob(store.path.name + ".bak-*"))) == 1
    return {"migration_backup": True}


def main() -> int:
    results = {}
    for name, fn in (
        ("create", test_create_and_transitions),
        ("illegal", test_illegal_transitions),
        ("vocabulary", test_status_vocabulary),
        ("expiry", test_card_expiry_and_task_expiry),
        ("active", test_active_filter),
        ("migration", test_legacy_migration),
        ("backup", test_migration_backup),
        ("working_skip", test_working_to_skipped),
        ("persistence", test_persistence_roundtrip),
    ):
        results.update(fn())
        assert results, name
    print(json.dumps(results, ensure_ascii=False))
    print(f"OK: {len(results)} state groups passed (card_ttl={CARD_TTL_S}s, task_ttl={TASK_TTL_S}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
