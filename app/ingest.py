"""Ingestion: every source file -> entity-resolved SQLite rows, PII masked
before storage. Idempotent — safe to run repeatedly (uses INSERT OR REPLACE /
DELETE+reinsert per source, keyed by stable natural keys, never autoincrement
accumulation across runs).
"""
import csv
import json
import re
from datetime import datetime
from pathlib import Path

import openpyxl

from app import pii
from app.normalize import canonical_reg, canonical_client, canonical_hub
from app.rules import RULES

BUNDLE_DIR = Path(__file__).resolve().parent.parent
NOW = lambda: datetime.utcnow().isoformat()


def _emit_fact(conn, entity_type, entity_id, attribute, value, source_file, source_ref,
               observed_at=None, precedence_rank=5, note=None, is_winner=1):
    conn.execute(
        "INSERT INTO facts (entity_type, entity_id, attribute, value, source_file, source_ref, "
        "observed_at, precedence_rank, ingested_at, is_winner, note) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (entity_type, entity_id, attribute, str(value), source_file, source_ref,
         observed_at, precedence_rank, NOW(), is_winner, note),
    )


def load_rules(conn):
    for r in RULES:
        conn.execute(
            "INSERT OR REPLACE INTO rules (rule_id, category, statement, source_citation, precedence) "
            "VALUES (?,?,?,?,?)",
            (r["rule_id"], r["category"], r["statement"], r["source_citation"], r["precedence"]),
        )


def load_fleet(conn) -> dict:
    """Returns entity_resolution_report dict for fleet_master.csv."""
    path = BUNDLE_DIR / "fleet_master.csv"
    rows = list(csv.DictReader(open(path, encoding="utf-8")))

    # Group by canonical registration -- this is the entity resolution step.
    groups = {}
    for i, row in enumerate(rows):
        reg = canonical_reg(row["registration_number"])
        if reg is None:
            continue
        groups.setdefault(reg, []).append((i, row))

    conn.execute("DELETE FROM vehicles")
    conn.execute("DELETE FROM vehicle_aliases")
    conn.execute("DELETE FROM facts WHERE entity_type='vehicle'")

    synthesized = 0
    merged_dupes = 0
    for reg, entries in groups.items():
        # Prefer the row that already carries a vehicle_id; fall back to a
        # synthetic one so every vehicle is queryable even when the source
        # data is incomplete (never silently drop a vehicle).
        with_id = [(i, r) for i, r in entries if r.get("vehicle_id") and r["vehicle_id"].strip()]
        chosen_i, chosen = (with_id[0] if with_id else entries[0])
        vehicle_id = chosen.get("vehicle_id") or f"VEH-{reg}"
        if not chosen.get("vehicle_id"):
            synthesized += 1
        if len(entries) > 1:
            merged_dupes += 1

        capacity = chosen.get("capacity_tonnes")
        conn.execute(
            "INSERT INTO vehicles (vehicle_id, reg_canonical, model, year, bs_stage, engine_heater, "
            "home_hub, capacity_tonnes, status, id_synthesized) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (vehicle_id, reg, chosen.get("model"),
             int(chosen["year"]) if chosen.get("year") else None,
             chosen.get("bs_stage"),
             chosen.get("engine_heater") or "No",
             canonical_hub(chosen.get("home_hub")),
             float(capacity) if capacity else None,
             chosen.get("status") or "Active",
             0 if with_id else 1),
        )
        for i, row in entries:
            conn.execute(
                "INSERT OR REPLACE INTO vehicle_aliases (alias_raw, reg_canonical, source) VALUES (?,?,?)",
                (row["registration_number"], reg, "fleet_master.csv"),
            )
            for attr in ("model", "year", "bs_stage", "engine_heater", "home_hub", "capacity_tonnes", "status"):
                if row.get(attr):
                    _emit_fact(conn, "vehicle", vehicle_id, attr, row[attr], "fleet_master.csv",
                               f"row {i+2}", precedence_rank=1,
                               is_winner=1 if (i, row) == (chosen_i, chosen) else 0,
                               note=None if (i, row) == (chosen_i, chosen) else
                               f"duplicate row for same physical vehicle (canonical reg {reg}); superseded by row {chosen_i+2} (P-01 fleet_master is system of record for its own duplicates: first row carrying a vehicle_id wins)")

    return {
        "raw_rows": len(rows),
        "unique_vehicles_resolved": len(groups),
        "rows_synthesized_id": synthesized,
        "duplicate_row_groups_merged": merged_dupes,
    }


