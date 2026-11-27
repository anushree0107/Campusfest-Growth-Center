"""Metric definitions. Every function takes an `as_of` date (end of that day, Asia/Kolkata)
so that no later outcome leaks into an earlier snapshot.

Definitions
  identified  : external people (person_type='external') with first_seen_at <= as_of, by first-touch campaign
  registered  : registrations of external people with registered_at <= as_of, by registration campaign
  confirmed   : registered whose latest status version (effective_at <= as_of) is 'confirmed'
  attended    : UNIQUE external persons with a check-in result='admitted' AND verification='verified'
                (occurred_at <= as_of); attributed to the check-in's registration campaign
  walk-ins    : attended persons whose check-in has no registration/campaign (unattributed)
  spend       : sum of marketing_spend_inr (net of ledger credits) on activity_date <= as_of
"""
from __future__ import annotations

import pandas as pd

STAGES = [
    ("session_to_start", "Landing session -> registration start"),
    ("start_to_registered", "Registration start -> registered person"),
    ("identified_to_registered", "Identified -> registered"),
    ("registered_to_confirmed", "Registered -> confirmed"),
    ("confirmed_to_attended", "Confirmed -> attended"),
]


def cut(as_of) -> str:
    return pd.Timestamp(as_of).strftime("%Y-%m-%d") + " 23:59:59"


def event_window(conn) -> tuple[pd.Timestamp, pd.Timestamp]:
    r = conn.execute("SELECT event_starts_at, event_ends_at FROM event").fetchone()
    return pd.Timestamp(r[0]), pd.Timestamp(r[1])


def history_range(conn) -> tuple[pd.Timestamp, pd.Timestamp]:
    r = conn.execute("SELECT history_start, history_end FROM event").fetchone()
    return pd.Timestamp(r[0]), pd.Timestamp(r[1])


def phase(conn, as_of) -> str:
    start, end = event_window(conn)
    d = pd.Timestamp(as_of)
    if d < start.normalize():
        return "pre-event"
    if d <= end.normalize():
        return "event"
    return "post-event"


def _q(conn, sql, params=()):
    return pd.read_sql(sql, conn, params=params)


def campaign_funnel(conn, as_of) -> pd.DataFrame:
    """Per-campaign funnel + marketing metrics as at end of `as_of`. Index = campaign_id (+ 'UNATTR')."""
    c = cut(as_of)
    camps = _q(conn, "SELECT campaign_id, campaign_name, channel, owner_name FROM campaigns ORDER BY campaign_id")
    df = camps.set_index("campaign_id")

    ident = _q(conn, """SELECT COALESCE(first_campaign_id,'UNATTR') campaign_id, COUNT(*) identified
                        FROM people WHERE person_type='external' AND first_seen_at<=? GROUP BY 1""", (c,))
    reg = _q(conn, """
        WITH r AS (
          SELECT r.registration_id, r.campaign_id,
                 COALESCE((SELECT h.status FROM reg_history h
                           WHERE h.registration_id=r.registration_id AND h.effective_at<=?
                           ORDER BY h.effective_at DESC, h.history_id DESC LIMIT 1),'interested') AS status
          FROM registrations r JOIN people p ON p.person_id=r.person_id
          WHERE p.person_type='external' AND r.registered_at<=?)
        SELECT campaign_id, COUNT(*) registered,
               SUM(status='confirmed') confirmed, SUM(status='cancelled') cancelled,
               SUM(status='waitlisted') waitlisted, SUM(status='interested') interested_only
        FROM r GROUP BY campaign_id""", (c, c))
    att = _q(conn, """
        SELECT COALESCE(campaign_id,'UNATTR') campaign_id, COUNT(*) attended FROM (
          SELECT ck.person_id, MAX(ck.campaign_id) campaign_id
          FROM checkins ck JOIN people p ON p.person_id=ck.person_id
          WHERE p.person_type='external' AND ck.result='admitted' AND ck.verification='verified'
                AND ck.occurred_at<=? GROUP BY ck.person_id)
        GROUP BY 1""", (c,))
    mk = _q(conn, """SELECT campaign_id, SUM(impressions) impressions, SUM(clicks) clicks,
                     SUM(reported_reach) reach, SUM(landing_page_sessions) sessions,
                     SUM(registration_starts) reg_starts, SUM(marketing_spend_inr) spend
                     FROM daily_marketing WHERE activity_date<=? GROUP BY campaign_id""", (c,))
    for part in (ident, reg, att, mk):
        df = df.join(part.set_index("campaign_id"), how="outer")
    df["campaign_name"] = df["campaign_name"].fillna("Walk-in / unattributed")
    num = [c_ for c_ in df.columns if c_ not in ("campaign_name", "channel", "owner_name")]
    df[num] = df[num].astype(float).fillna(0.0)
    if "UNATTR" in df.index and df.loc["UNATTR", ["identified", "registered", "attended"]].sum() == 0:
        df = df.drop(index="UNATTR")
    df = _derive(df)
    return df


