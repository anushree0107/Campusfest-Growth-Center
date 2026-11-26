"""Rule-based diagnosis: turns the live metrics into insight cards with a suggested action.
Everything is computed from the database (no hardcoded numbers)."""
from __future__ import annotations

import pandas as pd

from . import metrics as M


def _inr(x):
    return f"Rs {x:,.0f}"


def generate(conn, as_of) -> list[dict]:
    d = str(pd.Timestamp(as_of).date())
    df = M.campaign_funnel(conn, d)
    camps = df.drop(index="UNATTR", errors="ignore")
    tot = M.totals(df)
    ov = M.overview(conn, d)
    flags = M.flags(conn, d, df)
    bn = M.bottlenecks(df)
    mom = M.momentum(conn, d, 7)
    ph = M.phase(conn, d)
    out: list[dict] = []

    # 1. gaps to goal
    for label, key, goal in (("Registrations", "registered", "registration_goal"),
                             ("Identified people", "identified", "identified_goal")):
        pct = ov[key] / ov[goal] if ov[goal] else None
        if pct is not None and pct < 0.9:
            out.append({"severity": "high" if pct < 0.75 else "medium", "campaign_id": None,
                        "title": f"{label} at {pct:.0%} of goal" + (f" with {(M.event_window(conn)[0].normalize() - pd.Timestamp(d)).days} days to the event" if ph == "pre-event" else ""),
                        "detail": f"{ov[key]:,} vs goal {ov[goal]:,} (target version: {ov['target_version']}). Gap {ov[goal] - ov[key]:,}."
                                  + (" Goals are end-state numbers, so judge the pace against the registration curve, not the raw percentage." if ph == "pre-event" else ""),
                        "task": {"title": f"Close the {label.lower()} gap ({ov[goal] - ov[key]:,} short)",
                                 "description": f"At {d}, {label.lower()} are {ov[key]:,} vs goal {ov[goal]:,}. Review scale/change/stop decisions and reallocate budget.",
                                 "campaign_id": None, "priority": "high"}})
    if ph == "post-event" or tot.attended > 0:
        pct = ov["attended_unique_visitors"] / ov["visitors_goal"]
        if pct < 1:
            out.append({"severity": "medium", "campaign_id": None,
                        "title": f"Unique visitors at {pct:.0%} of goal",
                        "detail": f"{ov['attended_unique_visitors']:,} vs {ov['visitors_goal']:,} (unique external people, admitted and verified).",
                        "task": None})
    else:
        pr = M.projection(conn, d)
        pct = pr["projected_attendees"] / ov["visitors_goal"]
        out.append({"severity": "high" if pct < 0.85 else "low", "campaign_id": None,
                    "title": f"Projected attendees {pr['projected_attendees']:,} = {pct:.0%} of the {ov['visitors_goal']:,} goal",
                    "detail": f"Run-rate method (assumption-based): {pr['registrations_per_day_last14']}/day registrations over {pr['days_to_event']} days, "
                              f"x confirmed ratio {pr['confirmed_ratio']:.0%} x ASSUMED show-up {pr['assumed_showup']:.0%}. Naive extrapolation; treat as a range, not a forecast.",
                    "task": None})

    # 2. spend vs outcome share
    if camps.spend.sum() > 0 and camps.attended.sum() > 0:
        for cid, r in camps.iterrows():
            ss, as_ = r.spend / camps.spend.sum(), r.attended / camps.attended.sum()
            if ss - as_ > 0.12:
                out.append({"severity": "high", "campaign_id": cid,
                            "title": f"{r.campaign_name}: {ss:.0%} of spend but {as_:.0%} of attendees",
                            "detail": f"Cost per attendee {_inr(r.cost_per_attendee)} vs portfolio {_inr(tot.cost_per_attendee)}; show-up {r.showup_rate:.0%}.",
                            "task": {"title": f"Reduce or restructure {r.campaign_name} spend",
                                     "description": f"{r.campaign_name} takes {ss:.0%} of spend and returns {as_:.0%} of attendees (cost per attendee {_inr(r.cost_per_attendee)}). Cut budget or move to pay-per-registration; shift savings to SCALE campaigns.",
                                     "campaign_id": cid, "priority": "high"}})

    # 3. scale candidates
    for cid in flags.index[flags.decision == "SCALE"]:
        r = camps.loc[cid]
        cost = r.cost_per_attendee if r.attended > 0 else r.cost_per_confirmed
        unit = "attendee" if r.attended > 0 else "confirmed registration"
        out.append({"severity": "opportunity", "campaign_id": cid,
                    "title": f"Scale candidate: {r.campaign_name}",
                    "detail": flags.loc[cid, "reason"] + f" Only {r.spend / max(camps.spend.sum(), 1):.0%} of total spend.",
                    "task": {"title": f"Increase budget for {r.campaign_name}",
                             "description": f"Flag = SCALE (cost per {unit} {_inr(cost)}). Add budget in 10-15% steps and watch whether cost per {unit} stays under the scale threshold (diminishing returns).",
                             "campaign_id": cid, "priority": "medium"}})

    # 4. bottlenecks for CHANGE/STOP campaigns
    for cid in flags.index[flags.decision.isin(["CHANGE", "STOP"])]:
        b = bn.loc[cid]
        if b.bottleneck_stage not in ("No material bottleneck", "n/a"):
            out.append({"severity": "high" if flags.loc[cid, "decision"] == "STOP" else "medium", "campaign_id": cid,
                        "title": f"{camps.loc[cid, 'campaign_name']} ({flags.loc[cid, 'decision']}): bottleneck at '{b.bottleneck_stage}'",
                        "detail": f"Stage rate {b.stage_rate:.0%}, {b.gap_vs_median:.0%} below the portfolio median. {flags.loc[cid, 'reason']}",
                        "task": {"title": f"Fix '{b.bottleneck_stage}' for {camps.loc[cid, 'campaign_name']}",
                                 "description": f"Stage rate {b.stage_rate:.0%} is {b.gap_vs_median:.0%} below median. Run a reminder/nurture test on this cohort and measure the lift vs a holdout.",
                                 "campaign_id": cid, "priority": "high"}})

    # 5. momentum
    for cid, r in mom.iterrows():
        if ph != "pre-event":
            break   # registrations close at the event; week-on-week drops are expected, not a signal
        if cid in camps.index and r.regs_prev >= 15 and r.regs_change_pct is not None and r.regs_change_pct == r.regs_change_pct and r.regs_change_pct <= -0.3:
            out.append({"severity": "medium", "campaign_id": cid,
                        "title": f"{camps.loc[cid, 'campaign_name']}: registrations down {abs(r.regs_change_pct):.0%} week on week",
                        "detail": f"{int(r.regs_now)} vs {int(r.regs_prev)} registrations; spend {_inr(r.spend_now)} vs {_inr(r.spend_prev)}.",
                        "task": {"title": f"Investigate registration drop in {camps.loc[cid, 'campaign_name']}",
                                 "description": f"Registrations fell from {int(r.regs_prev)} to {int(r.regs_now)} week on week. Check creative fatigue, spend pause, and landing page.",
                                 "campaign_id": cid, "priority": "medium"}})
    order = {"high": 0, "medium": 1, "opportunity": 2, "low": 3}
    out.sort(key=lambda x: order.get(x["severity"], 9))
    return out
