"""Single entrypoint. `python -m app.cli <command>`.

Commands:
  ingest            parse all static/live sources into SQLite (idempotent)
  run [file...]     process ticket queue file(s) through the pipeline, export outputs
  watch [dir]       unattended mode: poll a directory for new ticket files and process
                     each one automatically as it appears (Ctrl+C to stop)
  approve <id>      human approval gate: mark one drafted comm as sent
  approve-all       approve every pending comm (prompts once, for demo convenience)
  explain <id>      replay the audit trail for one ticket, reconstruct the full decision
  query "<q>"       ask the Part A query interface
  report            write outputs/entity_resolution_report.json
  scan              PII leak scan over every output file (shipping gate)
  export-csv        mirror outputs/*.jsonl to outputs/*.csv
  dashboard         regenerate outputs/dashboard.html (static snapshot)
  serve [port]      LIVE operations console: approve, ingest new files, and ask
                     questions from the browser itself (default port 8420)
  reset             wipe local state and start clean
  all               ingest + run tickets.json + report + dashboard + csv + scan (one-command path)
"""
import hashlib
import json
import sys
import time
from datetime import datetime
from pathlib import Path

from app import db, ingest, pii
from app.pipeline import ingest_ticket_file, export_outputs, approve_comm, BUNDLE_DIR, OUTPUTS_DIR, AUDIT_DIR
from app.dashboard import write_dashboard
from app.csv_export import export_all_csv

MAIN_QUEUE = BUNDLE_DIR / "tickets.json"


def cmd_ingest():
    db.init_db()
    conn = db.connect()
    report = ingest.run_full_ingestion(conn)
    conn.close()
    print(json.dumps(report, indent=2))


def cmd_run(files):
    db.init_db()
    conn = db.connect()
    files = [Path(f) for f in files] if files else [MAIN_QUEUE]
    for f in files:
        r = ingest_ticket_file(conn, f)
        print(f"processed {r['source_file']}: {r['records_seen']} records seen")
    export_report = export_outputs(conn)
    conn.close()
    print(json.dumps(export_report, indent=2))
    if export_report["quarantine"] or export_report["blocked_no_vehicle"]:
        print(f"\n*** ALERT: {export_report['quarantine']} ticket(s) quarantined (broken records, need review), "
              f"{export_report['blocked_no_vehicle']} ticket(s) blocked (no eligible vehicle, need a dispatcher's call). "
              f"See outputs/quarantine.jsonl, outputs/blocked_no_vehicle.jsonl, or the dashboard's "
              f"Needs Attention panel.")
    _run_scan()  # early check right after processing; cmd_all re-scans everything (incl. dashboard.html, CSVs) at the very end


WATCH_EXTENSIONS = (".json", ".csv", ".jsonl")


def _process_one_file(conn, path: Path):
    r = ingest_ticket_file(conn, path)
    export_report = export_outputs(conn)
    write_dashboard(conn)
    export_all_csv()
    print(f"  -> {r['records_seen']} records seen; work_orders={export_report['work_orders']} "
          f"quarantine={export_report['quarantine']} blocked={export_report['blocked_no_vehicle']}")
    if export_report["quarantine"] or export_report["blocked_no_vehicle"]:
        print(f"  *** ALERT: {export_report['quarantine']} quarantined, "
              f"{export_report['blocked_no_vehicle']} blocked -- see dashboard Needs Attention panel")
    _run_scan(extensions=("*.jsonl", "*.csv", "*.html"))


