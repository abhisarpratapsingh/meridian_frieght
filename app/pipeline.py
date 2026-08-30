"""The 7-step breakdown-to-resolution pipeline. SQLite is the source of truth
(see db.py); this module only ever transitions a ticket forward through its
state machine and never re-does a step whose result already exists.

State machine per ticket_id:
  RECEIVED -> QUARANTINED (terminal)
  RECEIVED -> VALIDATED -> ENRICHED -> CLASSIFIED
    -> VEHICLE_SELECTED -> WORK_ORDER_CREATED -> COMMS_PENDING -> COMMS_SENT
    -> BLOCKED_NO_ELIGIBLE_VEHICLE (terminal-for-this-run, alerted, not silently dropped)

Re-running the pipeline on a ticket already at any terminal-for-this-run state
is a no-op: no new DB rows, no new timestamps, so re-export is byte-identical.
"""
import hashlib
import json
from datetime import datetime
from pathlib import Path

from app import pii
from app.adapters import load_ticket_records
from app.normalize import canonical_reg, canonical_client, canonical_hub
from app.eligibility import select_replacement
from app.comms_templates import draft_message

BUNDLE_DIR = Path(__file__).resolve().parent.parent
OUTPUTS_DIR = BUNDLE_DIR / "outputs"
AUDIT_DIR = BUNDLE_DIR / "audit"

TERMINAL_STATES = {"QUARANTINED", "COMMS_PENDING", "COMMS_SENT", "BLOCKED_NO_ELIGIBLE_VEHICLE"}

CRITICAL_FIELDS = ["ticket_id", "created_at", "vehicle", "origin_hub", "issue", "client", "km_from_origin_hub"]


def _now():
    return datetime.utcnow().isoformat()


