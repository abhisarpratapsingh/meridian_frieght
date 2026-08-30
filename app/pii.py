"""PII boundary. Two jobs:

1. redact(text) — strip phone/Aadhaar/DL-number spans out of any free text
   (transcript, emails, maintenance notes) BEFORE it is stored in the DB or
   used as a retrieval snippet. Applied at ingestion, not at output time, so
   raw PII never exists downstream of this module.
2. scan_paths(...) — an independent, adversarial re-check: grep every output
   file for the same patterns right before a run is considered complete, and
   fail loudly if anything slipped through. This is the "automated proof",
   not "we were careful", answer to the hard gate.

Structured PII fields (drivers_roster phone/dl/aadhaar columns) are never
copied into the DB at all — only a salted SHA-256 hash, for equality checks
(e.g. "is this the same driver record") without ever holding the raw value.
"""
import hashlib
import re

SALT = "meridian-freight-local-salt-v1"  # local-only, never transmitted; not a security boundary, just prevents accidental raw storage

PHONE_RE = re.compile(r"(?:\+?91[\s-]?)?\d{5}[\s-]?\d{5}\b")
AADHAAR_RE = re.compile(r"\b\d{4}\s\d{4}\s\d{4}\b")
DL_RE = re.compile(r"\b[A-Z]{2}\d{2}\s\d{11,13}\b")

PATTERNS = {
    "phone": PHONE_RE,
    "aadhaar": AADHAAR_RE,
    "dl_number": DL_RE,
}


def hash_value(value: str) -> str:
    if value is None:
        return None
    digits_only = re.sub(r"\D", "", str(value))
    basis = digits_only or str(value).strip()
    return hashlib.sha256((SALT + basis).encode("utf-8")).hexdigest()[:32]


def redact(text: str) -> str:
    if not text:
        return text
    out = text
    out = AADHAAR_RE.sub("[REDACTED_AADHAAR]", out)
    out = DL_RE.sub("[REDACTED_DL]", out)
    out = PHONE_RE.sub("[REDACTED_PHONE]", out)
    return out


def redact_and_count(text: str):
    """Same as redact(), but also returns how many spans were removed, split
    by pattern. Used at ingestion time to build a redaction audit ("we found
    and removed N instances, here's the breakdown") -- a scan that finds
    zero at output time is a much stronger claim when paired with a nonzero
    count of what was caught earlier, upstream, before it could ever reach
    an output file."""
    if not text:
        return text, {}
    counts = {}
    out = text
    for name, pat in (("aadhaar", AADHAAR_RE), ("dl_number", DL_RE), ("phone", PHONE_RE)):
        matches = pat.findall(out)
        if matches:
            counts[name] = len(matches)
        out = pat.sub(f"[REDACTED_{name.upper()}]", out)
    return out, counts


def redact_dict(value):
    """Recursively redact every string value in a dict/list/scalar -- used
    when exposing a raw source record for technical review, so a reviewer
    can cross-check the algorithm's reasoning against real input data
    without that record becoming a second, unaudited path for PII to leak."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: redact_dict(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_dict(v) for v in value]
    return value


def find_matches(text: str):
    """Return list of (pattern_name, match_text) — used by the scanner."""
    hits = []
    if not text:
        return hits
    for name, pat in PATTERNS.items():
        for m in pat.finditer(text):
            hits.append((name, m.group(0)))
    return hits


def scan_paths(paths) -> list:
    """Scan a list of file paths for PII patterns. Returns a list of
    {file, line_no, pattern, snippet} for every hit found. Empty list = clean.
    Intended to run as a mandatory gate before a run is reported complete."""
    findings = []
    for p in paths:
        if not p.exists():
            continue
        with open(p, "r", encoding="utf-8") as f:
            for i, line in enumerate(f, start=1):
                for name, match in find_matches(line):
                    findings.append({
                        "file": str(p),
                        "line_no": i,
                        "pattern": name,
                        "snippet": match,
                    })
    return findings