def cmd_watch(args):
    watch_dir = Path(args[0]) if args else (BUNDLE_DIR / "incoming")
    interval = float(args[1]) if len(args) > 1 else 10.0
    watch_dir.mkdir(parents=True, exist_ok=True)

    db.init_db()
    conn = db.connect()
    if conn.execute("SELECT COUNT(*) c FROM vehicles").fetchone()["c"] == 0:
        print("no ingested corpus found -- running ingest + main queue once before watching")
        ingest.run_full_ingestion(conn)
        ingest_ticket_file(conn, MAIN_QUEUE)
        export_outputs(conn)
        write_dashboard(conn)
        export_all_csv()

    print(f"watching {watch_dir} every {interval}s for new ticket files (any of {WATCH_EXTENSIONS}). Ctrl+C to stop.")
    try:
        while True:
            for path in sorted(watch_dir.iterdir()):
                if not path.is_file() or path.suffix.lower() not in WATCH_EXTENSIONS:
                    continue
                content_hash = hashlib.sha256(path.read_bytes()).hexdigest()
                seen = conn.execute(
                    "SELECT content_hash FROM watched_files WHERE filename=?", (path.name,)
                ).fetchone()
                if seen and seen["content_hash"] == content_hash:
                    continue  # already processed this exact file content
                print(f"[{time.strftime('%H:%M:%S')}] new/changed file detected: {path.name}")
                _process_one_file(conn, path)
                now = datetime.utcnow().isoformat()
                conn.execute(
                    "INSERT OR REPLACE INTO watched_files (filename, content_hash, first_seen_at, processed_at) "
                    "VALUES (?,?, COALESCE((SELECT first_seen_at FROM watched_files WHERE filename=?), ?), ?)",
                    (path.name, content_hash, path.name, now, now),
                )
                conn.commit()
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nwatch stopped.")
    finally:
        conn.close()


def cmd_approve(ticket_id):
    conn = db.connect()
    ok, msg = approve_comm(conn, ticket_id, approved_by="cli-operator")
    conn.close()
    print(msg)
    if ok:
        conn = db.connect()
        export_outputs(conn)
        conn.close()


def cmd_approve_all():
    conn = db.connect()
    pending = [r["ticket_id"] for r in conn.execute("SELECT ticket_id FROM comms WHERE status='PENDING'")]
    if not pending:
        print("nothing pending")
        return
    print(f"{len(pending)} messages pending approval:")
    for tid in pending:
        row = conn.execute("SELECT body FROM comms WHERE ticket_id=?", (tid,)).fetchone()
        print(f"  [{tid}] {row['body'][:120]}...")
    resp = input(f"Approve all {len(pending)}? [y/N] ").strip().lower()
    if resp != "y":
        print("cancelled")
        return
    for tid in pending:
        approve_comm(conn, tid, approved_by="cli-operator-bulk")
    export_outputs(conn)
    conn.close()
    print(f"approved {len(pending)}")


def cmd_explain(ticket_id):
    conn = db.connect()
    t = conn.execute("SELECT * FROM tickets WHERE ticket_id=?", (ticket_id,)).fetchone()
    if not t:
        print(f"no such ticket: {ticket_id}")
        return
    print(f"=== {ticket_id} === state={t['state']}  client={t['client_canonical']}  vehicle={t['reg_canonical']}")
    for row in conn.execute("SELECT * FROM audit_events WHERE ticket_id=? ORDER BY id", (ticket_id,)):
        rules = json.loads(row["rule_ids_json"])
        print(f"  [{row['created_at']}] {row['step']}: {row['decision']}" + (f"  (rules: {rules})" if rules else ""))
    occ = conn.execute("SELECT COUNT(*) c FROM ticket_occurrences WHERE ticket_id=?", (ticket_id,)).fetchone()["c"]
    print(f"  total occurrences seen across all files/reruns: {occ}")
    conn.close()


def cmd_query(question):
    from app import query as q
    conn = db.connect()
    result = q.answer(conn, question)
    conn.close()
    print(json.dumps(result, indent=2, ensure_ascii=False))


