"""CampusFest Growth Center - dashboard, execution manager and embedded AI analyst (Streamlit)."""
from __future__ import annotations

import json

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from growth import analyst, etl, insights, metrics as M, model, store

st.set_page_config(page_title="CampusFest Growth Center", page_icon="🎪", layout="wide")

DECISION_COLORS = {"SCALE": "#1b9e4b", "HOLD": "#8a8f98", "CHANGE": "#e08a00", "STOP": "#d62d3a", "INSUFFICIENT DATA": "#b0b4ba"}
HIST_START, HIST_END = pd.Timestamp("2025-01-15").date(), pd.Timestamp("2025-04-20").date()


@st.cache_resource
def get_db():
    return etl.get_conn()


conn = get_db()


def inr(x, dec=0):
    return "-" if x is None or pd.isna(x) else f"Rs {x:,.{dec}f}"


def pct(x, dec=0):
    return "-" if x is None or pd.isna(x) else f"{x:.{dec}%}"


def style_decision(v):
    c = DECISION_COLORS.get(v)
    return f"background-color:{c};color:white;font-weight:600" if c else ""


# ------------------------------------------------------------------ sidebar
st.sidebar.title("CampusFest Growth Center")
st.sidebar.caption(f"City: {store.get_meta(conn, 'city')} | Challenge A: Attendee Growth")
as_of = st.sidebar.date_input(
    "Snapshot date (end of day, IST)", value=HIST_END, min_value=HIST_START, max_value=HIST_END,
    help="All 2025 analytics are computed as at the end of this day. Later outcomes never leak into earlier dates.")
jump = st.sidebar.radio("Quick jump", ["Custom", "T-4 weeks (14 Mar)", "T-3 weeks (20 Mar)", "Event eve (11 Apr)", "Final (20 Apr)"], index=0, horizontal=False)
JUMPS = {"T-4 weeks (14 Mar)": "2025-03-14", "T-3 weeks (20 Mar)": "2025-03-20", "Event eve (11 Apr)": "2025-04-11", "Final (20 Apr)": "2025-04-20"}
if jump in JUMPS:
    as_of = pd.Timestamp(JUMPS[jump]).date()
AS_OF = str(as_of)
PHASE = M.phase(conn, AS_OF)
st.sidebar.markdown(f"**Phase:** {PHASE}")
plan_date = store.get_meta(conn, "plan_date")
st.sidebar.caption(f"Next-edition plan date (simulated today): **{plan_date}**, event {store.get_meta(conn, 'next_event_date')}")

secrets_obj = None
try:
    secrets_obj = st.secrets
except Exception:
    pass
api_key = analyst.get_api_key(secrets_obj, st.session_state.get("api_key_override", ""))
if not api_key:
    key_in = st.sidebar.text_input("Gemini API key (for AI Analyst)", type="password",
                                   help="Free key: https://aistudio.google.com/app/apikey. Stored only in this session.")
    if key_in:
        st.session_state["api_key_override"] = key_in
        st.rerun()
else:
    st.sidebar.success("AI Analyst: API key loaded")

pending_n = len(store.pending_proposals(conn))
if pending_n:
    st.sidebar.warning(f"{pending_n} AI proposal(s) awaiting your confirmation (Execute tab)")

tab_dash, tab_diag, tab_dec, tab_exec, tab_ai, tab_set = st.tabs(
    ["Measure", "Diagnose", "Decide", "Execute", "AI Analyst", "Settings & Data"])

df = M.campaign_funnel(conn, AS_OF)
camps = df.drop(index="UNATTR", errors="ignore")
tot = M.totals(df)
ov = M.overview(conn, AS_OF)
flags = M.flags(conn, AS_OF, df)