def _content_hash(record: dict) -> str:
    """Hash the CANONICAL fields, not the raw dict. Two records that mean the
    same thing but arrive with different key names (schema drift) must hash
    identically, or every cross-format duplicate would falsely read as a
    content conflict."""
    canonical = {f: record.get(f) for f in CRITICAL_FIELDS + ["driver_id", "destination", "severity", "resolution_note"]}
    return hashlib.sha256(json.dumps(canonical, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _audit(conn, ticket_id, step, decision, rule_ids=None, data_refs=None, actor="pipeline"):
    conn.execute(
        "INSERT INTO audit_events (ticket_id, step, decision, rule_ids_json, data_refs_json, actor, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (ticket_id, step, decision, json.dumps(rule_ids or []), json.dumps(data_refs or []), actor, _now()),
    )


def _parse_date(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", ""))
    except (ValueError, TypeError):
        return None


def _validate(record: dict) -> list:
    """Returns list of reasons the record fails validation. Empty = valid."""
    reasons = []
    for field in CRITICAL_FIELDS:
        v = record.get(field)
        if v in (None, "", "null"):
            reasons.append(f"missing critical field: {field}")
    if record.get("created_at") and _parse_date(record["created_at"]) is None:
        reasons.append(f"unparseable created_at: {record.get('created_at')!r}")
    veh = record.get("vehicle")
    if veh and not canonical_reg(veh):
        reasons.append(f"unparseable vehicle identifier: {veh!r}")
    if record.get("_adapter_notes"):
        for n in record["_adapter_notes"]:
            if n.startswith("missing/blank") or n.startswith("could not parse") or n.startswith("record-level") or n.startswith("record is not"):
                reasons.append(f"adapter: {n}")
    return reasons


def process_ticket(conn, raw_record: dict, source_file: str, occurrence_index: int):
    ticket_id = raw_record.get("ticket_id")
    content_hash = _content_hash(raw_record)

    if not ticket_id:
        # No usable id at all -- synthesize one deterministically from content so
        # reruns of the same broken record don't create duplicate quarantine rows.
        ticket_id = "NOID-" + content_hash[:16]

    existing = conn.execute("SELECT * FROM tickets WHERE ticket_id=?", (ticket_id,)).fetchone()

    if existing is not None:
        # Duplicate occurrence (within a file, across files, or a full rerun).
        is_new_occurrence = conn.execute(
            "SELECT 1 FROM ticket_occurrences WHERE ticket_id=? AND source_file=? AND occurrence_index=?",
            (ticket_id, source_file, occurrence_index),
        ).fetchone() is None
        if is_new_occurrence:
            existing_hash = _content_hash(json.loads(existing["raw_json"])) if existing["raw_json"] else None
            matches = int(existing_hash == content_hash)
            conn.execute(
                "INSERT OR IGNORE INTO ticket_occurrences (ticket_id, source_file, occurrence_index, "
                "content_hash, matches_winner, raw_json, seen_at) VALUES (?,?,?,?,?,?,?)",
                (ticket_id, source_file, occurrence_index, content_hash, matches, json.dumps(raw_record, default=str), _now()),
            )
            decision = "duplicate ticket_id, content matches first occurrence -- P-03 applied, no reprocessing" \
                if matches else \
                "duplicate ticket_id with CONFLICTING content vs first occurrence -- P-03 applied (first wins), conflict recorded for review"
            _audit(conn, ticket_id, "dedup", decision, rule_ids=["P-03"],
                   data_refs=[f"{source_file}#{occurrence_index}"])
        return  # never reprocess an existing ticket

    if existing is None:
        # Brand new ticket_id: validate.
        reasons = _validate(raw_record)
        conn.execute(
            "INSERT INTO ticket_occurrences (ticket_id, source_file, occurrence_index, content_hash, "
            "matches_winner, raw_json, seen_at) VALUES (?,?,?,?,1,?,?)",
            (ticket_id, source_file, occurrence_index, content_hash, json.dumps(raw_record, default=str), _now()),
        )

        if reasons:
            conn.execute(
                "INSERT INTO tickets (ticket_id, dedup_hash, first_seen_source, first_seen_at, state, "
                "quarantine_reason, raw_json, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (ticket_id, content_hash, source_file, _now(), "QUARANTINED",
                 "; ".join(reasons), json.dumps(raw_record, default=str), raw_record.get("created_at")),
            )
            _audit(conn, ticket_id, "validate", f"quarantined: {'; '.join(reasons)}",
                   data_refs=[f"{source_file}#{occurrence_index}"])
            return

        reg = canonical_reg(raw_record["vehicle"])
        client = canonical_client(raw_record["client"])
        hub = canonical_hub(raw_record["origin_hub"])
        conn.execute(
            "INSERT INTO tickets (ticket_id, dedup_hash, first_seen_source, first_seen_at, state, "
            "raw_json, vehicle_raw, reg_canonical, driver_id, origin_hub, km_from_origin_hub, "
            "destination, issue, severity_input, client_raw, client_canonical, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ticket_id, content_hash, source_file, _now(), "VALIDATED", json.dumps(raw_record, default=str),
             raw_record["vehicle"], reg, raw_record.get("driver_id"), hub,
             float(raw_record["km_from_origin_hub"]), raw_record.get("destination"),
             raw_record["issue"], raw_record.get("severity"), raw_record["client"], client,
             raw_record["created_at"]),
        )
        _audit(conn, ticket_id, "validate", "passed validation", data_refs=[f"{source_file}#{occurrence_index}"])

    _advance(conn, ticket_id)


def _advance(conn, ticket_id: str):
    """Push a VALIDATED ticket through enrich -> classify -> select -> work
    order -> draft comms. Each sub-step checks and updates `state` so this is
    safe to call again on a ticket already further along (it's a no-op past
    the current state because the row already reflects that step's outcome)."""
    ticket = dict(conn.execute("SELECT * FROM tickets WHERE ticket_id=?", (ticket_id,)).fetchone())
    if ticket["state"] in TERMINAL_STATES:
        return

    vehicle_exists = conn.execute("SELECT 1 FROM vehicles WHERE reg_canonical=?", (ticket["reg_canonical"],)).fetchone()
    driver = conn.execute("SELECT * FROM drivers WHERE driver_id=?", (ticket["driver_id"],)).fetchone() if ticket["driver_id"] else None
    maint_count = conn.execute("SELECT COUNT(*) c FROM maintenance_events WHERE reg_canonical=?", (ticket["reg_canonical"],)).fetchone()["c"]
    client_row = conn.execute("SELECT * FROM clients WHERE canonical_name=?", (ticket["client_canonical"],)).fetchone()

    if ticket["state"] == "VALIDATED":
        refs = [f"vehicles.reg_canonical={ticket['reg_canonical']}" if vehicle_exists else "vehicle NOT in fleet_master",
                f"drivers.driver_id={ticket['driver_id']}" if driver else "driver unresolved",
                f"maintenance_events: {maint_count} rows", f"clients.canonical_name={ticket['client_canonical']}"]
        _audit(conn, ticket_id, "enrich", "context gathered", data_refs=refs)
        conn.execute("UPDATE tickets SET state='ENRICHED' WHERE ticket_id=?", (ticket_id,))
        ticket["state"] = "ENRICHED"

    if ticket["state"] == "ENRICHED":
        severity = ticket["severity_input"] or "MEDIUM"
        applicable_rules = []
        if ticket["client_canonical"] == "Shakti Cement":
            applicable_rules.append("R-04")
        if ticket["client_canonical"] == "Vertex Retail":
            applicable_rules.append("R-05")
        if ticket["client_canonical"] == "Apex Chemicals":
            applicable_rules.append("R-06")
        if ticket["client_canonical"] == "Orion Pharma":
            applicable_rules.append("R-07")
        month = _parse_date(ticket["created_at"]).month
        if month in (7, 8, 9):
            applicable_rules.append("R-08")
        required_action = f"Replace vehicle, notify client per {applicable_rules or 'standard'} policy."
        conn.execute(
            "INSERT OR REPLACE INTO classifications (ticket_id, severity, required_action, rule_ids_json, "
            "rationale, decided_at) VALUES (?,?,?,?,?,?)",
            (ticket_id, severity, required_action, json.dumps(applicable_rules),
             f"severity taken from ticket field ({'present' if ticket['severity_input'] else 'defaulted to MEDIUM, none supplied'})",
             _now()),
        )
        _audit(conn, ticket_id, "classify", f"severity={severity}, action={required_action}", rule_ids=applicable_rules)
        conn.execute("UPDATE tickets SET state='CLASSIFIED' WHERE ticket_id=?", (ticket_id,))
        ticket["state"] = "CLASSIFIED"

    if ticket["state"] == "CLASSIFIED":
        selection = select_replacement(conn, ticket)
        if selection["vehicle_id"] is None:
            _audit(conn, ticket_id, "select_vehicle",
                   f"BLOCKED: no eligible vehicle found. Rejected {len(selection['rejected'])} candidates.",
                   rule_ids=[t["rule_id"] for t in selection["rule_trace"]],
                   data_refs=[json.dumps(r) for r in selection["rejected"]])
            conn.execute("UPDATE tickets SET state='BLOCKED_NO_ELIGIBLE_VEHICLE' WHERE ticket_id=?", (ticket_id,))
            return
        conn.execute(
            "INSERT OR REPLACE INTO vehicle_reservations (ticket_id, reg_canonical, reserved_at, rule_ids_json, hub_used) "
            "VALUES (?,?,?,?,?)",
            (ticket_id, selection["reg_canonical"], _now(),
             json.dumps([t["rule_id"] for t in selection["rule_trace"]]), selection["hub_used"]),
        )
        _audit(conn, ticket_id, "select_vehicle",
               f"selected {selection['reg_canonical']} from hub {selection['hub_used']}",
               rule_ids=[t["rule_id"] for t in selection["rule_trace"]],
               data_refs=[json.dumps(t) for t in selection["rule_trace"]])
        conn.execute("UPDATE tickets SET state='VEHICLE_SELECTED' WHERE ticket_id=?", (ticket_id,))
        ticket["state"] = "VEHICLE_SELECTED"
        ticket["_selection"] = selection

    if ticket["state"] == "VEHICLE_SELECTED":
        reservation = conn.execute("SELECT * FROM vehicle_reservations WHERE ticket_id=?", (ticket_id,)).fetchone()
        work_order_id = f"WO-{ticket_id}"
        citations = [
            f"tickets.raw_json#{ticket_id}", f"fleet_master.csv#{reservation['reg_canonical']}",
            f"dispatcher_interview.txt (rules: {reservation['rule_ids_json']})",
        ]
        conn.execute(
            "INSERT OR IGNORE INTO work_orders (work_order_id, ticket_id, vehicle_reg, created_at, citations_json) "
            "VALUES (?,?,?,?,?)",
            (work_order_id, ticket_id, reservation["reg_canonical"], _now(), json.dumps(citations)),
        )
        _audit(conn, ticket_id, "work_order", f"created {work_order_id}", data_refs=citations)
        conn.execute("UPDATE tickets SET state='WORK_ORDER_CREATED' WHERE ticket_id=?", (ticket_id,))
        ticket["state"] = "WORK_ORDER_CREATED"

    if ticket["state"] == "WORK_ORDER_CREATED":
        classification = conn.execute("SELECT * FROM classifications WHERE ticket_id=?", (ticket_id,)).fetchone()
        reservation = conn.execute("SELECT * FROM vehicle_reservations WHERE ticket_id=?", (ticket_id,)).fetchone()
        draft = draft_message(ticket, reservation, classification)
        message_id = f"MSG-{ticket_id}"
        citations = [f"work_orders#{ticket_id}", f"classifications#{ticket_id}",
                     f"rules: {classification['rule_ids_json'] if classification else '[]'}"]
        conn.execute(
            "INSERT OR IGNORE INTO comms (message_id, ticket_id, recipient, body, body_deterministic, "
            "polished, status, citations_json, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (message_id, ticket_id, f"{ticket['client_canonical']} coordinator", draft["body"],
             draft["body_deterministic"], int(draft["polished"]), "PENDING", json.dumps(citations), _now()),
        )
        decision = f"drafted {message_id}, awaiting human approval" + (" (LLM-polished, deterministic original retained for comparison)" if draft["polished"] else " (deterministic template)")
        _audit(conn, ticket_id, "draft_comms", decision, data_refs=citations)
        conn.execute("UPDATE tickets SET state='COMMS_PENDING' WHERE ticket_id=?", (ticket_id,))


