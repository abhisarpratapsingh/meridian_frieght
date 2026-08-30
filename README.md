# Meridian Freight — Breakdown-to-Resolution

## The live console (recommended for a demo)

```bash
pip install -r requirements.txt
python -m app.cli serve
```

`serve` bootstraps everything automatically on first run (ingests the static
corpus, processes the main queue) if the database is empty, then opens your
browser to the right URL itself. **Use that URL — do not open
`outputs/dashboard.html` by double-clicking it.** That file is a separate,
read-only static snapshot (see below); opening it directly will show a
loud yellow banner and every live action will correctly refuse to run,
because a file opened from disk has no server behind it to talk to. That's
not a bug — it's a different artifact, and the app now tells you so instead
of failing silently.

A real local app, not a static report:

- **Home** — an animated resolution-rate ring, a "Next Best Action" card
  (picks the single highest-priority thing to look at: blocked tickets
  first, then quarantined, then pending approval), a recent-activity feed,
  and quick-jump stats.
- **Approve** client messages — individually or all at once — with the
  result reflected immediately.
- **Resolve blocked tickets manually** — every active vehicle shown with its
  full rule-by-rule pass/fail against that ticket, so an override is an
  informed decision with a written reason that becomes part of the audit
  trail, not a guess.
- **Resolve quarantined tickets** — fix the missing/broken fields in a
  pre-filled form and resubmit; it goes through the exact same validation a
  fresh ticket faces.
- **Drag in a new ticket file** — the live path for the final-hour surprise
  file — or just drop it in `incoming/`: a background watcher processes it
  automatically, no button needed, and the dashboard auto-refreshes when it
  detects the change.
- **Ask questions**, live, from the browser.

Every action re-exports `outputs/*.jsonl`/`*.csv` and re-runs the PII scan
before responding, so the files on disk always match what the UI shows.
Still fully local — no internet dependency at runtime, same one-command
spirit as everything else here.

