"""Plain-English narration layer for the dashboard. Deterministic and
template-based on purpose (no LLM here) -- the audience for this text is a
non-technical stakeholder deciding whether to trust the system, so it has to
be exactly reproducible and never a source of its own hallucination risk.

app/rules.py statements are already written in plain prose (not code), so
"translating" a rule mostly means showing that sentence instead of a bare
rule_id -- see app/dashboard.py's use of RULE_TEXT.
"""

STATE_PLAIN = {
    "QUARANTINED": "Set aside — this record was too broken to safely act on, and a person has been alerted.",
    "VALIDATED": "Checked and accepted — has everything needed to process further.",
    "ENRICHED": "Looked up — we've pulled in the vehicle, driver, and client details for this ticket.",
    "CLASSIFIED": "Assessed — we've worked out how serious this is and which client rules apply.",
    "VEHICLE_SELECTED": "A replacement truck has been picked and reserved.",
    "WORK_ORDER_CREATED": "A work order has been created for the repair/replacement.",
    "COMMS_PENDING": "A message to the client has been drafted and is waiting for a person to approve it.",
    "COMMS_SENT": "The client has been notified — a person reviewed and approved this message.",
    "BLOCKED_NO_ELIGIBLE_VEHICLE": "On hold — no truck could be found that safely meets every rule, so this needs a person's judgment call.",
}

STEP_PLAIN = {
    "validate": "Step 1 — Checked the ticket for missing or broken information.",
    "dedup": "Note — this same ticket showed up again; it was not processed a second time.",
    "enrich": "Step 2 — Looked up the truck, driver, client, and repair history involved.",
    "classify": "Step 3 — Worked out how serious this is and which client's rules apply.",
    "select_vehicle": "Step 4 — Looked for a replacement truck that passes every safety and client rule.",
    "work_order": "Step 5 — Created the work order.",
    "draft_comms": "Step 6 — Drafted a message to the client and queued it for a person to approve.",
    "comms_sent": "Step 7 — A person approved the message and it was sent.",
}


def plain_ticket_story(t: dict, rules_by_id: dict) -> str:
    """One paragraph, no jargon, telling a non-technical reader what
    happened to this specific ticket and why."""
    client = t.get("client") or "an internal"
    vehicle = t.get("vehicle") or "a vehicle"
    route = f"{t.get('origin_hub') or 'its origin'} to {t.get('destination') or 'its destination'}"
    issue = t.get("issue") or "a breakdown"

    if t["state"] == "QUARANTINED":
        return (f"This record came in as ticket {t['ticket_id']} but was missing information we need to act "
                f"safely (details: {t.get('quarantine_reason') or 'see reason field'}). "
                f"Rather than guess, the system set it aside and flagged it for a person to check.")

    parts = [f"Truck {vehicle}, on a {client} run from {route}, reported: {issue}."]

    if t.get("severity"):
        parts.append(f"This was assessed as {t['severity'].lower()} severity.")

    if t["state"] == "BLOCKED_NO_ELIGIBLE_VEHICLE":
        parts.append("We looked for a replacement truck, but none of the available ones passed every safety "
                      "and client rule at the same time (for example: correct emissions class for the season, "
                      "not overdue for service, or a rotation rule for this client). Rather than send a truck "
                      "that breaks a rule, the system stopped and flagged this for a dispatcher to decide by hand.")
        return " ".join(parts)

    if t.get("replacement"):
        hub = t.get("hub")
        parts.append(f"A replacement truck ({t['replacement']}) was found at the {hub or 'nearest eligible'} "
                      f"hub and reserved so no other ticket can also claim it.")

    if t["state"] in ("WORK_ORDER_CREATED", "COMMS_PENDING", "COMMS_SENT"):
        parts.append("A work order has been logged for this.")

    if t["state"] == "COMMS_PENDING":
        parts.append("A message explaining this to the client has been written and is waiting for a person "
                      "to read and approve it before anything is sent.")
    elif t["state"] == "COMMS_SENT":
        parts.append("A person has reviewed and approved the client message, and it has been sent.")

    return " ".join(parts)
