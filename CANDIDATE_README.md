# Meridian Freight: Candidate README
### Everything runs on your machine. Files in, files out. No servers, no accounts, nothing to set up beyond your own stack.

## Your inputs (all in this bundle)
- `tickets.json`: the breakdown queue your pipeline processes. Contains duplicate ticket ids and broken records, by design.
- `fleet_master.csv`, `meridian_trips.csv`, `maintenance_log.xlsx`, `drivers_roster.csv`, `emails/` (40 threads), `dispatcher_interview.txt`: the context your decisions must be grounded in. Duplicates, conflicts, mixed formats, and mixed languages are the client's data quality, not ours.

## Your outputs (standardized so every candidate is judged the same way)
Create an `outputs/` directory:
- `outputs/work_orders.jsonl`, one line per work order:
  `{"work_order_id", "ticket_id", "vehicle_reg", "created_at", "citations": [...]}`
  Exactly one per unique valid ticket, no matter how many times it appears in the queue.
- `outputs/comms_pending.jsonl`: drafted client messages awaiting human approval, showing the approver the full context and citations.
- `outputs/comms_sent.jsonl`: written ONLY after a human approves (a CLI prompt is fine):
  `{"message_id", "ticket_id", "recipient", "body", "approved_by", "sent_at"}`
  Exactly one per approved ticket. No personal data in any body, ever.
- `outputs/quarantine.jsonl`: broken records, each with the reason they were quarantined.
- `audit/audit.jsonl`: one line per step per ticket: what was decided, on what data, under which rule.

## The three rules that decide this challenge
1. Duplicates are processed exactly once. Broken records are quarantined with an alert, never dropped silently, never a crash.
2. Running your entire pipeline twice, back to back, produces identical outputs. Nothing doubled, nothing lost.
3. In the final hour you receive a second, smaller ticket file that will not look exactly like the main queue. Handle it live.

## Practical
- One documented command starts your whole system on a clean machine.
- Any stack, any LLM (bring your own key; free tiers: Google AI Studio, Groq). AI assistance allowed. You must be able to explain every line.
- Commit to your repo at hour 3 (entity resolution report) and hour 5 (pipeline processing the queue end to end).