The exact same page also works opened directly as a file
(`outputs/dashboard.html`, see below) — it detects whether a server is
present and the interactive buttons explain themselves ("needs the live
server") instead of failing silently when there isn't one.

## Run it (one command, clean machine)

```bash
pip install -r requirements.txt
python -m app.cli all
```

This ingests every source file into `state/meridian.db` (SQLite, the single
source of truth), processes `tickets.json` through the full 7-step pipeline,
exports `outputs/*.jsonl` and `audit/audit.jsonl`, writes
`outputs/entity_resolution_report.json`, and runs the PII leak scanner as a
shipping gate (non-zero exit if anything is found).

## Everyday commands

```bash
python -m app.cli run [file ...]     # process a ticket file (defaults to tickets.json); repeatable, idempotent
python -m app.cli watch [dir] [sec]  # UNATTENDED mode: poll a directory (default incoming/) for new ticket
                                      # files and process each one automatically as it appears; Ctrl+C to stop
python -m app.cli approve TKT-0027   # human approval gate for one drafted client message
python -m app.cli approve-all        # bulk approve, with a confirmation prompt
python -m app.cli explain TKT-0027   # replay the full audit trail for one ticket
python -m app.cli query "..."        # Part A: ask a natural-language question, get an answer + citations
python -m app.cli report             # regenerate outputs/entity_resolution_report.json
python -m app.cli scan               # PII leak scan (jsonl + csv + dashboard.html)
python -m app.cli dashboard          # regenerate outputs/dashboard.html
python -m app.cli export-csv         # mirror outputs/*.jsonl to outputs/*.csv for Excel/Sheets
python -m app.cli reset              # wipe state/meridian.db and start clean
```

### Unattended processing — this is not a demo that only reacts to manual commands

`python -m app.cli watch` is the literal answer to "what happens when new
tickets come in": it polls `incoming/` (or any directory you point it at)
and processes each new or changed file the moment it appears — ingest,
export, dashboard, CSV, and a full PII scan, every time — with no human
touching the CLI. New files are tracked by content hash in `watched_files`,
so a file that reappears with the same content is never reprocessed. Verified
live: started `watch`, dropped a schema-drifted ticket file into `incoming/`
mid-run, and it was picked up and processed within one poll interval — see
EVIDENCE.md for the exact numbers.

## Dashboard

`outputs/dashboard.html` — one self-contained file, no server, no CDN, no
external fonts. Open it directly (`file://...`) on the same clean machine the
one-command deploy ran on.

- **Left sidebar navigation** (Needs Attention / Pipeline Overview / All
  Tickets / Ask a Question / Process New File / Rules) — instant view
  switching, no anchor-scrolling anywhere on the page.
- **Needs Attention**: quarantined, blocked, awaiting-approval, and sent —
  each in its own always-visible panel, not buried behind a filter dropdown.
  Under the live server, "Approve & send" and "Approve all pending" act
  immediately; opened as a static file, they explain how to get the server
  running instead of failing silently.
- **Ask a Question**: the Part A query interface, live, in the browser.
- **Process New File**: drag-and-drop (or click to choose) a ticket file —
  the live path for the final-hour surprise file.
- **Click any ticket, anywhere, to open a detail drawer** that slides in over
  the current view — never requires scrolling to find. Leads with a
  plain-English paragraph anyone can read standing alone, then the actual
  client message (and, if LLM-polished, the deterministic original for
  comparison), then — for blocked tickets — every candidate truck considered
  and why each was ruled out, then a collapsed **raw source record (PII-
  redacted)** so a technical reviewer can independently check the algorithm
  against the real input, then the full technical audit trail.
- Sidebar footer shows the PII scan status and the ingestion-time redaction
  count (proof redaction did real work, not just "we found nothing").
- KPI tiles, a pipeline-state funnel with a plain-English glossary, a
  per-client chart, and the full encoded rule set with citations.

Regenerated automatically by `python -m app.cli all`, or standalone via
`python -m app.cli dashboard`.

Run the surprise file live: `python -m app.cli run path/to/surprise_file.json`
— any JSON array, JSON-lines, or CSV, with any of the field-name variants
listed in `app/adapters.py`, is accepted; unmapped/broken records are
quarantined with a reason, never a crash.

Run the test suite: `python tests/test_pipeline.py` — executes the real
pipeline against the real bundle and asserts on real output files (rerun
idempotency, no duplicate work orders, quarantine-not-crash, surprise-file
tolerance, PII scanner catches a real leak). Leaves the repo in a clean,
demoable state when it finishes.

## Optional local LLM (Part A phrasing only, never decisions)

Off by default. Default model is `qwen2.5:1.5b`, chosen by benchmarking
against what was actually installed on the target machine (16GB RAM, Intel
Iris Xe — CPU-bound inference): 23 tok/s and correctly grounded, vs. 13.7
tok/s for a 7GB model and 7.5 tok/s for a mid-size one tested alongside it —
see EVIDENCE.md "local LLM selection" for the numbers. Bigger was not
smarter-for-this-job on this hardware.

```bash
ollama pull qwen2.5:1.5b
ollama serve                       # if not already running as a service
export MERIDIAN_LLM_ENABLED=1      # Windows PowerShell: $env:MERIDIAN_LLM_ENABLED=1
# optional: export MERIDIAN_LLM_MODEL=<another pulled model> to override
```

If Ollama isn't running, every query call falls back to the deterministic
template answer automatically — the system's correctness never depends on
this being on. First call after starting Ollama can take 15-20s (cold model
load); subsequent calls are fast.

### Optional local-LLM comms polish (separate flag, off by default)

Client messages are drafted deterministically by default (`app/comms_templates.py`) —
this is what ships without a human reading it first, so it stays template-based
unless you explicitly opt in:

```bash
export MERIDIAN_LLM_POLISH_COMMS=1   # separate from MERIDIAN_LLM_ENABLED on purpose
```

When on, each drafted message is rewritten for tone by the local LLM under a
strict "don't add/remove/change any fact" instruction; both the polished and
deterministic versions are kept, and the dashboard drawer shows both when
they differ so an expert can confirm nothing drifted. Verified working
end-to-end (~7s per message on this hardware). Performance scales linearly
with ticket count, so it's meant for reviewing specific messages, not for
blanket-enabling on a full batch run — a slow client-facing draft is worse
than a slow internal answer, so this stays a deliberate, separate choice.

## GitHub, GitHub Pages & Vercel

This repo is set up to push to GitHub directly — `.gitignore` excludes
runtime state (`state/`, `incoming/*`) and Python caches; `outputs/`,
`audit/`, and `docs/` **are** committed on purpose, as evidence of a real
run, not excluded as "build artifacts." Before pushing, regenerate a clean
run so what's committed reflects a real end-to-end pass, not leftover test
state:

```bash
python -m app.cli reset && python -m app.cli all
git add -A && git commit -m "..."
git push
```

### GitHub Pages (recommended — simplest hosted, read-only link)

`python -m app.cli dashboard` (and `all`) writes the same static snapshot to
**both** `outputs/dashboard.html` and `docs/index.html`. GitHub Pages can
serve `/docs` on the default branch with zero config beyond one settings
toggle:

1. Push this repo to GitHub (see above).
2. Repo → **Settings → Pages** → Source: **Deploy from a branch** → Branch:
   `main` (or your default), folder **`/docs`** → Save.
3. GitHub gives you a `https://<user>.github.io/<repo>/` URL within a minute
   or two. That's the same read-only dashboard described above — the
   yellow "read-only snapshot" banner is expected there too, for the same
   reason: GitHub Pages serves static files, there's no Python process
   behind it, so live actions correctly refuse to run and say so.
4. To update the published snapshot after making changes: regenerate
   (`python -m app.cli all`), commit `docs/index.html`, push. Pages
   redeploys automatically.

**What Vercel can and can't do here, stated plainly:** this system's live
features (approve, resolve, ingest, ask, the background file watcher) are a
stateful Python process — a SQLite file that persists between requests and a
background thread that polls a directory continuously. Vercel's hosting
model is serverless functions: no request-to-request persistence, no
long-running background threads, an ephemeral filesystem. **The live
console cannot run on Vercel as architected, and reworking it to fit that
model (a hosted database instead of SQLite, request-triggered ingestion
instead of a background watcher, no persistent local file state) would be a
different system, not a deploy step.** Claiming otherwise would be the kind
of thing this whole project has tried not to do.

What Vercel *can* do, and what `vercel.json` here is actually set up for:
serve `outputs/dashboard.html` as a static, read-only site — exactly the
same static-snapshot mode described above, just hosted instead of opened
locally. Push to GitHub, import the repo in Vercel with no build command
(it's already a static file, `outputDirectory: outputs`), and you get a
shareable read-only link to the same dashboard, snapshotted at whatever
state was last committed. For a live, interactive, hosted version, the
right target is a host that runs a persistent process (Render, Railway,
Fly.io, a plain VM) — happy to set that up if it's actually wanted, but it's
a different deployment, not a `vercel.json` tweak.

## Architecture

```
ingest/  fleet_master.csv, drivers_roster.csv, maintenance_log.xlsx, tickets.json,
         emails/, dispatcher_interview.txt, meridian_trips.csv (rollup only)
         -> entity resolution -> PII masking/redaction -> SQLite

rules.py rules encoded as data, each with a rule_id and a citation back to
         dispatcher_interview.txt or a corroborating email thread

pipeline.py  7-step deterministic state machine per ticket, vehicle
             reservation to prevent double-assignment within a run, atomic
             export of outbox files as materialized views of the DB

query.py     deterministic retrieval (structured lookups + SQLite FTS5) is
             the source of truth; optional local LLM only phrases an answer
             from snippets already retrieved, never adds a fact

pii.py       redact() strips PII at ingestion, before it's ever stored;
             redact_and_count() builds an ingestion-time redaction audit;
             scan_paths() is an independent gate re-checked (jsonl+csv+html)
             before every run is reported complete

comms_templates.py  deterministic client-message drafting (default, ships
             without review); optional local-LLM tone polish, opt-in
             separately from the query LLM, deterministic original always
             kept alongside for comparison

server.py    the live console (Flask, local-only) -- serves the same
             generate_dashboard() template fresh from live DB state on every
             request, plus approve/ingest/query endpoints; every mutating
             endpoint re-exports jsonl+csv+dashboard and re-scans for PII
             before responding
```

See [EVIDENCE.md](EVIDENCE.md) for every material decision, why it was made,
and what was deliberately cut, mapped to the rubric.