def ingest_ticket_file(conn, path: Path):
    records = load_ticket_records(path)
    for i, rec in enumerate(records):
        process_ticket(conn, rec, source_file=path.name, occurrence_index=i)
    conn.commit()
    return {"source_file": path.name, "records_seen": len(records)}


def list_candidates_for_ticket(conn, ticket_id: str) -> list:
    """Every active vehicle, with its full rule-by-rule pass/fail status
    against this specific ticket -- used by the manual-override picker for a
    BLOCKED ticket. Shows the same reasoning the automatic selector used, so
    a human overriding it is doing so with full information, not blind."""
    from app.eligibility import _maintenance_history, _apex_recent_hold_reg
    from app.rules import RuleContext, evaluate_eligibility
    from app.normalize import hub_search_order

    ticket = dict(conn.execute("SELECT * FROM tickets WHERE ticket_id=?", (ticket_id,)).fetchone())
    ticket_date = _parse_date(ticket["created_at"])
    reserved = {r["reg_canonical"] for r in conn.execute(
        "SELECT reg_canonical FROM vehicle_reservations WHERE ticket_id != ?", (ticket_id,)
    ).fetchall()}
    apex_holds = _apex_recent_hold_reg(conn, ticket["created_at"], ticket_id) if ticket.get("client_canonical") == "Apex Chemicals" else set()

    km = ticket["km_from_origin_hub"] or 0
    within_50 = km <= 50
    allowed_hubs = {ticket["origin_hub"]} if within_50 else set(hub_search_order(ticket["origin_hub"]))

    out = []
    for v in conn.execute("SELECT * FROM vehicles WHERE status='Active' ORDER BY vehicle_id"):
        v = dict(v)
        reg = v["reg_canonical"]
        blockers = []
        if reg == ticket["reg_canonical"]:
            blockers.append("this is the broken-down vehicle itself")
        if reg in reserved:
            blockers.append("already reserved by another ticket this run")
        if reg in apex_holds:
            blockers.append("R-06 Apex rotation hold")
        ctx = RuleContext(
            ticket_date=ticket_date, origin_hub=ticket["origin_hub"], destination=ticket["destination"],
            client=ticket.get("client_canonical"), km_from_origin=km,
            vehicle=v, maintenance_history=_maintenance_history(conn, reg, ticket_date),
        )
        results = evaluate_eligibility(ctx)
        rule_failures = [f"{r.rule_id}: {r.explanation}" for r in results if not r.passed]
        sourcing_ok = v["home_hub"] in allowed_hubs
        sourcing_note = None if sourcing_ok else (
            f"R-09: outside normal sourcing (within 50km requires the {ticket['origin_hub']} hub specifically)"
            if within_50 else f"R-09: not in the normal nearest-hub search order for {ticket['origin_hub']}"
        )
        out.append({
            "vehicle_id": v["vehicle_id"], "reg": reg, "model": v["model"], "year": v["year"],
            "bs_stage": v["bs_stage"], "home_hub": v["home_hub"],
            "eligible": not blockers and not rule_failures and sourcing_ok,
            "blockers": blockers + rule_failures,
            "sourcing_note": sourcing_note,
        })
    out.sort(key=lambda c: (not c["eligible"], bool(c["sourcing_note"]), c["reg"]))
    return out


