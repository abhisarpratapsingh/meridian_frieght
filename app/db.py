"""SQLite is the single source of truth. All outbox files (work_orders.jsonl,
comms_pending.jsonl, comms_sent.jsonl, quarantine.jsonl, audit.jsonl) are
deterministic materialized views exported FROM this database, never hand-appended.

Why this matters for the "run twice, identical output" rule: a rerun that finds a
ticket already at a terminal state does zero writes (no new rows, no new
timestamps). Since the export step renders existing rows in a stable sort order,
re-exporting after a no-op run produces byte-identical files.
"""
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "state" / "meridian.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS vehicles (
    vehicle_id TEXT PRIMARY KEY,
    reg_canonical TEXT UNIQUE NOT NULL,
    model TEXT, year INTEGER, bs_stage TEXT, engine_heater TEXT,
    home_hub TEXT, capacity_tonnes REAL, status TEXT,
    id_synthesized INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS vehicle_aliases (
    alias_raw TEXT PRIMARY KEY,
    reg_canonical TEXT NOT NULL,
    source TEXT
);

CREATE TABLE IF NOT EXISTS drivers (
    driver_id TEXT PRIMARY KEY,
    name TEXT, phone_hash TEXT, dl_hash TEXT, aadhaar_hash TEXT,
    joining_date TEXT, home_hub TEXT
);

CREATE TABLE IF NOT EXISTS clients (
    client_id TEXT PRIMARY KEY,
    canonical_name TEXT UNIQUE,
    sla_hours_effective REAL,
    sla_hours_contract REAL,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS maintenance_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reg_canonical TEXT, event_date TEXT, odometer_km INTEGER,
    mechanic TEXT, notes TEXT,
    is_brake_work INTEGER DEFAULT 0,
    is_jugaad INTEGER DEFAULT 0
);

-- Fact provenance: every resolved attribute with its source and precedence.
-- Conflicts are resolved by precedence_rank (lower = wins) and the losing
-- fact is kept, not deleted, so a conflict can be explained, not just applied.
CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT, entity_id TEXT, attribute TEXT, value TEXT,
    source_file TEXT, source_ref TEXT, observed_at TEXT,
    precedence_rank INTEGER, ingested_at TEXT,
    is_winner INTEGER DEFAULT 1, note TEXT
);

CREATE TABLE IF NOT EXISTS rules (
    rule_id TEXT PRIMARY KEY, category TEXT, statement TEXT,
    source_citation TEXT, precedence INTEGER
);

CREATE TABLE IF NOT EXISTS tickets (
    ticket_id TEXT PRIMARY KEY,
    dedup_hash TEXT,
    first_seen_source TEXT, first_seen_at TEXT,
    state TEXT NOT NULL,
    quarantine_reason TEXT,
    raw_json TEXT,
    vehicle_raw TEXT, reg_canonical TEXT, driver_id TEXT,
    origin_hub TEXT, km_from_origin_hub REAL, destination TEXT,
    issue TEXT, severity_input TEXT,
    client_raw TEXT, client_canonical TEXT,
    created_at TEXT
);

-- Every time a ticket_id is seen again (dup within a file, or across files
-- including the surprise file), we log the occurrence without touching the
-- winning ticket row. This is what lets us prove "processed exactly once"
-- while still being honest that N occurrences existed.
CREATE TABLE IF NOT EXISTS ticket_occurrences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id TEXT, source_file TEXT, occurrence_index INTEGER,
    content_hash TEXT, matches_winner INTEGER, raw_json TEXT, seen_at TEXT,
    UNIQUE(ticket_id, source_file, occurrence_index)
);

CREATE TABLE IF NOT EXISTS classifications (
    ticket_id TEXT PRIMARY KEY,
    severity TEXT, required_action TEXT,
    rule_ids_json TEXT, rationale TEXT, decided_at TEXT
);

-- Reservation prevents two tickets in the same run from both "eligibly"
-- claiming the same replacement vehicle.
CREATE TABLE IF NOT EXISTS vehicle_reservations (
    reservation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id TEXT UNIQUE NOT NULL,
    reg_canonical TEXT NOT NULL,
    reserved_at TEXT, rule_ids_json TEXT, hub_used TEXT
);

CREATE TABLE IF NOT EXISTS work_orders (
    work_order_id TEXT PRIMARY KEY,
    ticket_id TEXT UNIQUE NOT NULL,
    vehicle_reg TEXT, created_at TEXT, citations_json TEXT
);

CREATE TABLE IF NOT EXISTS comms (
    message_id TEXT PRIMARY KEY,
    ticket_id TEXT UNIQUE NOT NULL,
    recipient TEXT, body TEXT, body_deterministic TEXT, polished INTEGER DEFAULT 0,
    status TEXT, approved_by TEXT, sent_at TEXT, citations_json TEXT, created_at TEXT
);

CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id TEXT, step TEXT, decision TEXT,
    rule_ids_json TEXT, data_refs_json TEXT, actor TEXT, created_at TEXT
);

CREATE TABLE IF NOT EXISTS run_meta (
    key TEXT PRIMARY KEY, value TEXT
);

-- Tracks every ticket file `watch` mode has already ingested (by filename +
-- content hash), so a file that reappears -- or is re-read on watcher
-- restart -- is not treated as new. This is the real answer to "what
-- happens when new tickets come in unattended": not a demo, a loop that
-- notices a new file on disk and runs the same pipeline the CLI runs.
CREATE TABLE IF NOT EXISTS watched_files (
    filename TEXT PRIMARY KEY, content_hash TEXT, first_seen_at TEXT, processed_at TEXT
);

-- Free-text corpus for Part A retrieval (dispatcher transcript + 40 email
-- threads). Text is PII-redacted before it ever reaches this table -- see
-- app/pii.py:redact(), applied at ingestion in app/ingest.py.
CREATE TABLE IF NOT EXISTS documents (
    doc_id TEXT PRIMARY KEY,
    source_file TEXT, doc_type TEXT,
    passage_index INTEGER, text_redacted TEXT
);

-- meridian_trips.csv is entirely Sep-Oct 2018 (10,000 rows), disjoint from the
-- 2026 ticket queue -- see EVIDENCE.md "trips.csv scope decision". It cannot
-- inform live "already assigned" or "current location" checks for 2026
-- breakdowns, so we ingest it only as a per-vehicle/per-client rollup for
-- aggregate Q&A, not as 10k raw rows.
CREATE TABLE IF NOT EXISTS trip_summary (
    scope TEXT, key TEXT, trip_count INTEGER, total_billed REAL,
    first_trip_date TEXT, last_trip_date TEXT,
    PRIMARY KEY (scope, key)
);

CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
    doc_id, source_file, text_redacted
);

CREATE INDEX IF NOT EXISTS idx_facts_entity ON facts(entity_type, entity_id, attribute);
CREATE INDEX IF NOT EXISTS idx_audit_ticket ON audit_events(ticket_id);
CREATE INDEX IF NOT EXISTS idx_maint_reg ON maintenance_events(reg_canonical);
"""


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def reset_db() -> None:
    """Delete the DB file entirely. Used only by explicit `cli.py reset`,
    never by the normal pipeline path."""
    if DB_PATH.exists():
        DB_PATH.unlink()
    init_db()