def _is_brake_work(notes: str) -> bool:
    return bool(re.search(r"brake", notes or "", re.IGNORECASE))


def _is_jugaad(notes: str) -> bool:
    return bool(re.search(r"jugaad", notes or "", re.IGNORECASE))


def load_maintenance(conn) -> dict:
    path = BUNDLE_DIR / "maintenance_log.xlsx"
    wb = openpyxl.load_workbook(path, read_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))[1:]  # skip header

    conn.execute("DELETE FROM maintenance_events")
    count = 0
    unmatched = set()
    redaction_counts = {}
    for date, vehicle_raw, odometer, mechanic, notes in rows:
        if vehicle_raw is None:
            continue
        reg = canonical_reg(vehicle_raw)
        exists = conn.execute("SELECT 1 FROM vehicles WHERE reg_canonical=?", (reg,)).fetchone()
        if not exists:
            unmatched.add(reg)
        notes_redacted, hits = pii.redact_and_count(str(notes) if notes else "")
        for k, v in hits.items():
            redaction_counts[k] = redaction_counts.get(k, 0) + v
        event_date = str(date)[:10] if date else None
        conn.execute(
            "INSERT INTO maintenance_events (reg_canonical, event_date, odometer_km, mechanic, notes, "
            "is_brake_work, is_jugaad) VALUES (?,?,?,?,?,?,?)",
            (reg, event_date, odometer, mechanic, notes_redacted,
             int(_is_brake_work(notes)), int(_is_jugaad(notes))),
        )
        count += 1
    _record_redactions(conn, "maintenance_log.xlsx", redaction_counts)
    return {"maintenance_rows": count, "vehicles_in_log_not_in_fleet_master": len(unmatched),
            "pii_redacted": redaction_counts}


def load_drivers(conn) -> dict:
    path = BUNDLE_DIR / "drivers_roster.csv"
    rows = list(csv.DictReader(open(path, encoding="utf-8")))
    conn.execute("DELETE FROM drivers")
    for row in rows:
        conn.execute(
            "INSERT OR REPLACE INTO drivers (driver_id, name, phone_hash, dl_hash, aadhaar_hash, "
            "joining_date, home_hub) VALUES (?,?,?,?,?,?,?)",
            (row["driver_id"], row["name"], pii.hash_value(row.get("phone")),
             pii.hash_value(row.get("dl_number")), pii.hash_value(row.get("aadhaar")),
             row.get("joining_date"), canonical_hub(row.get("home_hub"))),
        )
    return {"driver_rows": len(rows)}


def load_clients(conn) -> dict:
    seed = {
        "Shakti Cement": (36.0, 48.0, "Effective SLA 36h overrides 48h contract text (R-04)."),
        "Vertex Retail": (None, None, "Ludhiana warehouse gate closes 18:00; late arrival -> scheduled next-morning delivery, never 'failed' (R-05)."),
        "Apex Chemicals": (None, None, "Rotate vehicle after any issue on an Apex run; not sent back next dispatch (R-06)."),
        "Orion Pharma": (None, None, "2020+ vehicles only; never unrefrigerated overnight at a hub (R-07)."),
        "Internal": (None, None, "Internal Meridian moves; no external client SLA."),
    }
    conn.execute("DELETE FROM clients")
    for name, (eff, contract, notes) in seed.items():
        conn.execute(
            "INSERT OR REPLACE INTO clients (client_id, canonical_name, sla_hours_effective, "
            "sla_hours_contract, notes) VALUES (?,?,?,?,?)",
            (name.upper().replace(" ", "_"), name, eff, contract, notes),
        )
    return {"clients": len(seed)}


