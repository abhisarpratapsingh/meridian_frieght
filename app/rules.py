"""The dispatcher's 14 years of tribal knowledge, structured as data.

Every rule has an id, a plain-English statement, and a citation back to
dispatcher_interview.txt (or the corroborating email thread). The pipeline
never hard-codes "if season == winter" inline without going through one of
these — that's what makes a decision citable in the audit log and in the
`explain` command.

Two rule categories:
  - "eligibility": pass/fail gates a candidate replacement vehicle must clear.
  - "data_precedence": how conflicting facts from different sources are resolved
    at ingestion time (used by resolve.py, cited in facts.note).
  - "client_policy": per-client rules that shape comms drafting and dispatch
    choices, not raw vehicle eligibility.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta

RULES = [
    {
        "rule_id": "R-01", "category": "eligibility",
        "statement": "Oct-Feb, no BS4 vehicle on any route touching Delhi/Gurgaon/Faridabad/Noida (NCR winter pollution restriction). BS6 only.",
        "source_citation": "dispatcher_interview.txt:14", "precedence": 10,
    },
    {
        "rule_id": "R-02", "category": "eligibility",
        "statement": "Nov-Feb, hill routes (Rudrapur and further toward Nainital) require an engine heater.",
        "source_citation": "dispatcher_interview.txt:18", "precedence": 10,
    },
    {
        "rule_id": "R-03", "category": "eligibility",
        "statement": "No vehicle with brake work in the last 30 days goes on a hill route.",
        "source_citation": "dispatcher_interview.txt:18", "precedence": 10,
    },
    {
        "rule_id": "R-04", "category": "client_policy",
        "statement": "Shakti Cement: effective SLA is 36 hours door-to-door, not the 48-hour contract figure. Plan and communicate to 36.",
        "source_citation": "dispatcher_interview.txt:22; emails/thread_01_shakti_sla.txt", "precedence": 5,
    },
    {
        "rule_id": "R-05", "category": "client_policy",
        "statement": "Vertex Retail: Ludhiana warehouse gate closes at 18:00 sharp. A delivery that would arrive after 18:00 is held and delivered at 08:00 next morning, coordinator informed the evening before. This is a scheduled morning delivery, never a failed delivery.",
        "source_citation": "dispatcher_interview.txt:24", "precedence": 5,
    },
    {
        "rule_id": "R-06", "category": "client_policy",
        "statement": "Apex Chemicals: a vehicle involved in any issue (breakdown, late arrival) on an Apex run does not go back to Apex on the very next dispatch. Rotate a different vehicle at least once in between.",
        "source_citation": "dispatcher_interview.txt:26", "precedence": 5,
    },
    {
        "rule_id": "R-07", "category": "eligibility",
        "statement": "Orion Pharma: only vehicles model year 2020 or later are dispatched (pharma audit / RC check requirement). Loads never wait unrefrigerated overnight at a hub.",
        "source_citation": "dispatcher_interview.txt:28", "precedence": 8,
    },
    {
        "rule_id": "R-08", "category": "client_policy",
        "statement": "Jul-Sep monsoon, routes east of Lucknow: add 20% to the computed ETA minimum; never quote the unpadded SLA to the client.",
        "source_citation": "dispatcher_interview.txt:32", "precedence": 5,
    },
    {
        "rule_id": "R-09", "category": "sourcing",
        "statement": "Breakdown within 50km of its origin hub: replacement is sourced from the ORIGIN hub, never 'nearest hub', even if a nearer hub exists — origin-hub trucks beyond 50km are kept free for premium client dispatch. Beyond 50km: nearest hub with an eligible vehicle.",
        "source_citation": "dispatcher_interview.txt:36", "precedence": 1,
    },
    {
        "rule_id": "R-10", "category": "eligibility",
        "statement": "Any vehicle more than 30 days past its due service date is grounded. No exceptions, including emergencies.",
        "source_citation": "dispatcher_interview.txt:38", "precedence": 1,
    },
    {
        "rule_id": "R-11", "category": "eligibility",
        "statement": "A vehicle patched with a temporary roadside fix (\"jugaad\") must receive a permanent repair within 7 days of the patch; until then it does not leave its home region.",
        "source_citation": "dispatcher_interview.txt:42; emails/thread_25_internal_jugaad.txt", "precedence": 3,
    },
    {
        "rule_id": "R-12", "category": "driver_policy",
        "statement": "Drivers with less than 6 months tenure never run solo at night; pair them or use a day dispatch.",
        "source_citation": "dispatcher_interview.txt:46", "precedence": 5,
    },
    {
        "rule_id": "P-01", "category": "data_precedence",
        "statement": "Vehicle model year: fleet_master.csv (verified against RC) is the system of record. An unverified hub-email claim about a vehicle's year does not override it.",
        "source_citation": "emails/thread_21_internal_yearconflict.txt", "precedence": 1,
    },
    {
        "rule_id": "P-02", "category": "data_precedence",
        "statement": "Vehicle odometer: the workshop maintenance-log reading is authoritative over an informal hub 'yard check' claim when the two conflict.",
        "source_citation": "emails/thread_22_internal_odoconflict.txt", "precedence": 1,
    },
    {
        "rule_id": "P-03", "category": "data_precedence",
        "statement": "Duplicate ticket_id within or across queue files: the first occurrence encountered (stable file order) is the winner and is processed; later occurrences are logged, never reprocessed, never silently discarded. If a later occurrence's content conflicts with the winner, the conflict is recorded, not resolved by guessing.",
        "source_citation": "CANDIDATE_README.md; Synq_AI_Forward_Deployment_Challenge.pdf (exactly-once processing rule)", "precedence": 1,
    },
]

RULES_BY_ID = {r["rule_id"]: r for r in RULES}


@dataclass
class RuleContext:
    ticket_date: datetime
    origin_hub: str
    destination: str
    client: str
    km_from_origin: float
    vehicle: dict           # reg_canonical, bs_stage, engine_heater, year, home_hub, status
    maintenance_history: list = field(default_factory=list)   # list of maintenance_events rows for this vehicle
    driver_joining_date: datetime = None
    is_night: bool = False


@dataclass
class RuleResult:
    rule_id: str
    passed: bool
    explanation: str


def _touches_ncr(hub: str, dest: str) -> bool:
    from app.normalize import NCR_HUBS
    return hub in NCR_HUBS or dest in NCR_HUBS


def _is_hill_route(hub: str, dest: str) -> bool:
    from app.normalize import HILL_HUBS
    return hub in HILL_HUBS or dest in HILL_HUBS


def _last_brake_work(history) -> datetime:
    dates = [h["event_date"] for h in history if h.get("is_brake_work")]
    return max(dates) if dates else None


def _last_jugaad(history) -> dict:
    jugaad_events = [h for h in history if h.get("is_jugaad")]
    if not jugaad_events:
        return None
    return max(jugaad_events, key=lambda h: h["event_date"])


def _last_service(history) -> datetime:
    dates = [h["event_date"] for h in history]
    return max(dates) if dates else None


# ASSUMPTION-01 (see EVIDENCE.md): no explicit "next service due date" field
# exists anywhere in the bundle (fleet_master has no due-date column; maintenance
# log gives event dates + a km-based "next check" in free text, but no live
# odometer feed exists to compare against). We use a documented proxy: a vehicle
# with no maintenance-log touchpoint in the last 90 days as of the ticket date is
# treated as overdue. This is explicit, cited, and swappable for a real due-date
# field if one becomes available.
OVERDUE_GRACE_DAYS = 90
JUGAAD_DEADLINE_DAYS = 7
BRAKE_HOLD_DAYS = 30


def evaluate_eligibility(ctx: RuleContext) -> list:
    """Run every eligibility rule against a candidate vehicle. Returns a list
    of RuleResult; caller decides pass/fail (all must pass)."""
    results = []
    v = ctx.vehicle
    month = ctx.ticket_date.month

    # R-01: winter NCR BS4 ban (Oct-Feb)
    if month in (10, 11, 12, 1, 2) and _touches_ncr(ctx.origin_hub, ctx.destination):
        ok = v.get("bs_stage") == "BS6"
        results.append(RuleResult("R-01", ok,
            f"Winter NCR route ({ctx.origin_hub}->{ctx.destination}, month {month}): requires BS6, vehicle is {v.get('bs_stage')}."))

    # R-02: hill route engine heater (Nov-Feb)
    if month in (11, 12, 1, 2) and _is_hill_route(ctx.origin_hub, ctx.destination):
        ok = v.get("engine_heater") == "Yes"
        results.append(RuleResult("R-02", ok,
            f"Winter hill route: requires engine heater, vehicle engine_heater={v.get('engine_heater')}."))

    # R-03: hill route brake-work hold (30 days)
    if _is_hill_route(ctx.origin_hub, ctx.destination):
        last_brake = _last_brake_work(ctx.maintenance_history)
        if last_brake is not None:
            days_since = (ctx.ticket_date - last_brake).days
            ok = days_since >= BRAKE_HOLD_DAYS
            results.append(RuleResult("R-03", ok,
                f"Hill route: last brake work {days_since}d ago (need >= {BRAKE_HOLD_DAYS}d)."))

    # R-07: Orion Pharma model-year floor
    if ctx.client == "Orion Pharma":
        ok = (v.get("year") or 0) >= 2020
        results.append(RuleResult("R-07", ok,
            f"Orion Pharma requires 2020+ vehicle, this one is {v.get('year')}."))

    # R-10: overdue service grounding
    last_service = _last_service(ctx.maintenance_history)
    if last_service is not None:
        days_since = (ctx.ticket_date - last_service).days
        ok = days_since <= OVERDUE_GRACE_DAYS
        results.append(RuleResult("R-10", ok,
            f"Last maintenance touchpoint {days_since}d ago (grounded if > {OVERDUE_GRACE_DAYS}d, ASSUMPTION-01)."))
    else:
        results.append(RuleResult("R-10", False, "No maintenance history at all for this vehicle: cannot confirm not-overdue, failing closed."))

    # R-11: jugaad 7-day hold, restricted to home region
    last_jugaad = _last_jugaad(ctx.maintenance_history)
    if last_jugaad is not None:
        days_since = (ctx.ticket_date - last_jugaad["event_date"]).days
        if days_since < JUGAAD_DEADLINE_DAYS:
            leaving_home_region = ctx.origin_hub != v.get("home_hub") or ctx.destination != v.get("home_hub")
            ok = not leaving_home_region
            results.append(RuleResult("R-11", ok,
                f"Vehicle under jugaad hold ({days_since}d of 7): restricted to home region {v.get('home_hub')}."))

    return results


def eligibility_passed(results: list) -> bool:
    return all(r.passed for r in results)