def resolve_blocked_manually(conn, ticket_id: str, vehicle_reg: str, operator: str, reason: str):
    """A dispatcher's manual override for a BLOCKED_NO_ELIGIBLE_VEHICLE ticket
    -- exactly the 'a person needs to make the call' case the automatic
    selector already told the operator about. Reuses the same downstream
    steps (_advance) the automatic path uses, so a manually-resolved ticket
    still gets a work order, a drafted client message, and a full audit
    trail -- it just starts from a human decision instead of a rule match."""
    ticket = conn.execute("SELECT * FROM tickets WHERE ticket_id=?", (ticket_id,)).fetchone()
    if ticket is None:
        return False, "no such ticket"
    if ticket["state"] != "BLOCKED_NO_ELIGIBLE_VEHICLE":
        return False, f"ticket is not in a blocked state (current state: {ticket['state']})"

    reg = canonical_reg(vehicle_reg)
    if reg == ticket["reg_canonical"]:
        return False, "cannot assign the broken-down vehicle as its own replacement -- this isn't a policy override, it's not physically possible"
    vehicle = conn.execute("SELECT * FROM vehicles WHERE reg_canonical=?", (reg,)).fetchone()
    if vehicle is None:
        return False, f"no such vehicle: {vehicle_reg}"
    already_reserved = conn.execute(
        "SELECT 1 FROM vehicle_reservations WHERE reg_canonical=? AND ticket_id != ?", (reg, ticket_id)
    ).fetchone()
    if already_reserved:
        return False, f"{reg} is already reserved by another ticket this run"
    if not reason or not reason.strip():
        return False, "a reason is required for a manual override -- this becomes part of the permanent audit trail"

    conn.execute(
        "INSERT OR REPLACE INTO vehicle_reservations (ticket_id, reg_canonical, reserved_at, rule_ids_json, hub_used) "
        "VALUES (?,?,?,?,?)",
        (ticket_id, reg, _now(), json.dumps(["MANUAL_OVERRIDE"]), vehicle["home_hub"]),
    )
    _audit(conn, ticket_id, "select_vehicle",
           f"MANUAL OVERRIDE by {operator}: selected {reg}. Reason: {reason.strip()}",
           rule_ids=["MANUAL_OVERRIDE"], actor=operator)
    conn.execute("UPDATE tickets SET state='VEHICLE_SELECTED' WHERE ticket_id=?", (ticket_id,))
    conn.commit()
    _advance(conn, ticket_id)
    conn.commit()
    return True, f"{reg} assigned, work order and draft message created"