def load_documents(conn) -> dict:
    """Dispatcher transcript + all 40 email threads, PII-redacted, chunked by
    paragraph for citation granularity."""
    conn.execute("DELETE FROM documents")
    conn.execute("DELETE FROM documents_fts")
    count = 0

    redaction_counts = {}

    def chunk_and_store(text, source_file, doc_type):
        nonlocal count
        redacted, hits = pii.redact_and_count(text)
        for k, v in hits.items():
            redaction_counts[k] = redaction_counts.get(k, 0) + v
        paragraphs = [p for p in re.split(r"\n\s*\n", redacted) if p.strip()]
        for idx, para in enumerate(paragraphs):
            doc_id = f"{source_file}#p{idx}"
            conn.execute(
                "INSERT OR REPLACE INTO documents (doc_id, source_file, doc_type, passage_index, text_redacted) "
                "VALUES (?,?,?,?,?)",
                (doc_id, source_file, doc_type, idx, para.strip()),
            )
            conn.execute(
                "INSERT INTO documents_fts (doc_id, source_file, text_redacted) VALUES (?,?,?)",
                (doc_id, source_file, para.strip()),
            )
            count += 1

    transcript_path = BUNDLE_DIR / "dispatcher_interview.txt"
    chunk_and_store(transcript_path.read_text(encoding="utf-8"), "dispatcher_interview.txt", "transcript")

    email_dir = BUNDLE_DIR / "emails"
    n_emails = 0
    for f in sorted(email_dir.glob("*.txt")):
        chunk_and_store(f.read_text(encoding="utf-8"), f"emails/{f.name}", "email")
        n_emails += 1

    _record_redactions(conn, "dispatcher_interview.txt + emails/*.txt", redaction_counts)
    return {"documents_indexed": count, "email_threads": n_emails, "pii_redacted": redaction_counts}


def _record_redactions(conn, source_label, counts: dict):
    """Append to the running ingestion-time PII redaction audit in run_meta.
    This is the upstream half of the PII story: the output scanner (pii.py
    scan_paths, run as a shipping gate) proves nothing leaked OUT; this proves
    something real was actually caught and removed on the way IN, so a zero
    finding at output time reads as 'redaction worked', not 'there was
    nothing to redact in the first place'."""
    existing_raw = conn.execute("SELECT value FROM run_meta WHERE key='pii_redaction_audit'").fetchone()
    audit = json.loads(existing_raw["value"]) if existing_raw else []
    total = sum(counts.values())
    if total:
        audit.append({"source": source_label, "counts": counts, "total": total})
    conn.execute("INSERT OR REPLACE INTO run_meta (key, value) VALUES ('pii_redaction_audit', ?)",
                 (json.dumps(audit),))


def load_trip_summary(conn) -> dict:
    """Rollup only -- see db.py note on why we don't ingest 10k raw rows."""
    import pandas as pd
    path = BUNDLE_DIR / "meridian_trips.csv"
    df = pd.read_csv(path)
    conn.execute("DELETE FROM trip_summary")

    by_client = df.groupby("client").agg(
        trip_count=("trip_id", "count"), total_billed=("billed_amount", "sum"),
        first_trip_date=("created_at", "min"), last_trip_date=("created_at", "max"),
    ).reset_index()
    for _, r in by_client.iterrows():
        conn.execute(
            "INSERT OR REPLACE INTO trip_summary VALUES ('client', ?, ?, ?, ?, ?)",
            (r["client"], int(r["trip_count"]), float(r["total_billed"]), str(r["first_trip_date"]), str(r["last_trip_date"])),
        )

    df["reg_canonical"] = df["vehicle_reg"].apply(canonical_reg)
    by_vehicle = df.groupby("reg_canonical").agg(
        trip_count=("trip_id", "count"), total_billed=("billed_amount", "sum"),
        first_trip_date=("created_at", "min"), last_trip_date=("created_at", "max"),
    ).reset_index()
    for _, r in by_vehicle.iterrows():
        conn.execute(
            "INSERT OR REPLACE INTO trip_summary VALUES ('vehicle', ?, ?, ?, ?, ?)",
            (r["reg_canonical"], int(r["trip_count"]), float(r["total_billed"]), str(r["first_trip_date"]), str(r["last_trip_date"])),
        )

    return {"trip_rows_processed": len(df), "clients_summarized": len(by_client), "vehicles_summarized": len(by_vehicle)}


def run_full_ingestion(conn) -> dict:
    report = {}
    conn.execute("DELETE FROM run_meta WHERE key='pii_redaction_audit'")  # reingestion should reset the audit, not accumulate across reruns
    load_rules(conn)
    report["fleet"] = load_fleet(conn)
    report["maintenance"] = load_maintenance(conn)
    report["drivers"] = load_drivers(conn)
    report["clients"] = load_clients(conn)
    report["documents"] = load_documents(conn)
    report["trips"] = load_trip_summary(conn)
    conn.commit()
    return report