def cmd_report():
    conn = db.connect()
    counts = {}
    for table in ("vehicles", "drivers", "clients", "tickets", "work_orders", "comms", "documents"):
        counts[table] = conn.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"]
    dup_groups = conn.execute(
        "SELECT reg_canonical, COUNT(*) n FROM vehicle_aliases GROUP BY reg_canonical HAVING n > 1"
    ).fetchall()
    conflicts = conn.execute("SELECT COUNT(*) c FROM facts WHERE is_winner=0").fetchone()["c"]
    synthesized = conn.execute("SELECT COUNT(*) c FROM vehicles WHERE id_synthesized=1").fetchone()["c"]
    audit_row = conn.execute("SELECT value FROM run_meta WHERE key='pii_redaction_audit'").fetchone()
    pii_audit = json.loads(audit_row["value"]) if audit_row else []
    report = {
        "entity_counts": counts,
        "vehicle_duplicate_source_rows_merged": len(dup_groups),
        "vehicle_ids_synthesized_due_to_missing_source_id": synthesized,
        "fact_conflicts_resolved_by_precedence": conflicts,
        "rules_encoded": conn.execute("SELECT COUNT(*) c FROM rules").fetchone()["c"],
        "pii_redacted_at_ingestion": pii_audit,
        "pii_redacted_at_ingestion_total": sum(a["total"] for a in pii_audit),
    }
    conn.close()
    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUTS_DIR / "entity_resolution_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


def _run_scan(extensions=("*.jsonl",)):
    # Every extension actually shipped must be covered here -- a scan that only
    # checks .jsonl would miss dashboard.html (embeds the same ticket data as
    # inline JSON) and the CSV exports entirely. cmd_all calls this with the
    # full set as its last step, after every artifact exists. docs/index.html
    # is the GitHub Pages mirror of the same dashboard -- also shipped, also scanned.
    paths = []
    for ext in extensions:
        paths += list(OUTPUTS_DIR.glob(ext)) + list(AUDIT_DIR.glob(ext))
    docs_html = BUNDLE_DIR / "docs" / "index.html"
    if "*.html" in extensions and docs_html.exists():
        paths.append(docs_html)
    findings = pii.scan_paths(paths)
    if findings:
        print(f"!!! PII SCAN FAILED: {len(findings)} finding(s) — see below. Fix before this run counts as shipped.")
        for f in findings[:20]:
            print(f"    {f['file']}:{f['line_no']} [{f['pattern']}] {f['snippet']}")
        sys.exit(2)
    print(f"PII scan clean: {len(paths)} file(s) checked ({', '.join(extensions)}), 0 findings.")


def cmd_scan():
    _run_scan(extensions=("*.jsonl", "*.csv", "*.html"))


def cmd_reset():
    db.reset_db()
    print("state reset")


def cmd_dashboard():
    conn = db.connect()
    path = write_dashboard(conn)
    conn.close()
    print(f"dashboard written to {path}")


def cmd_serve(args):
    from app.server import run_server
    open_browser = "--no-browser" not in args
    positional = [a for a in args if not a.startswith("--")]
    port = int(positional[0]) if positional else 8420
    run_server(port=port, open_browser=open_browser)


def cmd_export_csv():
    written = export_all_csv()
    print(json.dumps(written, indent=2))


def cmd_all():
    cmd_ingest()
    cmd_run([])
    cmd_report()
    cmd_dashboard()
    cmd_export_csv()
    cmd_scan()  # final, comprehensive pass over every shipped artifact -- jsonl, csv, and the dashboard's embedded data


COMMANDS = {
    "ingest": lambda args: cmd_ingest(),
    "run": lambda args: cmd_run(args),
    "watch": lambda args: cmd_watch(args),
    "approve": lambda args: cmd_approve(args[0]),
    "approve-all": lambda args: cmd_approve_all(),
    "explain": lambda args: cmd_explain(args[0]),
    "query": lambda args: cmd_query(args[0]),
    "report": lambda args: cmd_report(),
    "scan": lambda args: cmd_scan(),
    "reset": lambda args: cmd_reset(),
    "dashboard": lambda args: cmd_dashboard(),
    "serve": lambda args: cmd_serve(args),
    "export-csv": lambda args: cmd_export_csv(),
    "all": lambda args: cmd_all(),
}


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        sys.exit(1)
    COMMANDS[sys.argv[1]](sys.argv[2:])


if __name__ == "__main__":
    main()
