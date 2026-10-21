"""SQLite storage: saved analyses, payout batches + items, and webhook events.

One short-lived connection per call keeps it safe under FastAPI's thread pool.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

from backend import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS analyses (
    id          TEXT PRIMARY KEY,
    repo        TEXT NOT NULL,
    budget      REAL NOT NULL,
    created_at  TEXT NOT NULL,
    result      TEXT NOT NULL            -- full analyze_repo() output as JSON
);
CREATE TABLE IF NOT EXISTS payout_batches (
    batch_id         TEXT PRIMARY KEY,   -- PayPal payout_batch_id
    analysis_id      TEXT REFERENCES analyses(id),
    sender_batch_id  TEXT UNIQUE NOT NULL,
    total            REAL NOT NULL,
    status           TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS payout_items (
    sender_item_id   TEXT NOT NULL,      -- the package name
    batch_id         TEXT NOT NULL REFERENCES payout_batches(batch_id),
    payout_item_id   TEXT,               -- PayPal's id, known once the batch is processed
    email            TEXT NOT NULL,
    amount           REAL NOT NULL,
    status           TEXT,
    updated_at       TEXT NOT NULL,
    PRIMARY KEY (batch_id, sender_item_id)
);
CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at  TEXT NOT NULL,
    event_type   TEXT NOT NULL,
    resource_id  TEXT,
    message      TEXT,
    verified     INTEGER,                -- 1, 0, or NULL when verification isn't configured
    payload      TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect():
    conn = sqlite3.connect(config.DATABASE_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init():
    config.DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with connect() as c:
        c.execute("PRAGMA journal_mode = WAL")
        c.executescript(SCHEMA)


# ---------- analyses ----------

def save_analysis(result: dict) -> str:
    analysis_id = uuid.uuid4().hex[:12]
    with connect() as c:
        c.execute("INSERT INTO analyses (id, repo, budget, created_at, result) VALUES (?,?,?,?,?)",
                  (analysis_id, result["repo"], result["budget"], now(), json.dumps(result)))
    return analysis_id


def get_analysis(analysis_id: str) -> dict | None:
    with connect() as c:
        row = c.execute("SELECT * FROM analyses WHERE id = ?", (analysis_id,)).fetchone()
    if not row:
        return None
    return {**json.loads(row["result"]), "analysis_id": row["id"], "created_at": row["created_at"]}


def list_analyses(limit: int = 20) -> list[dict]:
    with connect() as c:
        rows = c.execute("SELECT a.id, a.repo, a.budget, a.created_at, b.batch_id, b.status "
                         "FROM analyses a LEFT JOIN payout_batches b ON b.analysis_id = a.id "
                         "ORDER BY a.created_at DESC LIMIT ?", (limit,)).fetchall()
    return [{"analysis_id": r["id"], "repo": r["repo"], "budget": r["budget"],
             "created_at": r["created_at"], "batch_id": r["batch_id"],
             "batch_status": r["status"]} for r in rows]


# ---------- payouts ----------

def batch_for_analysis(analysis_id: str) -> dict | None:
    with connect() as c:
        row = c.execute("SELECT * FROM payout_batches WHERE analysis_id = ?",
                        (analysis_id,)).fetchone()
    return dict(row) if row else None


def save_batch(batch_id: str, analysis_id: str | None, sender_batch_id: str, status: str | None,
               items: list[dict]):
    """items: [{"item_id", "email", "amount"}], as passed to payouts.create_batch()."""
    ts = now()
    with connect() as c:
        c.execute("INSERT INTO payout_batches VALUES (?,?,?,?,?,?,?)",
                  (batch_id, analysis_id, sender_batch_id,
                   round(sum(i["amount"] for i in items), 2), status, ts, ts))
        c.executemany("INSERT INTO payout_items (sender_item_id, batch_id, email, amount, "
                      "updated_at) VALUES (?,?,?,?,?)",
                      [(i["item_id"], batch_id, i["email"], i["amount"], ts) for i in items])


def update_batch_status(batch_id: str, status: str | None):
    with connect() as c:
        c.execute("UPDATE payout_batches SET status = ?, updated_at = ? WHERE batch_id = ?",
                  (status, now(), batch_id))


def update_item(batch_id: str, sender_item_id: str, *, status: str | None,
                payout_item_id: str | None = None):
    with connect() as c:
        c.execute("UPDATE payout_items SET status = ?, "
                  "payout_item_id = COALESCE(?, payout_item_id), updated_at = ? "
                  "WHERE batch_id = ? AND sender_item_id = ?",
                  (status, payout_item_id, now(), batch_id, sender_item_id))


def get_batch(batch_id: str) -> dict | None:
    with connect() as c:
        row = c.execute("SELECT * FROM payout_batches WHERE batch_id = ?", (batch_id,)).fetchone()
        if not row:
            return None
        items = c.execute("SELECT * FROM payout_items WHERE batch_id = ? ORDER BY amount DESC",
                          (batch_id,)).fetchall()
    return {**dict(row), "items": [dict(i) for i in items]}


# ---------- webhook events ----------

def save_event(event: dict, *, message: str, verified: bool | None) -> dict:
    row = {"received_at": now(), "event_type": event.get("event_type", "UNKNOWN"),
           "resource_id": (event.get("resource") or {}).get("id"),
           "message": message, "verified": verified}
    with connect() as c:
        c.execute("INSERT INTO events (received_at, event_type, resource_id, message, verified, "
                  "payload) VALUES (?,?,?,?,?,?)",
                  (row["received_at"], row["event_type"], row["resource_id"], message,
                   None if verified is None else int(verified), json.dumps(event)))
    return row


def list_events(limit: int = 200) -> list[dict]:
    with connect() as c:
        rows = c.execute("SELECT received_at, event_type, resource_id, message, verified "
                         "FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [{**dict(r), "verified": None if r["verified"] is None else bool(r["verified"])}
            for r in rows]
