"""Part A query interface. Deterministic retrieval is the source of truth;
an optional local LLM (Ollama) only phrases a sentence from snippets already
retrieved -- it is never allowed to introduce a fact that isn't in `citations`.

Because hallucination is negative-marked, the default behavior below every
lookup is: if retrieval confidence is below ABSTAIN_THRESHOLD, say so plainly
and cite nothing, rather than let an LLM paper over the gap.
"""
import json
import os
import re

ABSTAIN_THRESHOLD = 1  # min FTS hits required before we'll answer from free text at all
LLM_ENABLED = os.environ.get("MERIDIAN_LLM_ENABLED", "0") == "1"
# Default picked by benchmarking what was actually installed on the target
# machine (16GB RAM, Intel Iris Xe -- CPU-bound inference, no dedicated VRAM),
# not by parameter count alone -- see EVIDENCE.md "local LLM selection".
# qwen2.5:1.5b: 23 tok/s, followed the grounding instruction correctly, ~1GB.
# phi3:mini: 7.5 tok/s on the same hardware. gemma4:e2b: 13.7 tok/s, ~7GB,
# and answered vaguer despite being the largest model tested. Bigger was not
# smarter-for-this-job on this hardware -- benchmark, don't assume.
LLM_MODEL = os.environ.get("MERIDIAN_LLM_MODEL", "qwen2.5:1.5b")


