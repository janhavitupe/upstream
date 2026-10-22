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
CREATE TABLE IF NOT EXISTS carryover (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    package           TEXT NOT NULL,
    amount            REAL NOT NULL,
    source_batch_id   TEXT NOT NULL,
    source_item_id    TEXT NOT NULL,      -- the unclaimed item's sender_item_id
    created_at        TEXT NOT NULL,
    used_by_analysis  TEXT,               -- set when a later approved payout includes it
    UNIQUE (source_batch_id, source_item_id)
);
CREATE TABLE IF NOT EXISTS payroll_schedules (
    id               TEXT PRIMARY KEY,
    repo             TEXT NOT NULL,
    owner_login      TEXT NOT NULL,        -- excluded from the split: they're paying
    total            REAL NOT NULL,        -- monthly amount, shared by the top contributors
    pay_day          INTEGER NOT NULL,     -- 1-28, every month
    top_n            INTEGER NOT NULL,
    notice_days      INTEGER NOT NULL,     -- heads-up before payday
    emails           TEXT NOT NULL,        -- JSON {login: paypal email}
    active           INTEGER NOT NULL,
    created_at       TEXT NOT NULL,
    approved_at      TEXT NOT NULL,        -- the owner's standing approval
    last_window_end  TEXT                  -- work up to here has been paid or skipped
);
CREATE TABLE IF NOT EXISTS payroll_runs (
    id            TEXT PRIMARY KEY,
    schedule_id   TEXT NOT NULL REFERENCES payroll_schedules(id),
    period        TEXT NOT NULL,           -- YYYY-MM of the payday
    status        TEXT NOT NULL,           -- preview | skipped | paid | failed
    created_at    TEXT NOT NULL,
    pay_after     TEXT NOT NULL,           -- not before payday, and not before the heads-up ran
    window_start  TEXT NOT NULL,
    window_end    TEXT NOT NULL,
    revision      INTEGER NOT NULL,
    result        TEXT NOT NULL,           -- JSON split
    batch_id      TEXT,
    paid_at       TEXT,
    note          TEXT,
    UNIQUE (schedule_id, period)
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


def update_analysis(analysis_id: str, result: dict):
    result = {k: v for k, v in result.items() if k not in ("analysis_id", "created_at")}
    with connect() as c:
        c.execute("UPDATE analyses SET result = ? WHERE id = ?", (json.dumps(result), analysis_id))


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


# ---------- carry-over from unclaimed payouts ----------

def add_carryover(package: str, amount: float, batch_id: str, item_id: str) -> bool:
    """False if this item was already re-queued."""
    with connect() as c:
        cur = c.execute("INSERT OR IGNORE INTO carryover (package, amount, source_batch_id, "
                        "source_item_id, created_at) VALUES (?,?,?,?,?)",
                        (package, amount, batch_id, item_id, now()))
    return cur.rowcount == 1


def open_carryover() -> list[dict]:
    with connect() as c:
        rows = c.execute("SELECT * FROM carryover WHERE used_by_analysis IS NULL "
                         "ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def claim_carryover(ids: list[int], analysis_id: str) -> bool:
    """Mark carry-over as used by this analysis; False if any was already used elsewhere."""
    if not ids:
        return True
    with connect() as c:
        marks = ",".join("?" * len(ids))
        cur = c.execute(f"UPDATE carryover SET used_by_analysis = ? WHERE id IN ({marks}) "
                        "AND used_by_analysis IS NULL", (analysis_id, *ids))
        if cur.rowcount != len(ids):
            c.rollback()
            return False
    return True


def release_carryover(analysis_id: str):
    with connect() as c:
        c.execute("UPDATE carryover SET used_by_analysis = NULL WHERE used_by_analysis = ?",
                  (analysis_id,))


# ---------- team payroll ----------

def _schedule(row) -> dict:
    return {**dict(row), "emails": json.loads(row["emails"]), "active": bool(row["active"])}


def _run(row) -> dict:
    return {**dict(row), "result": json.loads(row["result"])}


def create_schedule(s: dict) -> str:
    sid = uuid.uuid4().hex[:12]
    ts = now()
    with connect() as c:
        c.execute("INSERT INTO payroll_schedules (id, repo, owner_login, total, pay_day, top_n, "
                  "notice_days, emails, active, created_at, approved_at, last_window_end) "
                  "VALUES (?,?,?,?,?,?,?,?,1,?,?,?)",
                  (sid, s["repo"], s["owner_login"], s["total"], s["pay_day"], s["top_n"],
                   s["notice_days"], json.dumps(s.get("emails") or {}), ts, ts,
                   s.get("last_window_end")))
    return sid


def get_schedule(sid: str) -> dict | None:
    with connect() as c:
        row = c.execute("SELECT * FROM payroll_schedules WHERE id = ?", (sid,)).fetchone()
    return _schedule(row) if row else None


def list_schedules(active_only: bool = False) -> list[dict]:
    q = "SELECT * FROM payroll_schedules" + (" WHERE active = 1" if active_only else "")
    with connect() as c:
        rows = c.execute(q + " ORDER BY created_at DESC").fetchall()
    return [_schedule(r) for r in rows]


def update_schedule(sid: str, **fields):
    if "emails" in fields:
        fields["emails"] = json.dumps(fields["emails"])
    if "active" in fields:
        fields["active"] = int(fields["active"])
    cols = ", ".join(f"{k} = ?" for k in fields)
    with connect() as c:
        c.execute(f"UPDATE payroll_schedules SET {cols} WHERE id = ?", (*fields.values(), sid))


def create_run(run: dict) -> bool:
    """False if this schedule already has a run for this period."""
    with connect() as c:
        cur = c.execute("INSERT OR IGNORE INTO payroll_runs (id, schedule_id, period, status, "
                        "created_at, pay_after, window_start, window_end, revision, result) "
                        "VALUES (?,?,?,?,?,?,?,?,1,?)",
                        (run["id"], run["schedule_id"], run["period"], run["status"], now(),
                         run["pay_after"], run["window_start"], run["window_end"],
                         json.dumps(run["result"])))
    return cur.rowcount == 1


def get_run(run_id: str) -> dict | None:
    with connect() as c:
        row = c.execute("SELECT * FROM payroll_runs WHERE id = ?", (run_id,)).fetchone()
    return _run(row) if row else None


def run_for_period(sid: str, period: str) -> dict | None:
    with connect() as c:
        row = c.execute("SELECT * FROM payroll_runs WHERE schedule_id = ? AND period = ?",
                        (sid, period)).fetchone()
    return _run(row) if row else None


def runs_for_schedule(sid: str) -> list[dict]:
    with connect() as c:
        rows = c.execute("SELECT * FROM payroll_runs WHERE schedule_id = ? ORDER BY period DESC",
                         (sid,)).fetchall()
    return [_run(r) for r in rows]


def update_run(run_id: str, *, expect_status: str | None = None, **fields) -> bool:
    """Conditional update: with expect_status, only applies if the run is still in that state."""
    if "result" in fields:
        fields["result"] = json.dumps(fields["result"])
    cols = ", ".join(f"{k} = ?" for k in fields)
    q, args = f"UPDATE payroll_runs SET {cols} WHERE id = ?", [*fields.values(), run_id]
    if expect_status:
        q += " AND status = ?"
        args.append(expect_status)
    with connect() as c:
        return c.execute(q, args).rowcount == 1


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
