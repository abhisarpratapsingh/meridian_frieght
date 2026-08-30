"""Client message drafting. Deterministic template is the only thing that
ever ships without a human reading it first, and it's the fallback for
everything below it -- an optional local-LLM "polish" pass can rewrite the
prose, but never adds a fact the template didn't already state, and never
runs unless explicitly opted into separately from the Part A query LLM
(comms are client-facing; that's a stricter bar than an internal Q&A answer).
"""
import os
import urllib.request
import json

from app import pii

CLIENT_NOTES = {
    "Shakti Cement": "Delivery is being replanned to our internal 36-hour commitment, not the 48-hour contract figure (per standing agreement).",
    "Vertex Retail": "If the revised arrival falls after 18:00 at the Ludhiana gate, this will be scheduled as a next-morning delivery at 08:00, not marked as a failed delivery, per our standing arrangement.",
    "Orion Pharma": "The replacement vehicle meets the 2020-or-later model-year requirement for your consignments.",
    "Apex Chemicals": "A different vehicle is being used for this dispatch, consistent with our post-incident rotation practice.",
}

POLISH_TIMEOUT = 20
POLISH_MODEL = os.environ.get("MERIDIAN_LLM_MODEL", "qwen2.5:1.5b")
POLISH_ENABLED = os.environ.get("MERIDIAN_LLM_POLISH_COMMS", "0") == "1"


def build_deterministic_message(ticket: dict, reservation, classification) -> str:
    """ticket/reservation/classification are sqlite3.Row or dict-likes."""
    client = ticket["client_canonical"] or "Client"
    issue = ticket["issue"] or "a mechanical issue"
    hub = reservation["hub_used"] if reservation else "the nearest"
    severity = (classification["severity"] if classification else ticket["severity_input"]) or "unclassified"

    lines = [
        f"Update on your shipment (ref {ticket['ticket_id']}): the assigned vehicle experienced a "
        f"breakdown ({issue}), assessed as {severity.lower()} severity.",
        f"A replacement vehicle has been dispatched from our {hub} hub and the delivery plan has been "
        f"adjusted accordingly.",
    ]
    note = CLIENT_NOTES.get(client)
    if note:
        lines.append(note)
    lines.append("We will confirm the revised delivery window shortly.")
    return pii.redact(" ".join(lines))


def _try_polish(base_text: str) -> str:
    """Best-effort local-LLM rewrite. Any failure returns None (caller keeps
    the deterministic text). The prompt explicitly forbids adding facts --
    this is a phrasing pass, not a drafting pass; the facts were already
    decided by the deterministic template above."""
    if not POLISH_ENABLED:
        return None
    try:
        prompt = (
            "Rewrite the following client update in clear, professional, courteous business English. "
            "Do not add, remove, or change any fact, number, date, place name, or client name. "
            "Do not invent an apology or a promise that isn't already there. Keep it to 2-4 sentences.\n\n"
            f"Text to rewrite:\n{base_text}"
        )
        req = urllib.request.Request(
            "http://localhost:11434/api/generate",
            data=json.dumps({"model": POLISH_MODEL, "prompt": prompt, "stream": False}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=POLISH_TIMEOUT) as resp:
            data = json.loads(resp.read())
            text = data.get("response", "").strip()
            return pii.redact(text) if text else None
    except Exception:
        return None


def draft_message(ticket: dict, reservation, classification) -> dict:
    """Returns {body, body_deterministic, polished}. `body` is what gets
    shown to the approver and (if approved) sent; `body_deterministic` is
    kept alongside it always, so a reviewer can diff the two and confirm the
    polish pass didn't drift from the facts -- this is the whole point of
    keeping both instead of overwriting."""
    deterministic = build_deterministic_message(ticket, reservation, classification)
    polished = _try_polish(deterministic)
    return {
        "body": polished or deterministic,
        "body_deterministic": deterministic,
        "polished": bool(polished),
    }
