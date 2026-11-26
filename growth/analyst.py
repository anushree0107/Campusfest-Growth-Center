"""Embedded AI Growth Analyst & Copilot.

Design: Gemini (function calling) answers ONLY from tool results that query the live SQLite database.
Write actions (tasks, budget changes) are never executed by the model -- they become *proposals*
that the user must confirm in the UI (store.accept_proposal).
"""
from __future__ import annotations

import functools
import json
import math
import os
import re
import sqlite3
from dataclasses import dataclass, field

import pandas as pd

from . import metrics as M, model, store

DEFAULT_MODELS = ["gemini-2.5-flash", "gemini-2.0-flash"]
FIRST_DAY, LAST_DAY = "2025-01-15", "2025-04-20"


def _clean(obj):
    """Make anything JSON-safe (NaN -> None, numpy -> python, DataFrame -> records)."""
    if isinstance(obj, pd.DataFrame):
        return _clean(obj.reset_index().to_dict("records"))
    if isinstance(obj, pd.Series):
        return _clean(obj.to_dict())
    if isinstance(obj, dict):
        return {str(k): _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else round(obj, 4)
    if hasattr(obj, "item"):
        return _clean(obj.item())
    if isinstance(obj, (pd.Timestamp,)):
        return obj.strftime("%Y-%m-%d %H:%M:%S")
    return obj


@dataclass
class Context:
    conn: sqlite3.Connection
    as_of: str                      # dashboard snapshot date (YYYY-MM-DD)
    trace: list = field(default_factory=list)
    proposals: list = field(default_factory=list)   # proposal_ids created in this turn


def _resolve_date(ctx: Context, d: str) -> str:
    d = (d or "").strip() or ctx.as_of
    try:
        ts = pd.Timestamp(d)
    except Exception:
        raise ValueError(f"Could not parse date '{d}'. Use YYYY-MM-DD.")
    if ts < pd.Timestamp(FIRST_DAY) or ts > pd.Timestamp(LAST_DAY):
        raise ValueError(f"Date {d} is outside the covered history {FIRST_DAY} to {LAST_DAY}; the data cannot answer for that date.")
    return ts.strftime("%Y-%m-%d")


def _match_campaign(conn, key: str) -> str | None:
    key = (key or "").strip()
    if not key:
        return None
    rows = conn.execute("SELECT campaign_id, name FROM campaign_plan").fetchall()
    for r in rows:
        if key.lower() in (r[0].lower(), r[1].lower()):
            return r[0]
    hits = [r[0] for r in rows if key.lower() in r[1].lower()]
    return hits[0] if len(hits) == 1 else None


def make_tools(ctx: Context):
    """Build the tool functions bound to a live DB connection (closures keep signatures clean for Gemini)."""
    conn = ctx.conn

    def traced(fn):
        @functools.wraps(fn)
        def wrapper(*a, **k):
            try:
                out = _clean(fn(*a, **k))
                ctx.trace.append({"tool": fn.__name__, "args": k or list(a), "result": out})
                return out if isinstance(out, dict) else {"result": out}
            except Exception as e:  # tool errors go back to the model so it can explain / retry
                err = {"error": str(e)}
                ctx.trace.append({"tool": fn.__name__, "args": k or list(a), "result": err})
                return err
        return wrapper

    @traced
    def get_overview(as_of_date: str = "") -> dict:
        """Headline funnel for a snapshot date (end of day IST): identified, registered, confirmed, cancelled,
        waitlisted, attended unique visitors, spend, each with the goal/budget version effective on that date,
        % of goal, cost per attendee and cost per registration. Leave as_of_date empty to use the dashboard date.
        Format YYYY-MM-DD, covered range 2025-01-15 to 2025-04-20."""
        d = _resolve_date(ctx, as_of_date)
        return {**M.overview(conn, d), "projection": M.projection(conn, d)}

    @traced
    def get_campaign_metrics(as_of_date: str = "", campaign: str = "") -> dict:
        """Per-campaign funnel and cost metrics for a snapshot date: identified, registered, confirmed, cancelled,
        attended, spend, reg_rate (registered/identified), showup_rate (attended/registered), cost_per_registration,
        cost_per_confirmed, cost_per_attendee, impressions, clicks, sessions, reg_starts (anonymous activity, not
        unique people). campaign = id (C01..C07) or name, empty = all. UNATTR = walk-ins without a campaign."""
        d = _resolve_date(ctx, as_of_date)
        df = M.campaign_funnel(conn, d)
        cid = _match_campaign(conn, campaign)
        if campaign and cid is None and campaign.upper() != "UNATTR":
            return {"error": f"No campaign matches '{campaign}'. Campaigns: " + ", ".join(df.index.astype(str))}
        if cid:
            df = df.loc[[cid]]
        cols = ["campaign_name", "identified", "registered", "confirmed", "cancelled", "waitlisted", "attended", "spend",
                "reg_rate", "confirm_rate", "cancel_rate", "showup_rate", "cost_per_registration", "cost_per_confirmed",
                "cost_per_attendee", "impressions", "clicks", "sessions", "reg_starts"]
        return {"as_of": d, "campaigns": df[cols]}

    @traced
    def get_scale_change_stop(as_of_date: str = "") -> dict:
        """Scale / change / stop / hold recommendation per campaign using the thresholds configured in the app,
        the reason, and each campaign's weakest person-level funnel stage versus the portfolio median."""
        d = _resolve_date(ctx, as_of_date)
        df = M.campaign_funnel(conn, d)
        f = M.flags(conn, d, df)
        b = M.bottlenecks(df)
        return {"as_of": d, "phase": M.phase(conn, d), "thresholds": M.get_settings(conn), "flags": f.join(b)}

    @traced
    def get_trend(as_of_date: str = "", freq: str = "weekly") -> dict:
        """Time series up to the snapshot date: new and cumulative identified people, registrations, attended
        and spend. freq = 'daily' or 'weekly' (weekly uses the last day of each week)."""
        d = _resolve_date(ctx, as_of_date)
        s = M.daily_series(conn, d)
        if freq == "weekly":
            s = s.resample("W").agg({"identified": "sum", "registered": "sum", "attended": "sum", "spend": "sum",
                                     "cum_identified": "last", "cum_registered": "last", "cum_attended": "last", "cum_spend": "last"})
        s = s.tail(60)
        s.index = s.index.strftime("%Y-%m-%d")
        return {"as_of": d, "freq": freq, "series": s}

    @traced
    def get_momentum(as_of_date: str = "", days: int = 7) -> dict:
        """What changed recently: registrations and spend per campaign in the last N days versus the previous N days."""
        d = _resolve_date(ctx, as_of_date)
        return {"as_of": d, "window_days": int(days), "momentum": M.momentum(conn, d, int(days))}

    @traced
    def get_projection(as_of_date: str = "") -> dict:
        """Projected final unique attendees at the snapshot date. Before the event this is a naive run-rate
        extrapolation times an ASSUMED show-up rate (flagged as assumption); after the event it is the actual."""
        d = _resolve_date(ctx, as_of_date)
        return {"as_of": d, **M.projection(conn, d)}

    @traced
    def get_engagement(as_of_date: str = "") -> dict:
        """Counts of identified-person engagement events per campaign (page_visit, brochure_download,
        nurture_email_sent/opened/clicked, event_question_submitted, registration_start, opted_out)."""
        d = _resolve_date(ctx, as_of_date)
        return {"as_of": d, "engagement": M.engagement(conn, d)}

    @traced
    def get_plan_and_tasks_summary() -> dict:
        """The team's CURRENT plan for the next edition: planned budget and attendee goal per campaign (editable in
        the app), campaign status, the 5 prioritized ideas with their scale/change/stop rules, the approved
        budget/target versions, and a count of tasks by status and overdue tasks."""
        plan = pd.read_sql("SELECT * FROM campaign_plan ORDER BY campaign_id", conn)
        ideas = pd.read_sql("SELECT idea_id, rank, name, decision, expected_attendees, budget_inr, campaign_id, kpi, scale_rule, change_rule, stop_rule FROM ideas ORDER BY rank IS NULL, rank", conn)
        tasks = store.list_tasks(conn)
        today = pd.Timestamp(store.get_meta(conn, "plan_date"))
        overdue = tasks[(tasks.status != "done") & (pd.to_datetime(tasks.due_date) < today)]
        b, w = model.baseline_from_conn(conn)
        budgets = dict(zip(plan.campaign_id, plan.budget_inr))
        cur = model.project(b, w, budgets, model.Scenario("Current plan", allocation=budgets, reserve=model.BASE_RESERVE,
                                                          recovery_ref_budget=model.BASE.recovery_ref_budget))
        return {"plan_date_simulated_today": str(today.date()), "campaign_plan": plan, "ideas": ideas,
                "tasks_by_status": tasks.status.value_counts().to_dict(), "overdue_tasks": int(len(overdue)),
                "projected_attendees_current_plan_assumption_based": round(cur["total"]),
                "planned_budget_total_incl_reserve": cur["total_budget"],
                "targets": pd.read_sql("SELECT * FROM targets ORDER BY effective_at", conn)}

    @traced
    def simulate_budget_change(changes_json: str, scenario: str = "base") -> dict:
        """What-if calculator for the NEXT edition (assumption-based model calibrated on 2025 actuals; it does not
        change any data). changes_json is a JSON object of campaign_id -> new budget in INR, e.g. '{"C02": 30000, "C05": 60000}'
        applied on top of the current planned budgets (C08 = no-show recovery). Returns projected unique attendees
        before vs after, per-campaign deltas and the marginal cost per extra attendee."""
        ch = json.loads(changes_json)
        plan = pd.read_sql("SELECT campaign_id, budget_inr FROM campaign_plan", conn)
        cur = dict(zip(plan.campaign_id, plan.budget_inr))
        new = dict(cur)
        for k, v in ch.items():
            cid = _match_campaign(conn, k)
            if cid is None:
                raise ValueError(f"Unknown campaign '{k}'")
            new[cid] = float(v)
        sc = model.STRETCH if scenario.lower().startswith("s") else model.BASE
        b, w = model.baseline_from_conn(conn)
        s_cur = model.Scenario(**{**sc.__dict__, "allocation": cur})
        s_new = model.Scenario(**{**sc.__dict__, "allocation": new})
        p0, p1 = model.project(b, w, cur, s_cur), model.project(b, w, new, s_new)
        delta_budget = sum(new.values()) - sum(cur.values())
        delta_att = p1["total"] - p0["total"]
        per = (p1["table"].attended - p0["table"].attended).round(1)
        return {"scenario_assumptions": sc.name, "budget_before": sum(cur.values()), "budget_after": sum(new.values()),
                "attendees_before": round(p0["total"]), "attendees_after": round(p1["total"]),
                "delta_attendees": round(delta_att, 1), "delta_budget": delta_budget,
                "marginal_cost_per_extra_attendee": None if abs(delta_att) < 0.5 else round(delta_budget / delta_att, 1),
                "per_campaign_attendee_delta": per.to_dict(),
                "caveat": "Model assumptions (elasticity, show-up lift, overlap haircut) are judgement calls, not measured effects."}

    @traced
    def list_tasks(status: str = "", campaign_id: str = "") -> dict:
        """Tasks in the management tool. Optional filters: status (todo/in_progress/blocked/done), campaign_id."""
        df = store.list_tasks(conn, status or None, _match_campaign(conn, campaign_id) or (campaign_id or None))
        return {"count": len(df), "tasks": df[["task_id", "title", "campaign_id", "owner", "due_date", "priority", "status", "source"]]}

    @traced
    def run_sql(query: str) -> dict:
        """Run ONE read-only SQL SELECT on the SQLite database for questions the other tools do not cover.
        Use the schema in the system prompt. Prefer the views v_reg_latest (latest status per external registration),
        v_attendees (unique admitted+verified external attendees) and v_campaign_final. Max 200 rows returned.
        Remember: filter people.person_type='external'; the base tables are de-duplicated but not snapshot-filtered."""
        q = query.strip().rstrip(";")
        if ";" in q or not re.match(r"^(select|with)\b", q, re.I):
            raise ValueError("Only a single SELECT statement is allowed.")
        if re.search(r"\b(insert|update|delete|drop|alter|attach|detach|pragma|create|replace|vacuum)\b", q, re.I):
            raise ValueError("Read-only: write keywords are not allowed.")
        path = conn.execute("PRAGMA database_list").fetchone()[2]
        ro = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            cur = ro.execute(f"SELECT * FROM ({q}) LIMIT 200")
            cols = [c[0] for c in cur.description]
            rows = cur.fetchall()
        finally:
            ro.close()
        return {"columns": cols, "rows": [list(r) for r in rows], "row_count": len(rows),
                "note": "Truncated at 200 rows." if len(rows) == 200 else ""}

    @traced
    def propose_task(title: str, description: str, campaign_id: str = "", owner: str = "", due_date: str = "",
                     priority: str = "medium", rationale: str = "") -> dict:
        """Propose a trackable task for the management tool. NOTHING is created until the user clicks Confirm in the
        UI. Provide a concrete title, description with the metric/threshold to act on, campaign_id (C01..C08 or empty),
        owner name, due_date YYYY-MM-DD, priority low/medium/high, and a data-based rationale."""
        cid = _match_campaign(conn, campaign_id) if campaign_id else None
        if campaign_id and not cid:
            raise ValueError(f"Unknown campaign '{campaign_id}'")
        pid = store.create_proposal(conn, "task", {"title": title, "description": description, "campaign_id": cid,
                                                   "owner": owner or None, "due_date": due_date or None,
                                                   "priority": priority if priority in store.PRIORITIES else "medium"}, rationale)
        ctx.proposals.append(pid)
        return {"proposal_id": pid, "status": "pending user confirmation", "note": "Tell the user it is awaiting their confirmation."}

    @traced
    def propose_budget_change(campaign_id: str, new_budget_inr: float, rationale: str = "") -> dict:
        """Propose changing a campaign's PLANNED budget (next-edition plan). NOT applied until the user confirms in the UI."""
        cid = _match_campaign(conn, campaign_id)
        if not cid:
            raise ValueError(f"Unknown campaign '{campaign_id}'")
        pid = store.create_proposal(conn, "budget_change", {"campaign_id": cid, "new_budget_inr": float(new_budget_inr)}, rationale)
        ctx.proposals.append(pid)
        return {"proposal_id": pid, "status": "pending user confirmation"}

    return [get_overview, get_campaign_metrics, get_scale_change_stop, get_trend, get_momentum, get_projection,
            get_engagement, get_plan_and_tasks_summary, simulate_budget_change, list_tasks, run_sql,
            propose_task, propose_budget_change]


def schema_text(conn) -> str:
    out = []
    for r in conn.execute("SELECT name, type FROM sqlite_master WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%' ORDER BY type DESC, name"):
        cols = [c[1] for c in conn.execute(f"PRAGMA table_info({r[0]})")]
        out.append(f"- {r[1]} {r[0]}({', '.join(cols)})")
    return "\n".join(out)


def system_prompt(ctx: Context) -> str:
    plan_date = store.get_meta(ctx.conn, "plan_date")
    return f"""You are the embedded Growth Analyst & Copilot for CampusFest attendee growth (city: {store.get_meta(ctx.conn, 'city')}).
You help a growth team measure, diagnose and decide, using ONLY the application's live data via your tools.

CONTEXT
- The dashboard snapshot date is {ctx.as_of} (end of day, IST). Data covers {FIRST_DAY} to {LAST_DAY} for CampusFest 2025 (fictional, synthetic).
  The 2025 data is the BASELINE. The team's plan, budgets, ideas and tasks are for the NEXT edition (simulated 'today' = {plan_date}).
- 2025 snapshots must never use information after the snapshot date.

HARD RULES
1. Every number must come from a tool result in this conversation. Call tools before answering; never use outside knowledge for figures. If you
   computed something yourself (ratio, difference), show the formula and inputs.
2. Label each claim as EVIDENCE (from tool data), CALCULATION, or ASSUMPTION/INFERENCE. Correlation in this data is observational; never claim a
   campaign CAUSED attendance. Reach/impressions are anonymous activity, NOT unique people. Registration starts, sessions and clicks are anonymous.
3. If the data cannot answer (e.g. revenue per attendee, ticket prices, demographics, college names, reasons for cancellation, actual capacity,
   sponsor contribution, dates outside the history, individual-level causal effects), say clearly "the data cannot answer this", explain what is
   missing and what data would answer it, and offer the closest answerable analysis. Never fabricate.
4. If a tool returns INSUFFICIENT DATA or small samples (<100 people), say the evidence is weak.
5. You cannot change data. To act, call propose_task or propose_budget_change; they create PROPOSALS awaiting user confirmation in the UI.
   Never say a task/budget was created or changed; say it is proposed and waiting for confirmation.
6. Follow-up questions refer to the earlier conversation; keep the same snapshot date unless the user changes it.
7. Be concise: lead with the answer, then evidence, then recommendation with trade-offs, then caveats. Use INR (Rs) and percentages with the underlying counts.

KEY DEFINITIONS (already implemented in the tools)
- identified = external people first seen; registered = external registrations; confirmed = latest status 'confirmed';
  attended = UNIQUE external people admitted+verified; walk-ins = attended without a campaign (unattributed).
- Cost per attendee = spend / attended (post-event). Pre-event use cost per confirmed registration (compared with the portfolio median).
- Targets/budget have versions; the tool returns the version effective on the snapshot date.
- Internal, staff and test people are excluded. Duplicate import rows are removed.

DATABASE SCHEMA (for run_sql)
{schema_text(ctx.conn)}
"""


class AnalystError(Exception):
    pass


def get_api_key(secrets=None, override: str = "") -> str:
    if override:
        return override
    for k in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        if os.environ.get(k):
            return os.environ[k]
        try:
            if secrets and k in secrets:
                return secrets[k]
        except Exception:
            pass
    return ""


def ask(conn, as_of: str, history: list[dict], user_msg: str, api_key: str, models: list[str] | None = None,
        client=None) -> dict:
    """Run one chat turn. history = [{'role': 'user'|'assistant', 'content': str}, ...] (prior turns, text only).
    `client` can be injected for tests. Returns {'answer', 'trace', 'proposal_ids', 'model'}."""
    from google import genai
    from google.genai import types

    ctx = Context(conn=conn, as_of=as_of)
    tools = make_tools(ctx)
    client = client or genai.Client(api_key=api_key)
    contents = [types.Content(role="user" if h["role"] == "user" else "model", parts=[types.Part(text=h["content"])])
                for h in history]
    contents.append(types.Content(role="user", parts=[types.Part(text=user_msg)]))
    cfg = types.GenerateContentConfig(
        system_instruction=system_prompt(ctx), tools=tools, temperature=0.2,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(maximum_remote_calls=10))
    last_err = None
    for name in (models or DEFAULT_MODELS):
        try:
            ctx.trace.clear()
            resp = client.models.generate_content(model=name, contents=contents, config=cfg)
            text = (resp.text or "").strip()
            if not text:
                text = "I could not produce an answer from the data. Please rephrase the question."
            return {"answer": text, "trace": ctx.trace, "proposal_ids": list(ctx.proposals), "model": name}
        except Exception as e:  # try next model on quota / availability errors
            last_err = e
            continue
    raise AnalystError(f"Gemini call failed: {last_err}")
