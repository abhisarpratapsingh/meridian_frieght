"""Format-tolerant ticket-file loader. Built BEFORE the surprise file arrives,
on purpose: the brief says the client's IT team "changes formats without
telling you", which is a class of problem, not one specific file. This module
handles the class: detect JSON array / JSON-lines / CSV, fuzzy-map renamed
fields, and NEVER raise -- an unreadable record is quarantined with a reason,
the rest of the file still processes.
"""
import csv
import json
import re

FIELD_ALIASES = {
    "ticket_id": ["ticket_id", "id", "ticketid", "ticket_no", "ticketno"],
    "created_at": ["created_at", "createdat", "timestamp", "date", "reported_at", "reportedat"],
    "vehicle": ["vehicle", "vehicle_reg", "vehiclereg", "reg", "registration", "vehicle_number"],
    "driver_id": ["driver_id", "driverid", "driver"],
    "origin_hub": ["origin_hub", "originhub", "hub", "origin"],
    "km_from_origin_hub": ["km_from_origin_hub", "kmfromoriginhub", "distance_km", "distancekm", "km"],
    "destination": ["destination", "dest"],
    "issue": ["issue", "problem", "fault", "issue_description"],
    "severity": ["severity", "priority"],
    "client": ["client", "customer"],
    "status": ["status"],
    "resolution_note": ["resolution_note", "resolutionnote", "notes"],
}


def _normkey(k):
    return re.sub(r"[^a-z0-9]", "", str(k).lower())


ALIAS_LOOKUP = {}
for canon, alts in FIELD_ALIASES.items():
    for a in alts:
        ALIAS_LOOKUP[_normkey(a)] = canon


def map_record(raw: dict) -> tuple:
    """Returns (mapped_dict, notes list). Unknown keys are kept under
    '_unmapped' rather than dropped -- visible in the audit, not silently lost."""
    mapped = {}
    unmapped = {}
    for k, v in raw.items():
        canon = ALIAS_LOOKUP.get(_normkey(k))
        if canon:
            if canon in mapped:
                unmapped[k] = v  # second raw key mapping to same canonical field: keep both, flag it
            else:
                mapped[canon] = v
        else:
            unmapped[k] = v
    notes = []
    if unmapped:
        notes.append(f"unmapped source fields (schema drift): {sorted(unmapped.keys())}")
    missing_required = [f for f in ("ticket_id", "created_at", "vehicle", "origin_hub", "issue", "client", "km_from_origin_hub")
                         if f not in mapped or mapped[f] in (None, "")]
    if missing_required:
        notes.append(f"missing/blank after mapping: {missing_required}")
    mapped["_unmapped"] = unmapped
    mapped["_adapter_notes"] = notes
    return mapped, notes


def _sniff_and_load_raw(path) -> list:
    text = open(path, encoding="utf-8-sig").read()
    stripped = text.strip()
    if not stripped:
        return []
    if stripped.startswith("["):
        return json.loads(stripped)
    if stripped.startswith("{"):
        # could be JSON-lines or a single JSON object
        lines = [l for l in stripped.splitlines() if l.strip()]
        records = []
        all_parsed = True
        for l in lines:
            try:
                records.append(json.loads(l))
            except json.JSONDecodeError:
                all_parsed = False
                break
        if all_parsed and records:
            return records
        return [json.loads(stripped)]
    # fall back to CSV
    return list(csv.DictReader(stripped.splitlines()))


def load_ticket_records(path) -> list:
    """Never raises. A file-level parse failure yields a single quarantine-style
    pseudo-record describing the failure instead of crashing the run."""
    try:
        raw_records = _sniff_and_load_raw(path)
    except Exception as e:
        return [{
            "ticket_id": None, "_file_level_error": f"{type(e).__name__}: {e}",
            "_adapter_notes": [f"could not parse file {path.name} at all: {e}"],
            "_unmapped": {},
        }]

    out = []
    for raw in raw_records:
        try:
            if not isinstance(raw, dict):
                out.append({"ticket_id": None, "_adapter_notes": [f"record is not an object: {raw!r}"], "_unmapped": {}})
                continue
            mapped, _ = map_record(raw)
            out.append(mapped)
        except Exception as e:
            out.append({"ticket_id": raw.get("ticket_id") if isinstance(raw, dict) else None,
                         "_adapter_notes": [f"record-level parse error: {type(e).__name__}: {e}"],
                         "_unmapped": raw if isinstance(raw, dict) else {}})
    return out