# ================================================================== MEASURE
with tab_dash:
    st.subheader(f"Funnel and targets as at end of {AS_OF}  ({PHASE})")
    st.caption(f"Goals/budget version in force: **{ov['target_version']}**. People = unique external people. "
               "Impressions, reach, sessions and registration starts are anonymous activity, not people.")
    c = st.columns(6)
    c[0].metric("Identified people", f"{ov['identified']:,}", f"{ov['identified_pct_of_goal']:.0%} of {ov['identified_goal']:,}")
    c[1].metric("Registered", f"{ov['registered']:,}", f"{ov['registered_pct_of_goal']:.0%} of {ov['registration_goal']:,}")
    c[2].metric("Confirmed", f"{ov['confirmed']:,}", f"{ov['confirmed'] / max(ov['registered'], 1):.0%} of registered", delta_color="off")
    if PHASE == "pre-event":
        pr = M.projection(conn, AS_OF)
        c[3].metric("Projected attendees", f"{pr['projected_attendees']:,}", f"{pr['projected_attendees'] / ov['visitors_goal']:.0%} of {ov['visitors_goal']:,} (assumption-based)", delta_color="off")
    else:
        c[3].metric("Unique visitors (attended)", f"{ov['attended_unique_visitors']:,}", f"{ov['attended_unique_visitors_pct_of_goal']:.0%} of {ov['visitors_goal']:,}")
    c[4].metric("Spend", inr(ov["spend_inr"]), f"{ov['spend_inr_pct_of_goal']:.0%} of {inr(ov['budget_inr'])}", delta_color="off")
    if ov["cost_per_attendee"]:
        c[5].metric("Cost per attendee", inr(ov["cost_per_attendee"]), f"cost per registration {inr(ov['cost_per_registration'])}", delta_color="off")
    else:
        c[5].metric("Cost per registration", inr(ov["cost_per_registration"]), "attendance not yet known", delta_color="off")

    left, right = st.columns([1, 1])
    with left:
        stages = ["Identified", "Registered", "Confirmed", "Attended (unique)"]
        vals = [ov["identified"], ov["registered"], ov["confirmed"], ov["attended_unique_visitors"]]
        fig = go.Figure(go.Funnel(y=stages, x=vals, textinfo="value+percent previous",
                                  marker={"color": ["#4c78a8", "#72b7b2", "#54a24b", "#e45756"]}))
        fig.update_layout(title="Person-level funnel (unique external people)", height=360, margin=dict(l=10, r=10, t=50, b=10))
        st.plotly_chart(fig, use_container_width=True)
    with right:
        gdf = pd.DataFrame({"metric": ["Identified", "Registered", "Unique visitors"],
                            "Actual": [ov["identified"], ov["registered"], ov["attended_unique_visitors"]],
                            "Goal": [ov["identified_goal"], ov["registration_goal"], ov["visitors_goal"]]})
        fig = go.Figure()
        fig.add_bar(x=gdf.metric, y=gdf.Goal, name="Goal", marker_color="#c9ced6")
        fig.add_bar(x=gdf.metric, y=gdf.Actual, name="Actual", marker_color="#4c78a8")
        fig.update_layout(barmode="group", title="Targets vs actuals", height=360, margin=dict(l=10, r=10, t=50, b=10))
        st.plotly_chart(fig, use_container_width=True)

    ser = M.daily_series(conn, AS_OF)
    if not ser.empty:
        tg = M.targets_as_of(conn, AS_OF)
        fig = go.Figure()
        fig.add_scatter(x=ser.index, y=ser.cum_identified, name="Identified (cum.)")
        fig.add_scatter(x=ser.index, y=ser.cum_registered, name="Registered (cum.)")
        fig.add_scatter(x=ser.index, y=ser.cum_attended, name="Attended (cum.)")
        fig.add_hline(y=tg["registration_goal"], line_dash="dot", annotation_text="registration goal")
        es, ee = M.event_window(conn)
        fig.add_vrect(x0=es, x1=ee, fillcolor="orange", opacity=0.15, line_width=0, annotation_text="festival")
        sp = go.Scatter(x=ser.index, y=ser.cum_spend, name="Spend (cum., Rs)", yaxis="y2", line=dict(dash="dash", color="gray"))
        fig.add_trace(sp)
        fig.update_layout(title="Growth over time", height=380, yaxis2=dict(overlaying="y", side="right", title="Rs"),
                          margin=dict(l=10, r=10, t=50, b=10), legend=dict(orientation="h"))
        st.plotly_chart(fig, use_container_width=True)

    st.markdown("#### Campaign KPIs")
    show = df[["campaign_name", "identified", "registered", "confirmed", "cancelled", "attended", "spend", "reg_rate",
               "showup_rate", "cost_per_registration", "cost_per_confirmed", "cost_per_attendee"]].copy()
    show.columns = ["Campaign", "Identified", "Registered", "Confirmed", "Cancelled", "Attended", "Spend (Rs)", "Reg. rate",
                    "Show-up", "Cost/registration", "Cost/confirmed", "Cost/attendee"]
    if PHASE == "pre-event":
        show = show.drop(columns=["Attended", "Show-up", "Cost/attendee"])
    st.dataframe(show.style.format({"Spend (Rs)": "{:,.0f}", "Reg. rate": "{:.0%}", "Show-up": "{:.0%}", "Cost/registration": "{:,.0f}",
                                    "Cost/confirmed": "{:,.0f}", "Cost/attendee": "{:,.0f}", "Identified": "{:,.0f}",
                                    "Registered": "{:,.0f}", "Confirmed": "{:,.0f}", "Cancelled": "{:,.0f}", "Attended": "{:,.0f}"}, na_rep="-"),
                 use_container_width=True)
    if "UNATTR" in df.index:
        st.caption("Walk-in / unattributed: attendees without a registration or campaign; they count in unique visitors but have no spend or cost metrics.")

    with st.expander("Anonymous activity funnel (not unique people)"):
        a = df.drop(index="UNATTR", errors="ignore")[["campaign_name", "impressions", "reach", "clicks", "sessions", "reg_starts", "registered"]]
        a.columns = ["Campaign", "Impressions", "Reported reach", "Clicks", "Landing sessions", "Registration starts", "Registered people"]
        st.dataframe(a.style.format("{:,.0f}", subset=a.columns[1:]), use_container_width=True)
        st.caption("Summed reach and impressions do not prove incremental attendance and are not unique people.")

