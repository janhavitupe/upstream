"""Storage for everything Upstream remembers.

SQLite (a file) locally and in tests; PostgreSQL when DATABASE_URL is set, e.g. a free Neon
database, so data survives deploys on hosts whose disk is wiped. Queries are written once in
SQLite's dialect and translated for PostgreSQL in _PgConnection. One short-lived connection (or
pooled connection) per call keeps it safe under FastAPI's thread pool.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

from backend import config

SCHEMA = """
-- From the removed dependency-funding module, kept because payout_batches refers to it
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
    sender_item_id   TEXT NOT NULL,      -- the person's GitHub login
    batch_id         TEXT NOT NULL REFERENCES payout_batches(batch_id),
    payout_item_id   TEXT,               -- PayPal's id, known once the batch is processed
    email            TEXT NOT NULL,
    amount           REAL NOT NULL,
    status           TEXT,
    updated_at       TEXT NOT NULL,
    PRIMARY KEY (batch_id, sender_item_id)
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
CREATE TABLE IF NOT EXISTS clients (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,     -- compared case-insensitively in queries
    email       TEXT NOT NULL,
    dont_chase  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS invoices (
    id                TEXT PRIMARY KEY,   -- PayPal invoice id
    number            TEXT,
    client_name       TEXT,
    client_email      TEXT NOT NULL,
    amount            REAL NOT NULL,
    currency          TEXT NOT NULL,
    due_date          TEXT NOT NULL,
    status            TEXT NOT NULL,      -- PayPal status: SENT, UNPAID, PAID, CANCELLED, ...
    payer_url         TEXT,               -- the client's pay page
    items             TEXT NOT NULL,      -- JSON
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    paid_at           TEXT,
    chase_stage       INTEGER NOT NULL DEFAULT 0,  -- reminders sent so far
    last_reminder_at  TEXT,
    last_reminder_day TEXT,               -- by the (demo) clock, for follow-up spacing
    final_day         TEXT,               -- when the final notice went out
    needs_you_at      TEXT,               -- when it was handed to the owner
    viewed            INTEGER,            -- PayPal: has the client opened it
    amount_due        REAL,               -- what's still owed (after partial payments)
    partial_allowed   INTEGER NOT NULL DEFAULT 0,
    written_off       INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS settings (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL                  -- JSON
);
CREATE TABLE IF NOT EXISTS retainers (
    id             TEXT PRIMARY KEY,      -- PayPal subscription id
    client_name    TEXT,
    client_email   TEXT NOT NULL,
    service        TEXT NOT NULL,
    amount         REAL NOT NULL,         -- per month
    currency       TEXT NOT NULL,
    plan_id        TEXT NOT NULL,
    status         TEXT NOT NULL,         -- APPROVAL_PENDING, ACTIVE, SUSPENDED, CANCELLED
    approve_url    TEXT,                  -- the client approves the subscription here
    token          TEXT NOT NULL UNIQUE,  -- the client's renewal page: /r/{token}
    next_billing   TEXT,                  -- YYYY-MM-DD
    last_payment   TEXT,                  -- PayPal time of the last charge seen
    heads_up_for   TEXT,                  -- the renewal date the last heads-up was about
    heads_up_note  TEXT,
    paused_by      TEXT,                  -- "client" or "you"
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS work_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    retainer_id  TEXT NOT NULL REFERENCES retainers(id),
    note         TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS campaigns (
    id              TEXT PRIMARY KEY,
    token           TEXT NOT NULL UNIQUE,   -- public page: /c/{token}
    title           TEXT NOT NULL,
    story           TEXT NOT NULL,
    patient_name    TEXT NOT NULL,          -- who the bill is for (shown only as initials)
    provider_name   TEXT NOT NULL,          -- the hospital as the organizer named it
    provider_email  TEXT NOT NULL,          -- the hospital's PayPal: donations go here
    goal            REAL NOT NULL,
    currency        TEXT NOT NULL,
    bill_path       TEXT,                   -- private file, never served
    bill_hash       TEXT,                   -- SHA-256: the same bill can't back two campaigns
    extraction      TEXT,                   -- JSON the AI read from the bill
    checks          TEXT,                   -- JSON [{key, label, ok, detail}]
    verdict         TEXT NOT NULL,          -- verified | needs_review | unreadable
    status          TEXT NOT NULL,          -- open | closed
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS donations (
    order_id     TEXT PRIMARY KEY,          -- PayPal order id
    campaign_id  TEXT NOT NULL REFERENCES campaigns(id),
    amount       REAL NOT NULL,
    status       TEXT NOT NULL,             -- CREATED | COMPLETED
    created_at   TEXT NOT NULL,
    captured_at  TEXT
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


def using_postgres() -> bool:
    return bool(config.DATABASE_URL)


class _PgConnection:
    """Gives a psycopg connection the sqlite3 interface the queries below use."""

    def __init__(self, conn):
        self.conn = conn

    @staticmethod
    def translate(sql: str) -> str:
        if re.match(r"\s*INSERT OR IGNORE\b", sql, re.I):
            sql = re.sub(r"INSERT OR IGNORE", "INSERT", sql, count=1, flags=re.I) + " ON CONFLICT DO NOTHING"
        return sql.replace("%", "%%").replace("?", "%s")

    def execute(self, sql: str, args=()):
        return self.conn.execute(self.translate(sql), tuple(args))

    def executemany(self, sql: str, rows):
        with self.conn.cursor() as cur:
            cur.executemany(self.translate(sql), [tuple(r) for r in rows])

    def executescript(self, script: str):
        for stmt in script.split(";"):
            if stmt.strip():
                self.conn.execute(_pg_ddl(stmt))

    def rollback(self):
        self.conn.rollback()


def _pg_ddl(sql: str) -> str:
    sql = sql.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY")
    return re.sub(r"\bREAL\b", "DOUBLE PRECISION", sql)  # Postgres REAL is 4 bytes: bad for money


_pool = None
_pool_lock = threading.Lock()


def _pg_pool():
    global _pool
    with _pool_lock:
        if _pool is None or _pool.conninfo != config.DATABASE_URL:
            from psycopg.rows import dict_row
            from psycopg_pool import ConnectionPool
            if _pool is not None:
                _pool.close()
            # check= drops connections a sleeping serverless database (Neon) has closed;
            # prepare_threshold=None: connection poolers (Neon's "-pooler" host, PgBouncer)
            # can't keep server-side prepared statements between transactions
            _pool = ConnectionPool(config.DATABASE_URL, min_size=1, max_size=5, open=True,
                                   kwargs={"row_factory": dict_row, "prepare_threshold": None},
                                   check=ConnectionPool.check_connection, max_idle=300)
        return _pool


@contextmanager
def connect():
    if using_postgres():
        with _pg_pool().connection() as conn:  # commits on success, rolls back on error
            yield _PgConnection(conn)
        return
    conn = sqlite3.connect(config.DATABASE_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# Columns added after the first release; CREATE TABLE IF NOT EXISTS won't add them
NEW_COLUMNS = {"invoices": [("last_reminder_day", "TEXT"), ("final_day", "TEXT"),
                            ("needs_you_at", "TEXT"), ("viewed", "INTEGER"),
                            ("amount_due", "REAL"),
                            ("partial_allowed", "INTEGER NOT NULL DEFAULT 0"),
                            ("written_off", "INTEGER NOT NULL DEFAULT 0")]}


def _columns(c, table: str) -> set[str]:
    if using_postgres():
        rows = c.execute("SELECT column_name AS name FROM information_schema.columns "
                         "WHERE table_name = ?", (table,)).fetchall()
    else:
        rows = c.execute(f"PRAGMA table_info({table})").fetchall()
    return {r["name"] for r in rows}


def _migrate(c):
    for table, cols in NEW_COLUMNS.items():
        have = _columns(c, table)
        for name, decl in cols:
            if name not in have:
                if using_postgres():
                    decl = _pg_ddl(decl)
                c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def init():
    if not using_postgres():
        config.DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with connect() as c:
        if not using_postgres():
            c.execute("PRAGMA journal_mode = WAL")
        c.executescript(SCHEMA)
        _migrate(c)
        for name, email in config.DEMO_CLIENTS.items():  # sandbox clients, so chat works at once
            c.execute("INSERT OR IGNORE INTO clients (name, email, created_at) VALUES (?,?,?)",
                      (name, email, now()))


# ---------- payouts ----------

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


# ---------- clients, invoices, settings ----------

def upsert_client(name: str, email: str) -> dict:
    """Add a client, or update the email of one with the same name (any capitalisation)."""
    with connect() as c:
        row = c.execute("SELECT id FROM clients WHERE lower(name) = lower(?)", (name.strip(),)).fetchone()
        if row:
            c.execute("UPDATE clients SET email = ? WHERE id = ?", (email.strip(), row["id"]))
        else:
            c.execute("INSERT INTO clients (name, email, created_at) VALUES (?,?,?)",
                      (name.strip(), email.strip(), now()))
    return find_client(name)


def find_client(name: str) -> dict | None:
    """Exact name first, then a unique partial match ("acme" -> "Acme Corp")."""
    with connect() as c:
        row = c.execute("SELECT * FROM clients WHERE lower(name) = lower(?)", (name.strip(),)).fetchone()
        if not row:
            rows = c.execute("SELECT * FROM clients WHERE lower(name) LIKE lower(?)",
                             (f"%{name.strip()}%",)).fetchall()
            row = rows[0] if len(rows) == 1 else None
    return dict(row) if row else None


def list_clients() -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM clients ORDER BY name").fetchall()]


def set_client_chase(name: str, dont_chase: bool) -> bool:
    with connect() as c:
        return c.execute("UPDATE clients SET dont_chase = ? WHERE lower(name) = lower(?)",
                         (int(dont_chase), name)).rowcount == 1


def save_invoice(inv: dict):
    ts = now()
    with connect() as c:
        c.execute("INSERT INTO invoices (id, number, client_name, client_email, amount, "
                  "currency, due_date, status, payer_url, items, created_at, updated_at) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                  "number = excluded.number, client_name = excluded.client_name, "
                  "client_email = excluded.client_email, amount = excluded.amount, "
                  "currency = excluded.currency, due_date = excluded.due_date, "
                  "status = excluded.status, payer_url = excluded.payer_url, "
                  "items = excluded.items, updated_at = excluded.updated_at",
                  (inv["id"], inv.get("number"), inv.get("client_name"), inv["client_email"],
                   inv["amount"], inv["currency"], inv["due_date"], inv["status"],
                   inv.get("payer_url"), json.dumps(inv.get("items") or []), ts, ts))


def _invoice(row) -> dict:
    return {**dict(row), "items": json.loads(row["items"])}


def get_invoice(invoice_id: str) -> dict | None:
    with connect() as c:
        row = c.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
    return _invoice(row) if row else None


def list_invoices(statuses: tuple[str, ...] | None = None) -> list[dict]:
    q, args = "SELECT * FROM invoices", ()
    if statuses:
        q += f" WHERE status IN ({','.join('?' * len(statuses))})"
        args = statuses
    with connect() as c:
        return [_invoice(r) for r in c.execute(q + " ORDER BY created_at DESC", args).fetchall()]


def update_invoice(invoice_id: str, **fields):
    fields["updated_at"] = now()
    cols = ", ".join(f"{k} = ?" for k in fields)
    with connect() as c:
        c.execute(f"UPDATE invoices SET {cols} WHERE id = ?", (*fields.values(), invoice_id))


def invoice_reminders(invoice_id: str) -> list[dict]:
    """Every reminder sent for one invoice, oldest first, with its text."""
    with connect() as c:
        rows = c.execute("SELECT received_at, event_type, message, payload FROM events "
                         "WHERE resource_id = ? AND event_type LIKE 'UPSTREAM.%' ORDER BY id",
                         (invoice_id,)).fetchall()
    return [{"at": r["received_at"], "type": r["event_type"], "message": r["message"],
             "note": json.loads(r["payload"]).get("note")} for r in rows]


def get_setting(key: str, default=None):
    with connect() as c:
        row = c.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def set_setting(key: str, value):
    with connect() as c:
        c.execute("INSERT INTO settings (key, value) VALUES (?,?) "
                  "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, json.dumps(value)))


# ---------- retainers ----------

def save_retainer(r: dict):
    ts = now()
    with connect() as c:
        c.execute("INSERT INTO retainers (id, client_name, client_email, service, amount, currency, "
                  "plan_id, status, approve_url, token, next_billing, created_at, updated_at) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (r["id"], r.get("client_name"), r["client_email"], r["service"], r["amount"],
                   r["currency"], r["plan_id"], r["status"], r.get("approve_url"), r["token"],
                   r.get("next_billing"), ts, ts))


def get_retainer(retainer_id: str) -> dict | None:
    with connect() as c:
        row = c.execute("SELECT * FROM retainers WHERE id = ?", (retainer_id,)).fetchone()
    return dict(row) if row else None


def retainer_by_token(token: str) -> dict | None:
    with connect() as c:
        row = c.execute("SELECT * FROM retainers WHERE token = ?", (token,)).fetchone()
    return dict(row) if row else None


def list_retainers(include_cancelled: bool = True) -> list[dict]:
    q = "SELECT * FROM retainers" + ("" if include_cancelled else " WHERE status != 'CANCELLED'")
    with connect() as c:
        return [dict(r) for r in c.execute(q + " ORDER BY created_at DESC").fetchall()]


def update_retainer(retainer_id: str, **fields):
    fields["updated_at"] = now()
    cols = ", ".join(f"{k} = ?" for k in fields)
    with connect() as c:
        c.execute(f"UPDATE retainers SET {cols} WHERE id = ?", (*fields.values(), retainer_id))


def add_work(retainer_id: str, note: str) -> dict:
    """Log delivered work once: the same note for the same retainer isn't added twice."""
    note = note.strip()[:500]
    with connect() as c:
        same = c.execute("SELECT * FROM work_log WHERE retainer_id = ? AND lower(note) = lower(?)",
                         (retainer_id, note)).fetchone()
        if same:
            return {**dict(same), "duplicate": True}
        row = c.execute("INSERT INTO work_log (retainer_id, note, created_at) VALUES (?,?,?) "
                        "RETURNING *", (retainer_id, note, now())).fetchone()
        return dict(row)


def work_after(retainer_id: str, after_id: int) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM work_log WHERE retainer_id = ? AND id > ? ORDER BY id",
            (retainer_id, after_id)).fetchall()]


def work_since(retainer_id: str, since_iso: str | None) -> list[dict]:
    q, args = "SELECT * FROM work_log WHERE retainer_id = ?", [retainer_id]
    if since_iso:
        q += " AND created_at >= ?"
        args.append(since_iso)
    with connect() as c:
        return [dict(r) for r in c.execute(q + " ORDER BY id", args).fetchall()]


# ---------- verified causes (Module 4) ----------

def _campaign(row) -> dict:
    d = dict(row)
    d["extraction"] = json.loads(d["extraction"]) if d["extraction"] else None
    d["checks"] = json.loads(d["checks"]) if d["checks"] else []
    return d


def save_campaign(c: dict):
    ts = now()
    with connect() as con:
        con.execute("INSERT INTO campaigns (id, token, title, story, patient_name, provider_name, "
                    "provider_email, goal, currency, bill_path, bill_hash, extraction, checks, "
                    "verdict, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (c["id"], c["token"], c["title"], c["story"], c["patient_name"],
                     c["provider_name"], c["provider_email"], c["goal"], c["currency"],
                     c.get("bill_path"), c.get("bill_hash"), json.dumps(c.get("extraction")),
                     json.dumps(c.get("checks") or []), c["verdict"], c.get("status", "open"), ts, ts))


def update_campaign(campaign_id: str, **fields):
    for k in ("extraction", "checks"):
        if k in fields:
            fields[k] = json.dumps(fields[k])
    fields["updated_at"] = now()
    cols = ", ".join(f"{k} = ?" for k in fields)
    with connect() as con:
        con.execute(f"UPDATE campaigns SET {cols} WHERE id = ?", (*fields.values(), campaign_id))


def get_campaign(campaign_id: str) -> dict | None:
    with connect() as con:
        row = con.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
    return _campaign(row) if row else None


def campaign_by_token(token: str) -> dict | None:
    with connect() as con:
        row = con.execute("SELECT * FROM campaigns WHERE token = ?", (token,)).fetchone()
    return _campaign(row) if row else None


def list_campaigns() -> list[dict]:
    with connect() as con:
        return [_campaign(r) for r in con.execute(
            "SELECT * FROM campaigns ORDER BY created_at DESC").fetchall()]


def campaign_with_bill(bill_hash: str, exclude_id: str | None = None) -> dict | None:
    with connect() as con:
        row = con.execute("SELECT * FROM campaigns WHERE bill_hash = ? AND id != ?",
                          (bill_hash, exclude_id or "")).fetchone()
    return _campaign(row) if row else None


def add_donation(order_id: str, campaign_id: str, amount: float):
    with connect() as con:
        con.execute("INSERT OR IGNORE INTO donations (order_id, campaign_id, amount, status, "
                    "created_at) VALUES (?,?,?,?,?)", (order_id, campaign_id, amount, "CREATED", now()))


def get_donation(order_id: str) -> dict | None:
    with connect() as con:
        row = con.execute("SELECT * FROM donations WHERE order_id = ?", (order_id,)).fetchone()
    return dict(row) if row else None


def complete_donation(order_id: str) -> bool:
    """True the first time a donation is marked paid (so it's counted once)."""
    with connect() as con:
        return con.execute("UPDATE donations SET status = 'COMPLETED', captured_at = ? "
                           "WHERE order_id = ? AND status != 'COMPLETED'",
                           (now(), order_id)).rowcount == 1


def campaign_raised(campaign_id: str) -> tuple[float, int]:
    with connect() as con:
        row = con.execute("SELECT COALESCE(SUM(amount), 0) AS total, COUNT(*) AS n FROM donations "
                          "WHERE campaign_id = ? AND status = 'COMPLETED'", (campaign_id,)).fetchone()
    return round(row["total"], 2), row["n"]


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


def list_events(limit: int = 200, prefixes: tuple[str, ...] = ()) -> list[dict]:
    """Newest first. With prefixes, only those event types, plus the reminder note if any."""
    q, args = "SELECT received_at, event_type, resource_id, message, verified, payload FROM events", []
    if prefixes:
        q += " WHERE " + " OR ".join("event_type LIKE ?" for _ in prefixes)
        args = [f"{p}%" for p in prefixes]
    with connect() as c:
        rows = c.execute(q + " ORDER BY id DESC LIMIT ?", (*args, limit)).fetchall()
    out = []
    for r in rows:
        e = {k: r[k] for k in ("received_at", "event_type", "resource_id", "message")}
        e["verified"] = None if r["verified"] is None else bool(r["verified"])
        if prefixes:
            payload = json.loads(r["payload"])
            e["note"] = payload.get("note")
        out.append(e)
    return out