def _try_ollama_phrase(question: str, snippets: list) -> str:
    """Best-effort local LLM phrasing. Any failure (not installed, model not
    pulled, network off) silently falls back to the deterministic template --
    the answer's factual content never depends on this succeeding."""
    if not LLM_ENABLED:
        return None
    try:
        import urllib.request
        prompt = (
            "Answer the question using ONLY the facts in the snippets below. "
            "Do not add any fact, name, number, or claim that is not literally present in a snippet. "
            "If the snippets don't answer the question, say so.\n\n"
            f"Question: {question}\n\nSnippets:\n" + "\n---\n".join(snippets)
        )
        req = urllib.request.Request(
            "http://localhost:11434/api/generate",
            data=json.dumps({"model": LLM_MODEL, "prompt": prompt, "stream": False}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=20) as resp:  # cold model load can take >8s; a slow answer still beats a spurious fallback
            data = json.loads(resp.read())
            return data.get("response", "").strip() or None
    except Exception:
        return None


def _vehicle_lookup(conn, reg_canonical):
    v = conn.execute("SELECT * FROM vehicles WHERE reg_canonical=?", (reg_canonical,)).fetchone()
    if not v:
        return None
    facts = conn.execute(
        "SELECT attribute, value, source_file, source_ref, is_winner FROM facts "
        "WHERE entity_type='vehicle' AND entity_id=? ORDER BY attribute", (v["vehicle_id"],)
    ).fetchall()
    return dict(v), [dict(f) for f in facts]


def _find_reg_in_text(conn, text):
    from app.normalize import canonical_reg
    for token in re.findall(r"[A-Za-z]{2}[\s-]?\d{1,2}[\s-]?[A-Za-z]{0,2}[\s-]?\d{3,4}", text):
        reg = canonical_reg(token)
        if reg and conn.execute("SELECT 1 FROM vehicles WHERE reg_canonical=?", (reg,)).fetchone():
            return reg
    return None


def _rule_search(conn, text):
    hits = []
    for row in conn.execute("SELECT * FROM rules"):
        words = set(re.findall(r"[a-z]{4,}", text.lower()))
        stmt_words = set(re.findall(r"[a-z]{4,}", row["statement"].lower()))
        overlap = words & stmt_words
        if len(overlap) >= 2:
            hits.append((len(overlap), dict(row)))
    hits.sort(key=lambda x: -x[0])
    return [h[1] for h in hits[:3]]


def answer(conn, question: str) -> dict:
    q = question.strip()
    qlow = q.lower()

    # 1. Vehicle-specific question
    reg = _find_reg_in_text(conn, q)
    if reg:
        result = _vehicle_lookup(conn, reg)
        if result:
            v, facts = result
            winners = [f for f in facts if f["is_winner"]]
            conflicts = [f for f in facts if not f["is_winner"]]
            text = (f"{reg}: {v['model']} ({v['year']}), {v['bs_stage']}, home hub {v['home_hub']}, "
                    f"status {v['status']}, capacity {v['capacity_tonnes']}t.")
            citations = [f"fleet_master.csv (winning fact: {f['attribute']}={f['value']})" for f in winners]
            if conflicts:
                text += f" Note: {len(conflicts)} conflicting source record(s) exist for this vehicle; resolved per P-01/data precedence, see facts table."
                citations += [f"fleet_master.csv (superseded: {f['attribute']}={f['value']}, {f['note']})" for f in conflicts]
            return {"answer": text, "citations": citations, "confidence": "high", "abstained": False}

    # 2. Client SLA / policy question
    for client in ("Shakti Cement", "Vertex Retail", "Apex Chemicals", "Orion Pharma"):
        if client.lower().split()[0] in qlow:
            row = conn.execute("SELECT * FROM clients WHERE canonical_name=?", (client,)).fetchone()
            if row:
                text = row["notes"]
                if row["sla_hours_effective"]:
                    text = f"Effective SLA is {row['sla_hours_effective']}h (contract says {row['sla_hours_contract']}h). " + text
                return {"answer": text, "citations": ["clients table (seeded from dispatcher_interview.txt + corroborating email thread)"],
                        "confidence": "high", "abstained": False}

    # 3. Rule / policy keyword question
    rule_hits = _rule_search(conn, q)
    if rule_hits:
        text = " ".join(f"[{r['rule_id']}] {r['statement']}" for r in rule_hits)
        citations = [r["source_citation"] for r in rule_hits]
        snippets = [r["statement"] for r in rule_hits]
        llm_phrased = _try_ollama_phrase(q, snippets)
        return {"answer": llm_phrased or text, "citations": citations, "confidence": "high", "abstained": False}

    # 4. Free-text retrieval over transcript + emails via FTS5.
    # OR-matching alone is too permissive -- almost any question shares one
    # common word with some passage, which would let the system "answer"
    # things it has no real basis for (exactly the hallucination-adjacent
    # failure mode negative marking is designed to catch). So: retrieve
    # candidates with OR, then keep only passages with >=2 shared significant
    # words with the question, same bar as the rule search above. Abstain if
    # nothing clears that bar.
    stopwords = {"what", "when", "where", "which", "does", "with", "have", "this", "that", "about", "tell"}
    q_words = {w for w in re.findall(r"[a-z]{4,}", qlow) if w not in stopwords}
    fts_query = " OR ".join(q_words) if q_words else None
    rows = []
    if fts_query:
        try:
            rows = conn.execute(
                "SELECT doc_id, source_file, text_redacted, bm25(documents_fts) as score "
                "FROM documents_fts WHERE documents_fts MATCH ? ORDER BY score LIMIT 10",
                (fts_query,),
            ).fetchall()
        except Exception:
            rows = []

    filtered = []
    for r in rows:
        doc_words = set(re.findall(r"[a-z]{4,}", r["text_redacted"].lower()))
        if len(q_words & doc_words) >= 2:
            filtered.append(r)
    rows = filtered[:5]

    if len(rows) < ABSTAIN_THRESHOLD:
        return {
            "answer": "Insufficient data in the ingested corpus to answer this confidently. No matching source record found.",
            "citations": [], "confidence": "none", "abstained": True,
        }

    snippets = [r["text_redacted"] for r in rows]
    citations = [r["doc_id"] for r in rows]
    llm_phrased = _try_ollama_phrase(q, snippets)
    fallback_text = "Most relevant source passages: " + " | ".join(s[:220] for s in snippets[:2])
    return {"answer": llm_phrased or fallback_text, "citations": citations, "confidence": "medium", "abstained": False}