# ================================================================== DIAGNOSE
with tab_diag:
    st.subheader("What is working, what is not, and why")
    ins = insights.generate(conn, AS_OF)
    st.markdown("#### Insight feed (computed from live data)")
    if not ins:
        st.info("No notable issues at this snapshot.")
    icon = {"high": "🔴", "medium": "🟠", "opportunity": "🟢", "low": "⚪"}
    for i, it in enumerate(ins):
        with st.container(border=True):
            st.markdown(f"{icon.get(it['severity'], '')} **{it['title']}**")
            st.caption(it["detail"])
            if it.get("task"):
                if st.button("Create task from this insight", key=f"ins_{AS_OF}_{i}"):
                    t = it["task"]
                    owner = None
                    if t.get("campaign_id"):
                        r = conn.execute("SELECT owner FROM campaign_plan WHERE campaign_id=?", (t["campaign_id"],)).fetchone()
                        owner = r[0] if r else None
                    due = (pd.Timestamp(plan_date) + pd.Timedelta(days=7)).strftime("%Y-%m-%d")
                    tid = store.add_task(conn, t["title"], t["description"], t.get("campaign_id"), None, owner, due, t["priority"], "todo", "insight")
                    st.success(f"Task #{tid} created (see Execute tab).")

    cL, cR = st.columns(2)
    with cL:
        cc = camps[camps.spend > 0].copy()
        if PHASE != "pre-event" and cc.attended.sum() > 0:
            cc["cost_metric"] = cc.cost_per_attendee
            cc["quality"] = cc.showup_rate
            ylab, xlab = "Cost per attendee (Rs)", "Show-up rate (attended / registered)"
        else:
            cc["cost_metric"] = cc.cost_per_confirmed
            cc["quality"] = cc.reg_rate
            ylab, xlab = "Cost per confirmed registration (Rs)", "Registration rate (registered / identified)"
        fig = px.scatter(cc, x="quality", y="cost_metric", size=cc.registered.clip(lower=1), text="campaign_name", color=flags.decision.reindex(cc.index),
                         color_discrete_map=DECISION_COLORS, labels={"quality": xlab, "cost_metric": ylab, "color": "Flag"})
        fig.update_traces(textposition="top center")
        fig.update_layout(title="Efficiency map (bubble = registrations)", height=420, margin=dict(l=10, r=10, t=50, b=10))
        st.plotly_chart(fig, use_container_width=True)
    with cR:
        if camps.spend.sum() > 0:
            share = pd.DataFrame({"Campaign": camps.campaign_name,
                                  "Share of spend": camps.spend / camps.spend.sum(),
                                  "Share of registrations": camps.registered / max(camps.registered.sum(), 1),
                                  "Share of attendees": camps.attended / max(camps.attended.sum(), 1)})
            if PHASE == "pre-event":
                share = share.drop(columns=["Share of attendees"])
            fig = px.bar(share.melt("Campaign"), x="Campaign", y="value", color="variable", barmode="group", labels={"value": "Share", "variable": ""})
            fig.update_layout(title="Where the money goes vs what comes back", height=420, yaxis_tickformat=".0%", margin=dict(l=10, r=10, t=50, b=10), legend=dict(orientation="h"))
            st.plotly_chart(fig, use_container_width=True)

    st.markdown("#### Stage conversion by campaign (weakest stage vs portfolio median)")
    bn = M.bottlenecks(df)
    stg = camps[["campaign_name", "reg_rate", "confirm_rate", "attended_per_confirmed", "cancel_rate", "session_to_start"]].copy()
    stg.columns = ["Campaign", "Identified > registered", "Registered > confirmed", "Confirmed > attended", "Cancellation rate", "Session > reg. start (anon.)"]
    stg["Weakest stage"] = bn.bottleneck_stage
    stg["Gap vs median"] = bn.gap_vs_median
    st.dataframe(stg.style.format({c_: "{:.0%}" for c_ in stg.columns if c_ not in ("Campaign", "Weakest stage")}, na_rep="-")
                 .background_gradient(subset=["Identified > registered", "Registered > confirmed", "Confirmed > attended"], cmap="RdYlGn", vmin=0.4, vmax=1.0),
                 use_container_width=True)

    st.markdown("#### Momentum: last 7 days vs previous 7 days (what changed)")
    mom = M.momentum(conn, AS_OF, 7)
    mom = mom.join(camps.campaign_name).reset_index().set_index("campaign_name") if not mom.empty else mom
    if not mom.empty:
        mom = mom.drop(columns=["campaign_id"], errors="ignore")
        mom.columns = ["Registrations (now)", "Spend (now)", "Registrations (prev)", "Spend (prev)", "Registration change"]
        st.dataframe(mom.style.format({"Registration change": "{:+.0%}", "Spend (now)": "{:,.0f}", "Spend (prev)": "{:,.0f}",
                                       "Registrations (now)": "{:,.0f}", "Registrations (prev)": "{:,.0f}"}, na_rep="-"), use_container_width=True)

    with st.expander("Identified-person engagement by campaign"):
        st.dataframe(M.engagement(conn, AS_OF), use_container_width=True)

