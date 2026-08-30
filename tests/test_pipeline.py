"""Executable proof for the three rules the challenge is actually scored on
(see CANDIDATE_README.md). Not a mocked unit-test suite -- runs the real
pipeline against the real bundle files in a scratch DB, then asserts on the
real output files. Run with: python tests/test_pipeline.py
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run(*args):
    r = subprocess.run([sys.executable, "-m", "app.cli", *args], cwd=ROOT, capture_output=True, text=True)
    return r


def read_jsonl(path):
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def test_rerun_is_byte_identical():
    run("reset")
    run("all")
    snap = {}
    for f in list((ROOT / "outputs").glob("*.jsonl")) + [ROOT / "audit" / "audit.jsonl"]:
        snap[f.name] = f.read_bytes()
    run("run")
    for f in list((ROOT / "outputs").glob("*.jsonl")) + [ROOT / "audit" / "audit.jsonl"]:
        assert f.read_bytes() == snap[f.name], f"{f.name} changed on rerun -- idempotency violated"
    print("PASS: full pipeline rerun produces byte-identical outputs")


def test_no_duplicate_work_orders_across_duplicate_tickets():
    run("reset")
    run("all")
    wos = read_jsonl(ROOT / "outputs" / "work_orders.jsonl")
    ticket_ids = [w["ticket_id"] for w in wos]
    assert len(ticket_ids) == len(set(ticket_ids)), "duplicate work order for same ticket_id"
    print(f"PASS: {len(wos)} work orders, all unique ticket_ids (no double-write on known duplicates)")


def test_broken_records_quarantined_not_crashed():
    run("reset")
    result = run("all")
    assert result.returncode == 0, f"pipeline crashed:\n{result.stdout}\n{result.stderr}"
    q = read_jsonl(ROOT / "outputs" / "quarantine.jsonl")
    assert len(q) >= 2, "expected the known broken records (TKT-9101, TKT-9102) to be quarantined"
    ids = {r["ticket_id"] for r in q}
    assert {"TKT-9101", "TKT-9102"}.issubset(ids)
    print(f"PASS: {len(q)} broken records quarantined, run did not crash")


def test_surprise_format_file_does_not_crash_and_dedupes():
    run("reset")
    run("all")
    before = len(read_jsonl(ROOT / "outputs" / "work_orders.jsonl"))
    result = run("run", "tests/fixtures/surprise_format_sample.json")
    assert result.returncode == 0, f"schema-drifted file crashed the pipeline:\n{result.stdout}\n{result.stderr}"
    after = len(read_jsonl(ROOT / "outputs" / "work_orders.jsonl"))
    # fixture has 1 record that duplicates TKT-0027 (already in main queue),
    # 1 unparseable record, and 2 new valid tickets under renamed fields
    assert after == before + 2, f"expected exactly 2 new work orders from the surprise file, got {after - before}"
    print(f"PASS: schema-drifted file processed without crashing, dedup against main queue held ({before} -> {after})")


def test_pii_scanner_catches_a_real_leak():
    run("reset")
    run("all")
    target = ROOT / "outputs" / "work_orders.jsonl"
    original = target.read_bytes()
    try:
        with open(target, "a", encoding="utf-8") as f:
            f.write('{"leak": "+91 98765 43210"}\n')
        result = run("scan")
        assert result.returncode != 0, "PII scanner did not fail on an injected phone number"
        assert "PII SCAN FAILED" in result.stdout
        print("PASS: PII scanner detects and fails on an injected leak")
    finally:
        target.write_bytes(original)


def test_no_pii_in_clean_run():
    run("reset")
    run("all")
    result = run("scan")
    assert result.returncode == 0, f"clean run should not trip the PII scanner:\n{result.stdout}"
    print("PASS: a clean run has zero PII findings across all output files")


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in fns:
        try:
            fn()
        except AssertionError as e:
            failures += 1
            print(f"FAIL: {fn.__name__}: {e}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    # leave the repo in a clean, demoable state
    run("reset")
    run("all")
    sys.exit(1 if failures else 0)