def _div(a, b):
    return a / b.where(b > 0)


def _derive(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["reg_rate"] = _div(df.registered, df.identified)
    df["confirm_rate"] = _div(df.confirmed, df.registered)
    df["cancel_rate"] = _div(df.cancelled, df.registered)
    df["showup_rate"] = _div(df.attended, df.registered)
    df["attended_per_confirmed"] = _div(df.attended, df.confirmed)
    df["session_to_start"] = _div(df.reg_starts, df.sessions)
    df["start_to_registered"] = _div(df.registered, df.reg_starts)
    df["identified_to_registered"] = df.reg_rate
    df["registered_to_confirmed"] = df.confirm_rate
    df["confirmed_to_attended"] = df.attended_per_confirmed
    df["cost_per_identified"] = _div(df.spend, df.identified)
    df["cost_per_registration"] = _div(df.spend, df.registered)
    df["cost_per_confirmed"] = _div(df.spend, df.confirmed)
    df["cost_per_attendee"] = _div(df.spend, df.attended)
    df["ctr"] = _div(df.clicks, df.impressions)
    return df


def totals(df: pd.DataFrame) -> pd.Series:
    cols = ["identified", "registered", "confirmed", "cancelled", "waitlisted", "attended",
            "impressions", "clicks", "reach", "sessions", "reg_starts", "spend"]
    t = df[cols].sum()
    t2 = _derive(pd.DataFrame([t]))
    return t2.iloc[0]


def targets_as_of(conn, as_of) -> dict:
    r = conn.execute("SELECT * FROM targets WHERE effective_at<=? ORDER BY effective_at DESC LIMIT 1",
                     (cut(as_of),)).fetchone()
    if r is None:
        r = conn.execute("SELECT * FROM targets ORDER BY effective_at LIMIT 1").fetchone()
        return {**dict(r), "note": "Before first target version; showing the launch plan."}
    return dict(r)


def overview(conn, as_of) -> dict:
    df = campaign_funnel(conn, as_of)
    t = totals(df)
    tg = targets_as_of(conn, as_of)
    out = {
        "as_of": str(pd.Timestamp(as_of).date()), "phase": phase(conn, as_of),
        "target_version": tg["target_version"],
        "identified": int(t.identified), "identified_goal": tg["identified_external_people_goal"],
        "registered": int(t.registered), "registration_goal": tg["registration_goal"],
        "confirmed": int(t.confirmed), "cancelled": int(t.cancelled), "waitlisted": int(t.waitlisted),
        "attended_unique_visitors": int(t.attended), "visitors_goal": tg["unique_external_visitors_goal"],
        "spend_inr": float(t.spend), "budget_inr": tg["marketing_budget_inr"],
    }
    for k, g in (("identified", "identified_goal"), ("registered", "registration_goal"),
                 ("attended_unique_visitors", "visitors_goal"), ("spend_inr", "budget_inr")):
        out[k + "_pct_of_goal"] = round(out[k] / out[g], 4) if out[g] else None
    out["cost_per_attendee"] = None if t.attended == 0 else round(float(t.spend / t.attended), 1)
    out["cost_per_registration"] = None if t.registered == 0 else round(float(t.spend / t.registered), 1)
    return out


def get_settings(conn) -> dict:
    return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")}


def flags(conn, as_of, df: pd.DataFrame | None = None) -> pd.DataFrame:
    """Scale / change / stop recommendation per campaign using thresholds in `settings`."""
    s = get_settings(conn)
    df = campaign_funnel(conn, as_of) if df is None else df
    ph = phase(conn, as_of)
    rows = []
    elig = df.drop(index="UNATTR", errors="ignore")
    med_cpc = elig.cost_per_confirmed[(elig.spend > 0) & (elig.confirmed > 0)].median()
    for cid, r in df.iterrows():
        if cid == "UNATTR":
            continue
        if r.identified < s["min_sample"] or r.spend <= 0:
            rows.append((cid, "INSUFFICIENT DATA", f"Fewer than {int(s['min_sample'])} identified people or no spend yet."))
            continue
        if ph == "post-event" and r.attended > 0:
            cpa, su = r.cost_per_attendee, r.showup_rate
            if cpa > s["stop_cpa_max"]:
                d, why = "STOP", f"Cost per attendee Rs {cpa:,.0f} > stop limit Rs {s['stop_cpa_max']:,.0f}."
            elif cpa > s["change_cpa_max"] or su < s["change_showup_min"]:
                d, why = "CHANGE", f"Cost per attendee Rs {cpa:,.0f} (limit Rs {s['change_cpa_max']:,.0f}), show-up {su:.0%} (min {s['change_showup_min']:.0%})."
            elif cpa <= s["scale_cpa_max"] and su >= s["scale_showup_min"]:
                d, why = "SCALE", f"Cost per attendee Rs {cpa:,.0f} <= Rs {s['scale_cpa_max']:,.0f} and show-up {su:.0%} >= {s['scale_showup_min']:.0%}."
            else:
                d, why = "HOLD", f"Cost per attendee Rs {cpa:,.0f}, show-up {su:.0%}: inside the hold band."
        else:
            cpc, rr, cr = r.cost_per_confirmed, r.reg_rate, r.cancel_rate
            if pd.isna(cpc):
                rows.append((cid, "INSUFFICIENT DATA", "No confirmed registrations yet."))
                continue
            # Pre-event, spend is front-loaded, so compare against the portfolio median instead of absolute INR.
            ratio = cpc / med_cpc if (pd.notna(med_cpc) and med_cpc > 0) else 1.0
            cr = 0.0 if pd.isna(cr) else cr
            if ratio > s["stop_cpc_ratio"]:
                d, why = "STOP", f"Cost per confirmed registration Rs {cpc:,.0f} is {ratio:.1f}x the portfolio median (Rs {med_cpc:,.0f}); stop limit {s['stop_cpc_ratio']:.1f}x."
            elif ratio > s["change_cpc_ratio"] or cr > s["change_cancel_max"]:
                d, why = "CHANGE", f"Cost per confirmed Rs {cpc:,.0f} = {ratio:.1f}x median (limit {s['change_cpc_ratio']:.2f}x); cancellation {cr:.0%} (limit {s['change_cancel_max']:.0%})."
            elif ratio <= s["scale_cpc_ratio"] and rr >= s["scale_regrate_min"]:
                d, why = "SCALE", f"Cost per confirmed Rs {cpc:,.0f} = {ratio:.2f}x median (<= {s['scale_cpc_ratio']:.2f}x) and registration rate {rr:.0%} >= {s['scale_regrate_min']:.0%}."
            else:
                d, why = "HOLD", f"Cost per confirmed Rs {cpc:,.0f} = {ratio:.2f}x median, registration rate {rr:.0%}: inside the hold band."
        rows.append((cid, d, why))
    out = pd.DataFrame(rows, columns=["campaign_id", "decision", "reason"]).set_index("campaign_id")
    out["rule_set"] = "post-event outcome rules" if (ph == "post-event") else "pre-event leading-indicator rules"
    return out


PERSON_STAGES = ["identified_to_registered", "registered_to_confirmed", "confirmed_to_attended"]


def bottlenecks(df: pd.DataFrame, min_gap: float = 0.10) -> pd.DataFrame:
    """Weakest person-level stage per campaign = stage furthest below the portfolio median (relative gap).
    The anonymous session/registration-start stages are excluded because they are not unique people."""
    d = df.drop(index="UNATTR", errors="ignore")
    labels = dict(STAGES)
    rows = []
    for cid, r in d.iterrows():
        gaps = {}
        for key in PERSON_STAGES:
            med = d[key].median()
            if pd.notna(r[key]) and pd.notna(med) and med > 0:
                gaps[key] = (med - r[key]) / med
        if not gaps:
            rows.append((cid, "n/a", None, None))
            continue
        k = max(gaps, key=gaps.get)
        if gaps[k] < min_gap:
            rows.append((cid, "No material bottleneck", r[k], gaps[k]))
        else:
            rows.append((cid, labels[k], r[k], gaps[k]))
    return pd.DataFrame(rows, columns=["campaign_id", "bottleneck_stage", "stage_rate", "gap_vs_median"]).set_index("campaign_id")


def daily_series(conn, as_of) -> pd.DataFrame:
    c = cut(as_of)
    a = _q(conn, "SELECT substr(first_seen_at,1,10) d, COUNT(*) identified FROM people WHERE person_type='external' AND first_seen_at<=? GROUP BY 1", (c,))
    b = _q(conn, """SELECT substr(r.registered_at,1,10) d, COUNT(*) registered FROM registrations r
                    JOIN people p ON p.person_id=r.person_id WHERE p.person_type='external' AND r.registered_at<=? GROUP BY 1""", (c,))
    t = _q(conn, """SELECT substr(first_at,1,10) d, COUNT(*) attended FROM (
                      SELECT ck.person_id, MIN(ck.occurred_at) first_at FROM checkins ck JOIN people p ON p.person_id=ck.person_id
                      WHERE p.person_type='external' AND ck.result='admitted' AND ck.verification='verified' AND ck.occurred_at<=?
                      GROUP BY ck.person_id) GROUP BY 1""", (c,))
    s = _q(conn, "SELECT substr(activity_date,1,10) d, SUM(marketing_spend_inr) spend FROM daily_marketing WHERE activity_date<=? GROUP BY 1", (c,))
    m = None
    for part in (a, b, t, s):
        part = part.set_index("d")
        m = part if m is None else m.join(part, how="outer")
    if m is None or m.empty:
        return pd.DataFrame()
    m.index = pd.to_datetime(m.index)
    full = pd.date_range(m.index.min(), pd.Timestamp(as_of).normalize())
    m = m.reindex(full).fillna(0)
    m.index.name = "date"
    for col in list(m.columns):
        m["cum_" + col] = m[col].cumsum()
    return m


def momentum(conn, as_of, days: int = 7) -> pd.DataFrame:
    """Registrations and spend in the last `days` vs the previous `days` (what is driving change)."""
    end = pd.Timestamp(as_of).normalize()
    w1_start, w0_start = end - pd.Timedelta(days=days - 1), end - pd.Timedelta(days=2 * days - 1)
    def win(a, b):
        r = _q(conn, """SELECT r.campaign_id, COUNT(*) regs FROM registrations r JOIN people p ON p.person_id=r.person_id
                        WHERE p.person_type='external' AND substr(r.registered_at,1,10) BETWEEN ? AND ? GROUP BY 1""",
               (a.strftime("%Y-%m-%d"), b.strftime("%Y-%m-%d"))).set_index("campaign_id")
        s = _q(conn, "SELECT campaign_id, SUM(marketing_spend_inr) spend FROM daily_marketing WHERE substr(activity_date,1,10) BETWEEN ? AND ? GROUP BY 1",
               (a.strftime("%Y-%m-%d"), b.strftime("%Y-%m-%d"))).set_index("campaign_id")
        return r.join(s, how="outer").fillna(0)
    cur, prev = win(w1_start, end), win(w0_start, w1_start - pd.Timedelta(days=1))
    out = cur.join(prev, how="outer", lsuffix="_now", rsuffix="_prev").fillna(0)
    out["regs_change_pct"] = (out.regs_now - out.regs_prev) / out.regs_prev.where(out.regs_prev > 0)
    return out


def projection(conn, as_of) -> dict:
    """Projected final unique attendees as of `as_of`.
    post-event: actual. Otherwise: confirmed now + run-rate of last 14 days x days to event
    (assumes the recent daily confirmation rate continues, no decay), times ASSUMED show-up (settings)."""
    start, end = event_window(conn)
    df = campaign_funnel(conn, as_of)
    t = totals(df)
    if pd.Timestamp(as_of).normalize() >= end.normalize():
        return {"method": "actual", "projected_attendees": int(t.attended), "assumed_showup": None}
    s = get_settings(conn)
    assumed = s.get("assumed_showup", 0.75)
    days_left = max((start.normalize() - pd.Timestamp(as_of).normalize()).days, 0)
    mom = momentum(conn, as_of, 14)
    run_rate = float(mom.regs_now.sum()) / 14.0
    conf_ratio = (t.confirmed / t.registered) if t.registered else 0.0
    proj_reg = float(t.registered) + run_rate * days_left
    proj_conf = proj_reg * conf_ratio
    return {"method": "run-rate", "days_to_event": days_left, "registrations_per_day_last14": round(run_rate, 1),
            "projected_registrations": int(round(proj_reg)), "confirmed_ratio": round(conf_ratio, 3),
            "assumed_showup": assumed, "projected_attendees": int(round(proj_conf * assumed))}


def engagement(conn, as_of) -> pd.DataFrame:
    c = cut(as_of)
    d = _q(conn, """SELECT COALESCE(i.campaign_id,'UNATTR') campaign_id, i.event_type, COUNT(*) n
                    FROM interactions i JOIN people p ON p.person_id=i.person_id
                    WHERE p.person_type='external' AND i.occurred_at<=? GROUP BY 1,2""", (c,))
    return d.pivot(index="campaign_id", columns="event_type", values="n").fillna(0).astype(int)