# ================================================================== DECIDE
with tab_dec:
    st.subheader("Scale / change / stop")
    st.caption(f"Rule set: **{flags.rule_set.iloc[0] if len(flags) else '-'}**. Thresholds are editable in Settings & Data and re-evaluate instantly.")
    fl = flags.join(camps[["campaign_name", "spend", "registered", "attended", "cost_per_confirmed", "cost_per_attendee"]]).join(bn[["bottleneck_stage"]])
    fl = fl[["campaign_name", "decision", "reason", "spend", "registered", "attended", "bottleneck_stage"]]
    fl.columns = ["Campaign", "Decision", "Why", "Spend (Rs)", "Registered", "Attended", "Weakest stage"]
    st.dataframe(fl.style.applymap(style_decision, subset=["Decision"]).format({"Spend (Rs)": "{:,.0f}", "Registered": "{:,.0f}", "Attended": "{:,.0f}"}),
                 use_container_width=True)

    st.markdown("---")
    st.subheader("Budget allocation planner (next edition)")
    st.caption("Uses the funnel model calibrated on 2025 actuals. Elasticities, show-up lift and overlap haircut are ASSUMPTIONS (see README). "
               "Edit budgets here and save to update the campaign plan used everywhere (including the AI analyst).")
    plan = pd.read_sql("SELECT * FROM campaign_plan ORDER BY campaign_id", conn)
    b, w = model.baseline_from_conn(conn)
    scen_name = st.radio("Model assumptions", ["Base case", "Stretch target"], horizontal=True)
    sc = model.BASE if scen_name == "Base case" else model.STRETCH
    cols = st.columns(4)
    new_budgets = {}
    for i, r in plan.iterrows():
        if r.status == "stopped":
            new_budgets[r.campaign_id] = 0.0
            continue
        with cols[i % 4]:
            new_budgets[r.campaign_id] = st.number_input(f"{r.campaign_id} {r['name']} (2025: {inr(b.spend_inr.get(r.campaign_id, 0))})",
                                                         min_value=0, value=int(r.budget_inr), step=1000, key=f"bud_{r.campaign_id}")
    reserve = sc.reserve
    s2 = model.Scenario(**{**sc.__dict__, "allocation": new_budgets})
    pr = model.project(b, w, new_budgets, s2)
    budget_cap = st.number_input("Approved total budget (Rs, incl. reserve)", min_value=0, value=int(sc.reserve + sum(sc.allocation.values())), step=10000)
    k = st.columns(4)
    k[0].metric("Projected unique attendees", f"{pr['total']:,.0f}", f"{pr['uplift_pct']:+.0%} vs 2025 ({pr['baseline_total']:,.0f})")
    k[1].metric("Total budget (incl. Rs {:,.0f} reserve)".format(reserve), inr(pr["total_budget"]),
                "within cap" if pr["total_budget"] <= budget_cap else f"OVER cap by {inr(pr['total_budget'] - budget_cap)}", delta_color="off")
    k[2].metric("Blended cost per attendee", inr(pr["blended_cost_per_attendee"]), f"2025: {inr(b.spend_inr.sum() / (b.attended.sum() + w))}", delta_color="off")
    k[3].metric("Capacity check", f"{pr['total'] / sc.capacity:.0%} of {sc.capacity:,}", "ASSUMED venue cap", delta_color="off")
    t = pr["table"].join(b[["campaign_name", "attended", "spend_inr"]].rename(columns={"attended": "attended_2025", "spend_inr": "spend_2025"}))
    t = t[["campaign_name", "spend_2025", "budget_inr", "attended_2025", "attended", "showup", "cost_per_attendee", "reach_needed"]]
    t.columns = ["Campaign", "2025 spend", "Planned budget", "2025 attended", "Projected attended", "Projected show-up", "Cost/attendee", "Reach needed (anon.)"]
    st.dataframe(t.style.format({"2025 spend": "{:,.0f}", "Planned budget": "{:,.0f}", "2025 attended": "{:,.0f}", "Projected attended": "{:,.0f}",
                                 "Projected show-up": "{:.0%}", "Cost/attendee": "{:,.0f}", "Reach needed (anon.)": "{:,.0f}"}, na_rep="-"), use_container_width=True)
    if st.button("Save these budgets to the campaign plan"):
        for cid, v in new_budgets.items():
            store.update_campaign_plan(conn, cid, budget_inr=float(v))
        st.success("Saved. The plan now reflects these budgets.")
        st.rerun()

    ss = model.scenario_summary(b, w)
    with st.expander("Base vs stretch scenario (funnel: reach > identified > registered > attended)"):
        ss2 = ss.copy()
        st.dataframe(ss2.style.format({"budget_inr": "{:,.0f}", "identified": "{:,.0f}", "registered": "{:,.0f}", "attended_campaigns": "{:,.0f}",
                                       "attendees": "{:,.0f}", "vs_2025_pct": "{:+.0%}", "cost_per_attendee": "{:,.0f}", "reach_needed": "{:,.0f}"}),
                     use_container_width=True)

    st.markdown("---")
    st.subheader("Five bets: prioritization (RICE-style: expected attendees x confidence / effort)")
    ideas = pd.read_sql("SELECT idea_id, rank, name, decision, expected_attendees, confidence, effort, budget_inr, kpi, scale_rule, change_rule, stop_rule FROM ideas ORDER BY rank IS NULL, rank", conn)
    ideas["score"] = ideas.expected_attendees.clip(lower=0) * ideas.confidence / ideas.effort.replace(0, pd.NA)
    st.dataframe(ideas.rename(columns={"expected_attendees": "Incremental attendees (base)"}), use_container_width=True)