def resolve_quarantine(conn, ticket_id: str, corrected_fields: dict, operator: str):
    """A human fixes the missing/broken fields on a QUARANTINED record and
    resubmits it. Re-validated with the exact same rules a fresh ticket
    would face -- a correction that still doesn't pass stays quarantined,
    it is never force-accepted."""
    ticket = conn.execute("SELECT * FROM tickets WHERE ticket_id=?", (ticket_id,)).fetchone()
    if ticket is None:
        return False, "no such ticket"
    if ticket["state"] != "QUARANTINED":
        return False, f"ticket is not quarantined (current state: {ticket['state']})"

    original = json.loads(ticket["raw_json"]) if ticket["raw_json"] else {}
    merged = {**original, **{k: v for k, v in corrected_fields.items() if v not in (None, "")}}
    merged["ticket_id"] = ticket_id  # never allow the correction to change identity
    # These describe the ORIGINAL broken record (which fields the ingestion
    # adapter couldn't map/found blank) -- stale diagnostics from before the
    # correction, not a property of the corrected record. Carrying them
    # forward would make _validate() reject a record that's now actually fine.
    merged.pop("_adapter_notes", None)
    merged.pop("_unmapped", None)

    reasons = _validate(merged)
    if reasons:
        return False, "still invalid after correction: " + "; ".join(reasons)

    reg = canonical_reg(merged["vehicle"])
    client = canonical_client(merged["client"])
    hub = canonical_hub(merged["origin_hub"])
    conn.execute(
        "UPDATE tickets SET state='VALIDATED', quarantine_reason=NULL, raw_json=?, vehicle_raw=?, "
        "reg_canonical=?, driver_id=?, origin_hub=?, km_from_origin_hub=?, destination=?, issue=?, "
        "severity_input=?, client_raw=?, client_canonical=?, created_at=? WHERE ticket_id=?",
        (json.dumps(merged, default=str), merged["vehicle"], reg, merged.get("driver_id"), hub,
         float(merged["km_from_origin_hub"]), merged.get("destination"), merged["issue"],
         merged.get("severity"), merged["client"], client, merged["created_at"], ticket_id),
    )
    _audit(conn, ticket_id, "validate",
           f"corrected and resubmitted by {operator} (previously quarantined: {ticket['quarantine_reason']})",
           actor=operator)
    conn.commit()
    _advance(conn, ticket_id)
    conn.commit()
    return True, "corrected, validated, and pushed through the pipeline"


