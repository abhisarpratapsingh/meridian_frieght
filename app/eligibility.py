"""Replacement-vehicle selection. This is where R-09 (origin-hub-within-50km
overrides "nearest hub") actually bites, and where every rejected candidate
gets a logged reason -- the selection is provably not a coin flip.
"""
from datetime import datetime

from app.normalize import hub_search_order, canonical_reg
from app.rules import RuleContext, evaluate_eligibility, eligibility_passed


def _maintenance_history(conn, reg_canonical, as_of: datetime):
    """Only events on or before `as_of` (the ticket's own created_at) count.
    A vehicle's eligibility for a ticket must be judged on what was knowable
    at that point in time -- using a LATER maintenance record (e.g. a jugaad
    patch that happens after this ticket) to decide this ticket's eligibility
    is temporal data leakage, and produced nonsensical output like "under
    jugaad hold, -81 days of 7" before this filter was added."""
    rows = conn.execute(
        "SELECT event_date, is_brake_work, is_jugaad, notes FROM maintenance_events WHERE reg_canonical=?",
        (reg_canonical,),
    ).fetchall()
    out = []
    for r in rows:
        if not r["event_date"]:
            continue
        try:
            d = datetime.fromisoformat(r["event_date"])
        except ValueError:
            continue
        if d > as_of:
            continue
        out.append({"event_date": d, "is_brake_work": bool(r["is_brake_work"]),
                     "is_jugaad": bool(r["is_jugaad"]), "notes": r["notes"]})
    return out


def _apex_recent_hold_reg(conn, before_ticket_created_at, before_ticket_id):
    """R-06: the vehicle on the immediately-preceding Apex-client ticket
    (either the vehicle that broke down, or the replacement it got) is on a
    one-rotation hold and must not be picked again for the next Apex ticket.

    Scope note (see EVIDENCE.md): we only observe Apex activity through the
    breakdown-ticket stream, not the full dispatch schedule, so "next
    dispatch" is approximated as "next Apex breakdown ticket in this queue".
    """
    row = conn.execute(
        "SELECT t.reg_canonical AS broken_reg, w.vehicle_reg AS repl_reg "
        "FROM tickets t LEFT JOIN work_orders w ON w.ticket_id = t.ticket_id "
        "WHERE t.client_canonical='Apex Chemicals' AND t.created_at < ? AND t.ticket_id != ? "
        "AND t.state IN ('WORK_ORDER_CREATED','COMMS_PENDING','COMMS_SENT') "
        "ORDER BY t.created_at DESC LIMIT 1",
        (before_ticket_created_at, before_ticket_id),
    ).fetchone()
    if not row:
        return set()
    return {r for r in (row["broken_reg"], row["repl_reg"]) if r}


def select_replacement(conn, ticket: dict):
    """Returns dict: {vehicle_id, reg_canonical, hub_used, rule_trace, rejected}
    or None-vehicle variant with rejected candidates listed if nothing qualifies."""
    ticket_date = datetime.fromisoformat(ticket["created_at"])
    km = ticket["km_from_origin_hub"] or 0
    origin_hub = ticket["origin_hub"]
    broken_reg = ticket.get("reg_canonical")

    within_50 = km <= 50
    if within_50:
        hubs = [origin_hub]
        sourcing_note = f"km_from_origin_hub={km} <= 50: R-09 restricts sourcing to origin hub only ({origin_hub})."
    else:
        hubs = hub_search_order(origin_hub)
        sourcing_note = f"km_from_origin_hub={km} > 50: R-09 allows nearest-hub search, order={hubs}."

    apex_holds = _apex_recent_hold_reg(conn, ticket["created_at"], ticket["ticket_id"]) if ticket.get("client_canonical") == "Apex Chemicals" else set()

    reserved = {r["reg_canonical"] for r in conn.execute(
        "SELECT reg_canonical FROM vehicle_reservations WHERE ticket_id != ?", (ticket["ticket_id"],)
    ).fetchall()}

    rejected = []
    for hub in hubs:
        candidates = conn.execute(
            "SELECT * FROM vehicles WHERE home_hub=? AND status='Active' ORDER BY vehicle_id", (hub,)
        ).fetchall()
        for v in candidates:
            reg = v["reg_canonical"]
            if reg == broken_reg:
                rejected.append({"vehicle_id": v["vehicle_id"], "reg": reg, "reason": "is the broken-down vehicle itself"})
                continue
            if reg in reserved:
                rejected.append({"vehicle_id": v["vehicle_id"], "reg": reg, "reason": "already reserved by another ticket this run"})
                continue
            if reg in apex_holds:
                rejected.append({"vehicle_id": v["vehicle_id"], "reg": reg, "reason": "R-06 Apex rotation hold: used on the immediately preceding Apex ticket"})
                continue

            ctx = RuleContext(
                ticket_date=ticket_date, origin_hub=origin_hub, destination=ticket["destination"],
                client=ticket.get("client_canonical"), km_from_origin=km,
                vehicle=dict(v), maintenance_history=_maintenance_history(conn, reg, ticket_date),
            )
            results = evaluate_eligibility(ctx)
            if eligibility_passed(results):
                return {
                    "vehicle_id": v["vehicle_id"], "reg_canonical": reg, "hub_used": hub,
                    "rule_trace": [{"rule_id": "R-09", "passed": True, "explanation": sourcing_note}] +
                                  [{"rule_id": r.rule_id, "passed": r.passed, "explanation": r.explanation} for r in results],
                    "rejected": rejected,
                }
            else:
                failed = [r for r in results if not r.passed]
                rejected.append({"vehicle_id": v["vehicle_id"], "reg": reg,
                                  "reason": "; ".join(f"{r.rule_id}: {r.explanation}" for r in failed)})
        if within_50:
            break  # R-09: within 50km, origin hub only -- no fallback to other hubs, ever

    return {
        "vehicle_id": None, "reg_canonical": None, "hub_used": None,
        "rule_trace": [{"rule_id": "R-09", "passed": True, "explanation": sourcing_note}],
        "rejected": rejected,
    }