# ================================================================== EXECUTE
with tab_exec:
    st.subheader("Execution: campaigns, tasks, data")
    # ---- AI/insight proposals
    pend = store.pending_proposals(conn)
    if len(pend):
        st.markdown("#### Pending proposals from the AI Analyst (need your confirmation)")
        for _, p in pend.iterrows():
            payload = json.loads(p.payload)
            with st.container(border=True):
                st.markdown(f"**{p.kind.replace('_', ' ').title()}**: " + (payload.get("title") if p.kind == "task" else f"{payload['campaign_id']} budget -> {inr(payload['new_budget_inr'])}"))
                if p.kind == "task":
                    st.caption(f"{payload.get('description', '')} | campaign {payload.get('campaign_id')} | owner {payload.get('owner')} | due {payload.get('due_date')} | priority {payload.get('priority')}")
                st.caption(f"Rationale: {p.rationale}")
                a, r_ = st.columns([1, 1])
                if a.button("Confirm", key=f"acc_{p.proposal_id}"):
                    st.success(store.accept_proposal(conn, int(p.proposal_id)))
                    st.rerun()
                if r_.button("Reject", key=f"rej_{p.proposal_id}"):
                    store.reject_proposal(conn, int(p.proposal_id))
                    st.rerun()

    left, right = st.columns([3, 2])
    with left:
        st.markdown("#### Campaign plan (budgets, goals, status)")
        plan = pd.read_sql("SELECT campaign_id, name, channel, owner, budget_inr, attendee_goal, status, kind, notes FROM campaign_plan ORDER BY campaign_id", conn)
        edited = st.data_editor(plan, key="plan_editor", hide_index=True, use_container_width=True,
                                disabled=["campaign_id", "name", "channel", "kind"],
                                column_config={"status": st.column_config.SelectboxColumn(options=store.CAMPAIGN_STATUSES),
                                               "budget_inr": st.column_config.NumberColumn("budget_inr", min_value=0, step=1000)})
        if st.button("Save campaign plan"):
            n = 0
            for i, r in edited.iterrows():
                o = plan.loc[i]
                ch = {k: r[k] for k in ("budget_inr", "attendee_goal", "status", "owner", "notes") if r[k] != o[k]}
                if ch:
                    store.update_campaign_plan(conn, r.campaign_id, **ch)
                    n += 1
            st.success(f"Saved {n} change(s).")
            st.rerun()
    with right:
        st.markdown("#### Add a campaign")
        with st.form("new_campaign"):
            nm = st.text_input("Name")
            ch_ = st.text_input("Channel", "other")
            ow = st.text_input("Owner")
            bd = st.number_input("Budget (Rs)", 0, step=1000)
            gl = st.number_input("Attendee goal", 0, step=10)
            nt = st.text_area("Notes")
            if st.form_submit_button("Add campaign") and nm:
                st.success(f"Created {store.add_campaign(conn, nm, ch_, ow, bd, int(gl), nt)}")
                st.rerun()

    st.markdown("#### Tasks")
    owners = sorted({r[0] for r in conn.execute("SELECT DISTINCT owner FROM campaign_plan WHERE owner IS NOT NULL")} | {r[0] for r in conn.execute("SELECT DISTINCT owner FROM tasks WHERE owner IS NOT NULL")})
    cids = [""] + [r[0] for r in conn.execute("SELECT campaign_id FROM campaign_plan ORDER BY campaign_id")]
    with st.expander("Add task", expanded=False):
        with st.form("new_task"):
            a, b_ = st.columns(2)
            title = a.text_input("Title")
            camp = b_.selectbox("Campaign", cids)
            desc = st.text_area("Description")
            c1, c2, c3 = st.columns(3)
            ow = c1.text_input("Owner")
            due = c2.date_input("Due", value=pd.Timestamp(plan_date).date() + pd.Timedelta(days=7))
            pri = c3.selectbox("Priority", store.PRIORITIES, index=1)
            if st.form_submit_button("Create task"):
                try:
                    store.add_task(conn, title, desc, camp or None, None, ow or None, str(due), pri)
                    st.rerun()
                except ValueError as e:
                    st.error(str(e))
    tasks = store.list_tasks(conn)
    today = pd.Timestamp(plan_date)
    tasks["overdue"] = (tasks.status != "done") & (pd.to_datetime(tasks.due_date) < today)
    k = st.columns(5)
    for i, s_ in enumerate(store.TASK_STATUSES):
        k[i].metric(s_.replace("_", " ").title(), int((tasks.status == s_).sum()))
    k[4].metric("Overdue (vs plan date)", int(tasks.overdue.sum()))
    fcamp = st.selectbox("Filter by campaign", ["All"] + cids[1:], key="task_filter")
    view = tasks if fcamp == "All" else tasks[tasks.campaign_id == fcamp]
    view = view.copy()
    view["due_date"] = pd.to_datetime(view.due_date).dt.date
    ed = st.data_editor(view[["task_id", "title", "campaign_id", "owner", "due_date", "priority", "status", "source", "overdue"]],
                        key="task_editor", hide_index=True, use_container_width=True, num_rows="fixed",
                        disabled=["task_id", "source", "overdue"],
                        column_config={"status": st.column_config.SelectboxColumn(options=store.TASK_STATUSES),
                                       "priority": st.column_config.SelectboxColumn(options=store.PRIORITIES),
                                       "campaign_id": st.column_config.SelectboxColumn(options=cids[1:]),
                                       "due_date": st.column_config.DateColumn()})
    cA, cB = st.columns([1, 3])
    if cA.button("Save task changes"):
        n = 0
        for i, r in ed.iterrows():
            o = view.loc[i]
            ch = {}
            for k_ in ("title", "campaign_id", "owner", "priority", "status"):
                if (r[k_] if pd.notna(r[k_]) else None) != (o[k_] if pd.notna(o[k_]) else None):
                    ch[k_] = r[k_]
            if r.due_date != o.due_date:
                ch["due_date"] = str(r.due_date)
            if ch:
                store.update_task(conn, int(r.task_id), **ch)
                n += 1
        st.success(f"Saved {n} task(s).")
        st.rerun()
    del_id = cB.number_input("Delete task id", min_value=0, value=0, step=1)
    if cB.button("Delete task") and del_id:
        store.delete_task(conn, int(del_id))
        st.rerun()

    st.markdown("#### Log new marketing activity (updates the 2025 analytics)")
    st.caption("Appends a daily marketing row (spend/clicks/etc.). Funnel costs, flags and AI answers recompute immediately.")
    with st.form("mk_form"):
        c1, c2, c3 = st.columns(3)
        mc = c1.selectbox("Campaign", [r[0] for r in conn.execute("SELECT campaign_id FROM campaigns ORDER BY campaign_id")])
        md = c2.date_input("Activity date", value=as_of, min_value=HIST_START, max_value=HIST_END)
        sp = c3.number_input("Spend (Rs, negative = credit)", value=0, step=500)
        c4, c5, c6, c7 = st.columns(4)
        im = c4.number_input("Impressions", 0)
        cl = c5.number_input("Clicks", 0)
        se = c6.number_input("Landing sessions", 0)
        rs = c7.number_input("Registration starts", 0)
        if st.form_submit_button("Add activity row"):
            store.add_daily_marketing(conn, mc, str(md), impressions=im, clicks=cl, landing_page_sessions=se, registration_starts=rs, marketing_spend_inr=sp)
            st.success("Added. Dashboard updated.")
            st.rerun()
    with st.expander("Change log"):
        st.dataframe(pd.read_sql("SELECT at, actor, action, detail FROM change_log ORDER BY log_id DESC LIMIT 100", conn), use_container_width=True)