def approve_comm(conn, ticket_id: str, approved_by: str):
    row = conn.execute("SELECT * FROM comms WHERE ticket_id=?", (ticket_id,)).fetchone()
    if row is None:
        return False, "no drafted comm for this ticket"
    if row["status"] == "SENT":
        return True, "already sent (idempotent no-op)"
    conn.execute(
        "UPDATE comms SET status='SENT', approved_by=?, sent_at=? WHERE ticket_id=?",
        (approved_by, _now(), ticket_id),
    )
    _audit(conn, ticket_id, "comms_sent", f"approved by {approved_by}", actor=approved_by)
    conn.execute("UPDATE tickets SET state='COMMS_SENT' WHERE ticket_id=?", (ticket_id,))
    conn.commit()
    return True, "sent"


# ---------- deterministic export: outbox files are materialized views, not appended-to logs ----------

def _atomic_write_jsonl(path: Path, rows: list):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
    tmp.replace(path)  # atomic on POSIX and Windows (same filesystem)


def export_outputs(conn):
    work_orders = [dict(r) for r in conn.execute("SELECT * FROM work_orders ORDER BY ticket_id")]
    for r in work_orders:
        r["citations"] = json.loads(r.pop("citations_json"))
    _atomic_write_jsonl(OUTPUTS_DIR / "work_orders.jsonl", work_orders)

    pending = [dict(r) for r in conn.execute("SELECT * FROM comms WHERE status='PENDING' ORDER BY ticket_id")]
    for r in pending:
        r["citations"] = json.loads(r.pop("citations_json"))
    _atomic_write_jsonl(OUTPUTS_DIR / "comms_pending.jsonl", pending)

    sent = []
    for r in conn.execute("SELECT * FROM comms WHERE status='SENT' ORDER BY ticket_id"):
        sent.append({
            "message_id": r["message_id"], "ticket_id": r["ticket_id"], "recipient": r["recipient"],
            "body": r["body"], "approved_by": r["approved_by"], "sent_at": r["sent_at"],
        })
    _atomic_write_jsonl(OUTPUTS_DIR / "comms_sent.jsonl", sent)

    quarantine = []
    for r in conn.execute("SELECT * FROM tickets WHERE state='QUARANTINED' ORDER BY ticket_id"):
        quarantine.append({
            "ticket_id": r["ticket_id"], "reason": r["quarantine_reason"],
            "source_file": r["first_seen_source"], "first_seen_at": r["first_seen_at"],
        })
    _atomic_write_jsonl(OUTPUTS_DIR / "quarantine.jsonl", quarantine)

    blocked = []
    for r in conn.execute("SELECT * FROM tickets WHERE state='BLOCKED_NO_ELIGIBLE_VEHICLE' ORDER BY ticket_id"):
        blocked.append({"ticket_id": r["ticket_id"], "reason": "no eligible replacement vehicle found",
                         "source_file": r["first_seen_source"]})
    _atomic_write_jsonl(OUTPUTS_DIR / "blocked_no_vehicle.jsonl", blocked)  # extra, not in the required list, kept separate so it never gets mistaken for a silent drop

    audit = []
    for r in conn.execute("SELECT * FROM audit_events ORDER BY id"):
        audit.append({
            "ticket_id": r["ticket_id"], "step": r["step"], "decision": r["decision"],
            "rule_ids": json.loads(r["rule_ids_json"]), "data_refs": json.loads(r["data_refs_json"]),
            "actor": r["actor"], "created_at": r["created_at"],
        })
    _atomic_write_jsonl(AUDIT_DIR / "audit.jsonl", audit)

    return {
        "work_orders": len(work_orders), "comms_pending": len(pending), "comms_sent": len(sent),
        "quarantine": len(quarantine), "blocked_no_vehicle": len(blocked), "audit_events": len(audit),
    }
