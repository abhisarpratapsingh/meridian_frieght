"""The live operations console. Still local-only, still one-command, still
no internet required at runtime -- but now a real app instead of a static
snapshot, because an enterprise tool closes the loop (approve, ingest a new
file, ask a question) instead of just reporting on one.

Every mutating endpoint re-exports outputs/*.jsonl, outputs/*.csv, and
outputs/dashboard.html after it runs, and re-scans for PII -- so the file
outputs stay a true mirror of the live DB no matter how the state changed,
and the hard gate is checked on every single action, not just at the end of
a batch run.
"""
import hashlib
import json
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, request, Response

from app import db, pii, query as query_mod
from app.dashboard import generate_dashboard
from app.pipeline import (
    ingest_ticket_file, export_outputs, approve_comm, BUNDLE_DIR, OUTPUTS_DIR, AUDIT_DIR,
    list_candidates_for_ticket, resolve_blocked_manually, resolve_quarantine,
)
from app.csv_export import export_all_csv

INCOMING_DIR = BUNDLE_DIR / "incoming"
MAIN_QUEUE = BUNDLE_DIR / "tickets.json"
WATCH_INTERVAL_SECONDS = 8

app = Flask(__name__)


@app.errorhandler(Exception)
def handle_any_error(e):
    """Any unhandled exception in a route must still come back as JSON, never
    Flask's default HTML error page -- the client always does `await
    r.json()`, and parsing HTML as JSON throws, which used to surface to the
    operator as the misleading "could not reach the server" (a network-layer
    message) for what was actually a server-side bug. Logged server-side in
    full, returned to the client as a short, honest message."""
    traceback.print_exc()
    return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 500


def _sync_outputs(conn):
    """Every mutating action ends here: re-export jsonl, mirror to csv,
    rescan for PII across everything actually shipped. This is what keeps
    the file outputs honest as a mirror of DB state after any live action,
    not just after a batch `run`."""
    export_outputs(conn)
    export_all_csv()
    paths = list(OUTPUTS_DIR.glob("*.jsonl")) + list(OUTPUTS_DIR.glob("*.csv")) + list(AUDIT_DIR.glob("*.jsonl"))
    docs_html = BUNDLE_DIR / "docs" / "index.html"
    if docs_html.exists():
        paths.append(docs_html)
    findings = pii.scan_paths(paths)
    return {"pii_clean": len(findings) == 0, "pii_findings": len(findings)}


@app.get("/")
def index():
    conn = db.connect()
    try:
        html = generate_dashboard(conn)
    finally:
        conn.close()
    return Response(html, mimetype="text/html")


@app.post("/api/approve/<ticket_id>")
def api_approve(ticket_id):
    conn = db.connect()
    try:
        approved_by = (request.json or {}).get("approved_by", "dashboard-operator")
        ok, msg = approve_comm(conn, ticket_id, approved_by=approved_by)
        sync = _sync_outputs(conn)
    finally:
        conn.close()
    return jsonify({"ok": ok, "message": msg, **sync})


@app.post("/api/approve-all")
def api_approve_all():
    conn = db.connect()
    try:
        approved_by = (request.json or {}).get("approved_by", "dashboard-operator")
        pending = [r["ticket_id"] for r in conn.execute("SELECT ticket_id FROM comms WHERE status='PENDING'")]
        for tid in pending:
            approve_comm(conn, tid, approved_by=approved_by)
        sync = _sync_outputs(conn)
    finally:
        conn.close()
    return jsonify({"ok": True, "approved_count": len(pending), **sync})


@app.post("/api/ingest")
def api_ingest():
    """Live equivalent of `python -m app.cli run <file>` -- this is what the
    final-hour surprise file uses: drag it onto the dashboard instead of
    switching to a terminal."""
    if "file" not in request.files:
        return jsonify({"ok": False, "error": "no file in request"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"ok": False, "error": "empty filename"}), 400

    INCOMING_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = Path(f.filename).name  # strip any path components -- never trust a client-supplied path
    dest = INCOMING_DIR / safe_name
    f.save(dest)

    conn = db.connect()
    try:
        result = ingest_ticket_file(conn, dest)
        sync = _sync_outputs(conn)
        counts = {
            "quarantined": conn.execute("SELECT COUNT(*) c FROM tickets WHERE state='QUARANTINED'").fetchone()["c"],
            "blocked": conn.execute("SELECT COUNT(*) c FROM tickets WHERE state='BLOCKED_NO_ELIGIBLE_VEHICLE'").fetchone()["c"],
            "work_orders": conn.execute("SELECT COUNT(*) c FROM work_orders").fetchone()["c"],
        }
    finally:
        conn.close()
    return jsonify({"ok": True, "file": safe_name, "records_seen": result["records_seen"], **counts, **sync})


@app.get("/api/query")
def api_query():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"ok": False, "error": "missing ?q="}), 400
    conn = db.connect()
    try:
        result = query_mod.answer(conn, q)
    finally:
        conn.close()
    return jsonify({"ok": True, **result})


@app.get("/api/health")
def api_health():
    return jsonify({"ok": True, "mode": "live-server"})