# ================================================================== AI ANALYST
with tab_ai:
    st.subheader("AI Growth Analyst & Copilot")
    st.caption(f"Answers come from tool calls on the live database (snapshot {AS_OF}), not from generic knowledge. "
               "It can propose tasks and budget changes; nothing changes until you confirm.")
    if "messages" not in st.session_state:
        st.session_state.messages = []
    examples = ["Why are we missing the registration target? Diagnose it.",
                "Which campaigns should I scale, change or stop, and why?",
                "If I move Rs 20,000 from Paid Social to Student Referrals, what happens to attendance?",
                "What was our revenue per attendee?",
                "Create a task for the owner of the weakest campaign."]
    ec = st.columns(len(examples))
    clicked = None
    for i, e in enumerate(examples):
        if ec[i].button(e, key=f"ex_{i}", use_container_width=True):
            clicked = e
    if st.button("Clear conversation"):
        st.session_state.messages = []
        st.rerun()
    for m_ in st.session_state.messages:
        with st.chat_message(m_["role"]):
            st.markdown(m_["content"])
            if m_.get("trace"):
                with st.expander(f"Data used ({len(m_['trace'])} tool call(s))"):
                    for tr in m_["trace"]:
                        st.markdown(f"`{tr['tool']}` args: `{json.dumps(tr['args'], default=str)}`")
                        st.json(tr["result"], expanded=False)
    prompt = st.chat_input("Ask about performance, diagnosis, what to do next...") or clicked
    if prompt:
        if not api_key:
            st.error("Add a Gemini API key in the sidebar (or GEMINI_API_KEY in secrets/env) to use the analyst.")
        else:
            st.session_state.messages.append({"role": "user", "content": prompt})
            with st.chat_message("user"):
                st.markdown(prompt)
            with st.chat_message("assistant"):
                with st.spinner("Querying the data..."):
                    try:
                        hist = [{"role": m_["role"], "content": m_["content"]} for m_ in st.session_state.messages[:-1]]
                        res = analyst.ask(conn, AS_OF, hist, prompt, api_key)
                        st.markdown(res["answer"])
                        st.session_state.messages.append({"role": "assistant", "content": res["answer"], "trace": res["trace"]})
                        if res["trace"]:
                            with st.expander(f"Data used ({len(res['trace'])} tool call(s)) - model {res['model']}"):
                                for tr in res["trace"]:
                                    st.markdown(f"`{tr['tool']}` args: `{json.dumps(tr['args'], default=str)}`")
                                    st.json(tr["result"], expanded=False)
                    except Exception as e:
                        st.error(f"The analyst could not answer: {e}")
            st.rerun() if store.pending_proposals(conn).shape[0] else None
    pend = store.pending_proposals(conn)
    if len(pend):
        st.markdown("#### Proposed actions awaiting confirmation")
        for _, p in pend.iterrows():
            payload = json.loads(p.payload)
            with st.container(border=True):
                st.markdown(f"**{p.kind.replace('_', ' ').title()}**: " + (payload.get("title") if p.kind == "task" else f"{payload['campaign_id']} budget -> {inr(payload['new_budget_inr'])}"))
                st.caption(f"{payload.get('description', '')}  Rationale: {p.rationale}")
                a, r_ = st.columns(2)
                if a.button("Confirm", key=f"ai_acc_{p.proposal_id}"):
                    st.success(store.accept_proposal(conn, int(p.proposal_id)))
                    st.rerun()
                if r_.button("Reject", key=f"ai_rej_{p.proposal_id}"):
                    store.reject_proposal(conn, int(p.proposal_id))
                    st.rerun()

