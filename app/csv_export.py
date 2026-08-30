"""Mirrors every outbox JSONL file to CSV. JSONL is the graded/authoritative
format (per CANDIDATE_README.md); CSV is a convenience export for reviewers
who work in Excel/Sheets, not a second source of truth -- regenerated fresh
from the same DB every run, same as the JSONL exports.
"""
import csv
import json
from pathlib import Path

from app.pipeline import OUTPUTS_DIR, AUDIT_DIR

FILES = [
    (OUTPUTS_DIR / "work_orders.jsonl", OUTPUTS_DIR / "work_orders.csv"),
    (OUTPUTS_DIR / "comms_pending.jsonl", OUTPUTS_DIR / "comms_pending.csv"),
    (OUTPUTS_DIR / "comms_sent.jsonl", OUTPUTS_DIR / "comms_sent.csv"),
    (OUTPUTS_DIR / "quarantine.jsonl", OUTPUTS_DIR / "quarantine.csv"),
    (OUTPUTS_DIR / "blocked_no_vehicle.jsonl", OUTPUTS_DIR / "blocked_no_vehicle.csv"),
    (AUDIT_DIR / "audit.jsonl", AUDIT_DIR / "audit.csv"),
]


def _flatten(row: dict) -> dict:
    out = {}
    for k, v in row.items():
        out[k] = json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v
    return out


def export_all_csv() -> dict:
    written = {}
    for jsonl_path, csv_path in FILES:
        rows = []
        if jsonl_path.exists():
            for line in jsonl_path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    rows.append(_flatten(json.loads(line)))
        tmp = csv_path.with_suffix(".tmp.csv")
        if rows:
            fieldnames = list(rows[0].keys())
            with open(tmp, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows)
        else:
            tmp.write_text("", encoding="utf-8")
        tmp.replace(csv_path)
        written[csv_path.name] = len(rows)
    return written