@app.get("/api/candidates/<ticket_id>")
def api_candidates(ticket_id):
    """Full candidate list with pass/fail-per-rule for a blocked ticket's
    manual-override picker -- the human sees exactly what the automatic
    selector saw, not a blank dropdown."""
    conn = db.connect()
    try:
        candidates = list_candidates_for_ticket(conn, ticket_id)
    finally:
        conn.close()
    return jsonify({"ok": True, "candidates": candidates})


@app.post("/api/resolve-blocked/<ticket_id>")
def api_resolve_blocked(ticket_id):
    body = request.json or {}
    conn = db.connect()
    try:
        ok, msg = resolve_blocked_manually(
            conn, ticket_id, body.get("vehicle_reg", ""),
            body.get("operator", "dashboard-operator"), body.get("reason", ""),
        )
        sync = _sync_outputs(conn) if ok else {}
    finally:
        conn.close()
    return jsonify({"ok": ok, "message": msg, **sync})


@app.post("/api/resolve-quarantine/<ticket_id>")
def api_resolve_quarantine(ticket_id):
    body = request.json or {}
    conn = db.connect()
    try:
        ok, msg = resolve_quarantine(
            conn, ticket_id, body.get("fields", {}), body.get("operator", "dashboard-operator"),
        )
        sync = _sync_outputs(conn) if ok else {}
    finally:
        conn.close()
    return jsonify({"ok": ok, "message": msg, **sync})


@app.get("/api/status")
def api_status():
    """Lightweight polling target for auto-refresh: a fingerprint that
    changes whenever the DB's ticket-relevant state changes (including from
    the background watcher processing a new file with nobody at the
    keyboard), so the page can detect it and prompt a refresh instead of the
    operator needing to know to reload."""
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT (SELECT COUNT(*) FROM tickets) AS tickets, (SELECT COUNT(*) FROM work_orders) AS wo, "
            "(SELECT COUNT(*) FROM comms WHERE status='SENT') AS sent, "
            "(SELECT COUNT(*) FROM audit_events) AS audit"
        ).fetchone()
        fingerprint = hashlib.sha256(json.dumps(dict(row)).encode()).hexdigest()[:16]
    finally:
        conn.close()
    return jsonify({"ok": True, "fingerprint": fingerprint, "watching": str(INCOMING_DIR)})


def _watch_loop():
    """Runs for the lifetime of the server process: the actual answer to
    'no automatic inbound ticket option'. A file dropped into incoming/ --
    by a person, by another system, by the final-hour surprise-file handoff
    -- gets picked up and processed within one poll interval, with nobody
    touching the UI or the CLI. Same content-hash dedup as `cli.py watch` so
    a reappearing file is never reprocessed."""
    conn = db.connect()
    try:
        while True:
            try:
                for path in sorted(INCOMING_DIR.glob("*")):
                    if not path.is_file() or path.suffix.lower() not in (".json", ".jsonl", ".csv"):
                        continue
                    content_hash = hashlib.sha256(path.read_bytes()).hexdigest()
                    seen = conn.execute(
                        "SELECT content_hash FROM watched_files WHERE filename=?", (path.name,)
                    ).fetchone()
                    if seen and seen["content_hash"] == content_hash:
                        continue
                    print(f"[watcher] processing {path.name}")
                    ingest_ticket_file(conn, path)
                    _sync_outputs(conn)
                    now = datetime.utcnow().isoformat()
                    conn.execute(
                        "INSERT OR REPLACE INTO watched_files (filename, content_hash, first_seen_at, processed_at) "
                        "VALUES (?,?, COALESCE((SELECT first_seen_at FROM watched_files WHERE filename=?), ?), ?)",
                        (path.name, content_hash, path.name, now, now),
                    )
                    conn.commit()
            except Exception:
                traceback.print_exc()  # a bad file must never kill the watcher loop itself
            time.sleep(WATCH_INTERVAL_SECONDS)
    finally:
        conn.close()


def run_server(host="127.0.0.1", port=8420, debug=False, open_browser=True):
    from app import ingest as ingest_mod

    db.init_db()
    conn = db.connect()
    if conn.execute("SELECT COUNT(*) c FROM vehicles").fetchone()["c"] == 0:
        print("no ingested corpus found -- bootstrapping (ingest + main queue) before starting...")
        ingest_mod.run_full_ingestion(conn)
        ingest_ticket_file(conn, MAIN_QUEUE)
        export_outputs(conn)
        export_all_csv()
        print("bootstrap complete.")
    conn.close()

    INCOMING_DIR.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=_watch_loop, daemon=True).start()

    url = f"http://{host}:{port}"
    print("=" * 72)
    print(f"  Meridian Freight LIVE console:  {url}")
    print("  Open EXACTLY this URL in your browser for live actions to work.")
    print("  outputs/dashboard.html is a SEPARATE, read-only static snapshot --")
    print("  opening that file directly (double-click / file:// in the address")
    print("  bar) will always show 'needs the live server', because a file")
    print("  opened from disk has no server to talk to. That is not a bug --")
    print("  it is a different artifact. Use the URL printed above instead.")
    print(f"  Auto-watching {INCOMING_DIR} every {WATCH_INTERVAL_SECONDS}s.")
    print("=" * 72)

    if open_browser:
        import webbrowser
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    # threaded=True: a slow ingest request (large file) must not block a
    # concurrent approve click from being served -- single-threaded dev
    # server default would make that read as "unreachable" client-side.
    app.run(host=host, port=port, debug=debug, threaded=True)