# ================================================================== SETTINGS
with tab_set:
    st.subheader("Thresholds (scale / change / stop rules)")
    sdf = pd.read_sql("SELECT key, value, description FROM settings ORDER BY rowid", conn)
    sed = st.data_editor(sdf, hide_index=True, use_container_width=True, disabled=["key", "description"], key="settings_editor")
    if st.button("Save thresholds"):
        for i, r in sed.iterrows():
            if r.value != sdf.loc[i, "value"]:
                store.update_setting(conn, r.key, r.value)
        st.success("Saved. Flags recompute.")
        st.rerun()

    st.subheader("Goal and budget versions")
    st.dataframe(pd.read_sql("SELECT * FROM targets ORDER BY effective_at", conn), use_container_width=True)
    with st.form("target_form"):
        c = st.columns(6)
        te = c[0].date_input("Effective", value=as_of)
        ti = c[1].number_input("Identified goal", 0, value=3600)
        tr_ = c[2].number_input("Registration goal", 0, value=3200)
        tv = c[3].number_input("Unique visitors goal", 0, value=2200)
        tb = c[4].number_input("Marketing budget (Rs)", 0, value=300000, step=10000)
        tl = c[5].text_input("Label", "Updated plan")
        if st.form_submit_button("Add target version"):
            store.add_target_version(conn, f"{te} 09:00:00", ti, tr_, tv, tb, tl)
            st.rerun()

    st.subheader("Data quality (ETL log)")
    st.dataframe(pd.read_sql("SELECT * FROM etl_log", conn), use_container_width=True)
    st.caption("Duplicate import rows (same stable ID) are removed. Internal, staff and test people are excluded from all attendee metrics. "
               "Walk-ins appear as unattributed attendance.")
    st.subheader("Reset")
    if st.button("Reset ALL data and edits to the original dataset", type="primary"):
        etl.reset_database(conn)
        st.session_state.messages = []
        st.success("Original data restored.")
        st.rerun()
