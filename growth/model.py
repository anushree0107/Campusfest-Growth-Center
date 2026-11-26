"""Quantitative growth model: single source of truth for the deck, README and app.

Baseline = CampusFest 2025 actuals from the provided dataset (SOURCED).
Everything marked ASSUMPTION is a judgement call, listed in README and editable here.

Funnel for one campaign c with budget B:
    identified_c(B) = identified_2025_c * (B / spend_2025_c) ** e_c        (diminishing returns)
    registered_c    = identified_c * reg_rate_c                            (observed 2025)
    attended_c      = registered_c * showup_c'                             (showup lifted by recovery programme)
    total           = 2025_attended + overlap_haircut * (sum_c attended_c + walk-ins - 2025_attended)
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

CAMPAIGN_IDS = ["C01", "C02", "C03", "C04", "C05", "C06", "C07"]

# ASSUMPTION: elasticity of identified people to spend (<1 = diminishing returns).
ELASTICITY = {"C01": 0.70, "C02": 0.80, "C03": 0.70, "C04": 0.65, "C05": 0.60, "C06": 0.50, "C07": 0.60}

# ASSUMPTION: planned 2026 allocations (INR). C08 = no-show recovery programme, RESERVE = test/contingency.
BASE_ALLOCATION = {"C01": 60000, "C02": 45000, "C03": 25000, "C04": 52000,
                   "C05": 42000, "C06": 24000, "C07": 27000, "C08": 15000}
BASE_RESERVE = 10000
STRETCH_ALLOCATION = {"C01": 72000, "C02": 48000, "C03": 25000, "C04": 68000,
                      "C05": 58000, "C06": 27000, "C07": 32000, "C08": 20000}
STRETCH_RESERVE = 10000


@dataclass
class Scenario:
    name: str
    elasticity_bump: float = 0.0     # stretch: ideas improve conversion-to-spend response
    uplift_low: float = 0.08         # show-up lift (pts) where 2025 show-up < 65%
    uplift_high: float = 0.03        # show-up lift elsewhere
    cap_low: float = 0.80
    cap_high: float = 0.92
    recovery_ref_budget: float = 15000   # budget at which the full uplift is achieved
    walk_ins: int = 125
    overlap_haircut: float = 0.10    # ASSUMPTION: 10% of incremental overlaps between ideas
    capacity: int = 3500             # ASSUMPTION: venue cap on unique external attendees
    allocation: dict = field(default_factory=dict)
    reserve: float = 0


BASE = Scenario("Base case", allocation=BASE_ALLOCATION, reserve=BASE_RESERVE)
STRETCH = Scenario("Stretch target", elasticity_bump=0.10, uplift_low=0.15, uplift_high=0.05,
                   recovery_ref_budget=20000, walk_ins=150, allocation=STRETCH_ALLOCATION,
                   reserve=STRETCH_RESERVE)


def baseline_from_conn(conn) -> tuple[pd.DataFrame, int]:
    """2025 per-campaign baseline (external people only, de-duplicated) + unattributed walk-in attendees."""
    b = pd.read_sql(
        "SELECT campaign_id, campaign_name, identified, registered, confirmed, attended, spend_inr "
        "FROM v_campaign_final ORDER BY campaign_id", conn)
    reach = pd.read_sql(
        "SELECT campaign_id, SUM(reported_reach) reach, SUM(landing_page_sessions) sessions, "
        "SUM(clicks) clicks FROM daily_marketing GROUP BY campaign_id", conn)
    b = b.merge(reach, on="campaign_id", how="left").set_index("campaign_id")
    b["reg_rate"] = b.registered / b.identified
    b["showup"] = b.attended / b.registered
    b["cpa"] = b.spend_inr / b.attended
    walk = conn.execute("SELECT COUNT(*) FROM v_attendees WHERE campaign_id IS NULL").fetchone()[0]
    return b, int(walk)


def project(baseline: pd.DataFrame, walk_2025: int, budgets: dict, scen: Scenario) -> dict:
    """Project attendance for a budget allocation."""
    rows = []
    rec_strength = min(1.0, budgets.get("C08", 0) / scen.recovery_ref_budget) if scen.recovery_ref_budget else 0
    for cid in baseline.index:
        r = baseline.loc[cid]
        b = float(budgets.get(cid, 0))
        e = min(ELASTICITY.get(cid, 0.6) + scen.elasticity_bump, 0.95)
        mult = (b / r.spend_inr) ** e if r.spend_inr > 0 and b > 0 else 0.0
        ident = r.identified * mult
        reg = ident * r.reg_rate
        low = r.showup < 0.65
        lift = (scen.uplift_low if low else scen.uplift_high) * rec_strength
        cap = scen.cap_low if low else scen.cap_high
        showup2 = max(r.showup, min(r.showup + lift, cap))
        att = reg * showup2
        rows.append({"campaign_id": cid, "budget_inr": b, "identified": ident, "registered": reg,
                     "showup": showup2, "attended": att,
                     "reach_needed": ident * (r.reach / r.identified)})
    df = pd.DataFrame(rows).set_index("campaign_id")
    df["cost_per_attendee"] = df.budget_inr / df.attended.where(df.attended > 0)
    gross = df.attended.sum() + scen.walk_ins
    base_total = baseline.attended.sum() + walk_2025
    total = base_total + (gross - base_total) * (1 - scen.overlap_haircut)
    total = min(total, scen.capacity)
    spend = sum(budgets.values()) + scen.reserve
    return {"table": df, "gross": gross, "total": total, "baseline_total": base_total,
            "total_budget": spend, "uplift_pct": total / base_total - 1,
            "blended_cost_per_attendee": spend / total}


def scenario_summary(baseline, walk_2025) -> pd.DataFrame:
    out = []
    for s in (BASE, STRETCH):
        p = project(baseline, walk_2025, s.allocation, s)
        t = p["table"]
        out.append({"scenario": s.name, "budget_inr": p["total_budget"],
                    "identified": t.identified.sum(), "registered": t.registered.sum(),
                    "attended_campaigns": t.attended.sum(), "walk_ins": s.walk_ins,
                    "attendees": p["total"], "vs_2025_pct": p["uplift_pct"],
                    "cost_per_attendee": p["blended_cost_per_attendee"],
                    "reach_needed": t.reach_needed.sum()})
    return pd.DataFrame(out)


def base_goals_by_campaign(conn) -> dict:
    b, w = baseline_from_conn(conn)
    t = project(b, w, BASE.allocation, BASE)["table"]
    return t.attended.to_dict()


def idea_contributions(baseline, walk_2025, scen: Scenario = BASE) -> dict:
    """Incremental attendees by idea vs 2025 baseline (before overlap haircut)."""
    no_rec = dict(scen.allocation)
    no_rec["C08"] = 0
    p_no = project(baseline, walk_2025, no_rec, scen)["table"]
    p_full = project(baseline, walk_2025, scen.allocation, scen)["table"]
    inc = (p_no.attended - baseline.attended).to_dict()
    rec = float(p_full.attended.sum() - p_no.attended.sum())
    return {"C01": inc["C01"], "C02": inc["C02"], "C03": inc["C03"], "C04": inc["C04"],
            "C05": inc["C05"], "C06": inc["C06"], "C07": inc["C07"], "recovery": rec}


# ----------------------------------------------------------------------------------------
# Strategy content (ideas) -- numbers are computed from the model at seed time.
# ----------------------------------------------------------------------------------------
IDEA_DEFS = [
    dict(idea_id="I1", name="Referral engine: invite-a-friend with team rewards",
         concept="Every registrant gets a personal link; both inviter and friend unlock perks (queue-skip, merch) when the friend shows up. Student Referrals had the lowest cost per attendee (Rs 69) and 79% show-up but only 19k spend.",
         cids=["C05"], confidence=0.70, effort=3, budget_key="C05",
         start="T-10w", end="T-0", kpi="Invites per registrant, invite-to-registration %, cost per attendee",
         scale_rule="Invites/registrant >= 0.8, invite-to-registration >= 20% and cost per attendee <= Rs 100 for 7 days -> +25% budget",
         change_rule="Invites/registrant < 0.5 after 10 days -> change reward (not the channel)",
         stop_rule="Cost per attendee > Rs 180 for 7 days or invite-to-registration < 8% -> stop"),
    dict(idea_id="I2", name="College partnership blitz: group registration with club leads",
         concept="Sign 40+ clubs/colleges as partner groups with a shared registration link and group perks. College Partnerships had 86% show-up (best) at Rs 81 per attendee, because group commitment drives attendance.",
         cids=["C04"], confidence=0.70, effort=4, budget_key="C04",
         start="T-11w", end="T-1w", kpi="Partner groups signed, registrations per group, show-up %",
         scale_rule="Registrations per group >= 12 and show-up >= 80% -> add 10 more groups",
         change_rule="Registrations per group < 8 by T-5w -> shift to group-leader incentives",
         stop_rule="Cost per attendee > Rs 160 for 10 days -> stop new group onboarding"),
    dict(idea_id="I3", name="No-show recovery: reminders, waitlist back-fill, cancel-and-rebook",
         concept="Paid Social and Creators registrants show up only 50% of the time versus 75-86% elsewhere. A WhatsApp/SMS reminder ladder (T-7, T-2, T-0), calendar add, and waitlist back-fill converts existing registrants at a fraction of acquisition cost.",
         cids=["C08"], confidence=0.80, effort=2, budget_key="C08",
         start="T-4w", end="T-0", kpi="Confirmed-to-attended %, reminder click rate, waitlist fill rate",
         scale_rule="Reminder cohort show-up >= control + 8 pts -> roll to all channels",
         change_rule="Lift < 3 pts after first 500 sends -> change message/timing",
         stop_rule="Lift <= 0 after 1,000 sends -> stop and reallocate"),
    dict(idea_id="I4", name="Ambassador programme scale-up with tiered rewards",
         concept="Campus Ambassadors delivered the largest attendee volume among efficient channels (390 at Rs 108). Add tiers and a weekly leaderboard so the top 25 ambassadors each bring 15+ registrations.",
         cids=["C01"], confidence=0.75, effort=3, budget_key="C01",
         start="T-12w", end="T-0", kpi="Active ambassadors, registrations per active ambassador",
         scale_rule="Registrations per active ambassador >= 10 -> recruit another 20",
         change_rule="Active ambassador rate < 50% -> change onboarding and rewards",
         stop_rule="Cost per attendee > Rs 160 for 10 days -> cap programme"),
    dict(idea_id="I5", name="Organic search and community groups (always-on, low-cost)",
         concept="Organic Search (Rs 72) and Community Groups (Rs 97, 82% show-up) are cheap but small. Publish festival content for student search intent and seed WhatsApp/Discord communities.",
         cids=["C06", "C07"], confidence=0.60, effort=3, budget_key=("C06", "C07"),
         start="T-12w", end="T-0", kpi="Organic sessions, community joins, registration rate",
         scale_rule="Registration starts per session >= 30% -> add content/communities",
         change_rule="Sessions flat for 14 days -> change topics and distribution",
         stop_rule="Cost per attendee > Rs 150 for 14 days -> pause paid support"),
]

REJECTED = [
    dict(idea_id="R1", name="REJECT: scale Paid Social at current efficiency",
         concept="Cost per attendee Rs 274 and 50% show-up. Keep a smaller budget for retargeting only.",
         cids=["C02"], decision="reject"),
    dict(idea_id="R2", name="REJECT: more creator sponsorships at flat fees",
         concept="Highest cost per attendee (Rs 337), 50% show-up. Only revisit with pay-per-registration deals.",
         cids=["C03"], decision="reject"),
    dict(idea_id="R3", name="AVOID: buy more reach / impressions",
         concept="Reach is anonymous and not unique people; summed reach does not prove incremental attendance. Optimize for attended people, not reach.",
         cids=[], decision="reject"),
]


def make_ideas(conn) -> list[dict]:
    b, w = baseline_from_conn(conn)
    contrib = idea_contributions(b, w)
    rows = []
    for d in IDEA_DEFS:
        if d["idea_id"] == "I3":
            exp = contrib["recovery"]
            reach = int(round(b.registered.sum() * 0.5))  # registrants exposed to reminders (half are low show-up)
        else:
            exp = sum(contrib[c] for c in d["cids"])
            reach = int(round(sum(project(b, w, BASE.allocation, BASE)["table"].loc[c, "identified"] - b.loc[c, "identified"]
                                  for c in d["cids"])))
        bk = d["budget_key"]
        budget = sum(BASE_ALLOCATION[k] for k in (bk if isinstance(bk, tuple) else (bk,)))
        impact = round(exp / reach, 3) if reach > 0 else 0.0
        rows.append({**d, "reach": reach, "impact": impact, "expected_attendees": int(round(exp)),
                     "budget_inr": budget, "campaign_id": ",".join(d["cids"]),
                     "score": exp * d["confidence"] / d["effort"], "decision": "invest",
                     "start_date": d["start"], "end_date": d["end"],
                     "scale_rule": d["scale_rule"], "change_rule": d["change_rule"],
                     "stop_rule": d["stop_rule"]})
    rows.sort(key=lambda r: -r["score"])
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    for d in REJECTED:
        paid_loss = contrib[d["cids"][0]] if d["cids"] else 0
        rows.append({**d, "rank": None, "reach": 0, "impact": 0.0, "confidence": 0.0, "effort": 0,
                     "budget_inr": 0, "expected_attendees": int(round(paid_loss)),
                     "campaign_id": ",".join(d["cids"]), "kpi": "", "scale_rule": "",
                     "change_rule": "", "stop_rule": "", "start_date": "", "end_date": "", "score": 0})
    return rows


IDEAS: list = []  # filled by seed_app_state via make_ideas(conn)

# Starter tasks: due dates are offsets (days) from the simulated "plan date" -- see app.
STARTER_TASKS = []


def build_starter_tasks(plan_date: pd.Timestamp) -> list[dict]:
    def d(n):
        return (plan_date + pd.Timedelta(days=n)).strftime("%Y-%m-%d")
    return [
        dict(title="Launch referral links in registration confirmation email", description="Personal link per registrant; track invites per registrant.", campaign_id="C05", idea_id="I1", owner="Dev Malhotra", due_date=d(7), priority="high", status="in_progress"),
        dict(title="Sign first 15 partner colleges/clubs", description="Group registration link and group perks; target 12 registrations per group.", campaign_id="C04", idea_id="I2", owner="Meera Iyer", due_date=d(14), priority="high", status="todo"),
        dict(title="Build WhatsApp reminder ladder (T-7, T-2, T-0)", description="Template approval, opt-in handling, control group of 10%.", campaign_id="C08", idea_id="I3", owner="Dev Malhotra", due_date=d(35), priority="medium", status="todo"),
        dict(title="Recruit 60 ambassadors and set tier rewards", description="Weekly leaderboard; track active rate.", campaign_id="C01", idea_id="I4", owner="Meera Iyer", due_date=d(10), priority="high", status="todo"),
        dict(title="Publish 8 search-intent pages for the festival", description="Student queries: fests in Bengaluru, college fest tickets.", campaign_id="C06", idea_id="I5", owner="Arjun Verma", due_date=d(21), priority="medium", status="todo"),
        dict(title="Cut Paid Social to retargeting-only budget (Rs 45k)", description="Re-point spend to registrants who started but did not complete.", campaign_id="C02", idea_id="R1", owner="Arjun Verma", due_date=d(5), priority="high", status="todo"),
        dict(title="Renegotiate creator deals to pay-per-registration", description="Replace flat fees; drop creators with show-up < 55%.", campaign_id="C03", idea_id="R2", owner="Nisha Rao", due_date=d(12), priority="medium", status="todo"),
        dict(title="Weekly funnel review: scale/change/stop decisions", description="Use dashboard flags; log decisions.", campaign_id=None, idea_id=None, owner="Growth lead", due_date=d(7), priority="medium", status="todo"),
    ]
