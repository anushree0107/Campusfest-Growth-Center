"""Write operations (management layer). Everything is persisted in SQLite and
analytics are computed from the tables on every read, so edits flow straight into the dashboard."""
from __future__ import annotations

import json
import re

import pandas as pd

TASK_STATUSES = ["todo", "in_progress", "blocked", "done"]
PRIORITIES = ["low", "medium", "high"]
CAMPAIGN_STATUSES = ["planned", "active", "paused", "stopped"]


def now() -> str:
    return pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S")


def log(conn, action: str, detail: str = "", actor: str = "user") -> None:
    conn.execute("INSERT INTO change_log(at,actor,action,detail) VALUES (?,?,?,?)", (now(), actor, action, detail))
    conn.commit()


def get_meta(conn, key: str, default: str | None = None) -> str | None:
    r = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return r[0] if r else default


def set_meta(conn, key: str, value: str) -> None:
    conn.execute("INSERT INTO meta(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
    conn.commit()


# ---------------------------------------------------------------- tasks
def add_task(conn, title: str, description: str = "", campaign_id: str | None = None, idea_id: str | None = None,
             owner: str | None = None, due_date: str | None = None, priority: str = "medium",
             status: str = "todo", source: str = "manual") -> int:
    title = (title or "").strip()
    if not title:
        raise ValueError("Task title is required")
    if priority not in PRIORITIES:
        priority = "medium"
    if status not in TASK_STATUSES:
        status = "todo"
    if campaign_id:
        ok = conn.execute("SELECT 1 FROM campaign_plan WHERE campaign_id=?", (campaign_id,)).fetchone()
        if not ok:
            raise ValueError(f"Unknown campaign_id {campaign_id}")
    t = now()
    cur = conn.execute(
        "INSERT INTO tasks(title,description,campaign_id,idea_id,owner,due_date,priority,status,source,created_at,updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (title, description, campaign_id or None, idea_id or None, owner, due_date, priority, status, source, t, t))
    conn.commit()
    log(conn, "task_created", f"#{cur.lastrowid} {title} (source={source})", actor="ai" if source == "ai" else "user")
    return cur.lastrowid


def update_task(conn, task_id: int, **fields) -> None:
    allowed = {"title", "description", "campaign_id", "idea_id", "owner", "due_date", "priority", "status"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return
    if "status" in sets and sets["status"] not in TASK_STATUSES:
        raise ValueError("Invalid status")
    cols = ", ".join(f"{k}=?" for k in sets)
    conn.execute(f"UPDATE tasks SET {cols}, updated_at=? WHERE task_id=?", (*sets.values(), now(), int(task_id)))
    conn.commit()
    log(conn, "task_updated", f"#{task_id} {sets}")


def delete_task(conn, task_id: int) -> None:
    conn.execute("DELETE FROM tasks WHERE task_id=?", (int(task_id),))
    conn.commit()
    log(conn, "task_deleted", f"#{task_id}")


def list_tasks(conn, status: str | None = None, campaign_id: str | None = None) -> pd.DataFrame:
    sql, p = "SELECT * FROM tasks WHERE 1=1", []
    if status:
        sql += " AND status=?"; p.append(status)
    if campaign_id:
        sql += " AND campaign_id=?"; p.append(campaign_id)
    return pd.read_sql(sql + " ORDER BY CASE status WHEN 'done' THEN 1 ELSE 0 END, due_date", conn, params=p)


# ---------------------------------------------------------------- campaign plan
def update_campaign_plan(conn, campaign_id: str, **fields) -> None:
    allowed = {"budget_inr", "attendee_goal", "status", "owner", "notes", "name", "channel"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return
    if "budget_inr" in sets and float(sets["budget_inr"]) < 0:
        raise ValueError("Budget cannot be negative")
    if "status" in sets and sets["status"] not in CAMPAIGN_STATUSES:
        raise ValueError("Invalid campaign status")
    old = conn.execute("SELECT * FROM campaign_plan WHERE campaign_id=?", (campaign_id,)).fetchone()
    if old is None:
        raise ValueError(f"Unknown campaign_id {campaign_id}")
    cols = ", ".join(f"{k}=?" for k in sets)
    conn.execute(f"UPDATE campaign_plan SET {cols} WHERE campaign_id=?", (*sets.values(), campaign_id))
    conn.commit()
    log(conn, "campaign_plan_updated", f"{campaign_id}: " + ", ".join(f"{k}: {old[k]} -> {v}" for k, v in sets.items()))


def add_campaign(conn, name: str, channel: str, owner: str, budget_inr: float, attendee_goal: int, notes: str = "") -> str:
    ids = [r[0] for r in conn.execute("SELECT campaign_id FROM campaign_plan")]
    n = max(int(re.sub(r"\D", "", i) or 0) for i in ids) + 1
    cid = f"C{n:02d}"
    conn.execute("INSERT INTO campaign_plan VALUES (?,?,?,?,?,?,?,?,?)",
                 (cid, name, channel, owner, budget_inr, attendee_goal, "planned", "proposed", notes))
    conn.commit()
    log(conn, "campaign_created", f"{cid} {name} budget {budget_inr}")
    return cid


# ---------------------------------------------------------------- source data edits
def add_daily_marketing(conn, campaign_id: str, activity_date: str, impressions: int = 0, clicks: int = 0,
                        reported_reach: int = 0, landing_page_sessions: int = 0, registration_starts: int = 0,
                        marketing_spend_inr: float = 0, platform: str = "manual entry") -> str:
    """Append a daily marketing row (e.g. today's spend). Flows into funnel, cost metrics and flags."""
    c = conn.execute("SELECT campaign_name, owner_name, owner_id FROM campaigns WHERE campaign_id=?", (campaign_id,)).fetchone()
    if c is None:
        raise ValueError(f"campaign_id {campaign_id} is not a tracked acquisition campaign")
    aid = f"MAN-{campaign_id}-{pd.Timestamp(activity_date).strftime('%m%d')}-{pd.Timestamp.now().strftime('%H%M%S%f')[:9]}"
    conn.execute(
        "INSERT INTO daily_marketing(campaign_name,owner_name,activity_date,impressions,clicks,reported_reach,"
        "landing_page_sessions,registration_starts,marketing_spend_inr,platform,activity_id,campaign_id,owner_id)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (c["campaign_name"], c["owner_name"], pd.Timestamp(activity_date).strftime("%Y-%m-%d 00:00:00"),
         impressions, clicks, reported_reach, landing_page_sessions, registration_starts,
         marketing_spend_inr, platform, aid, campaign_id, c["owner_id"]))
    conn.commit()
    log(conn, "marketing_row_added", f"{campaign_id} {activity_date} spend={marketing_spend_inr} clicks={clicks}")
    return aid


def add_target_version(conn, effective_at: str, identified: int, registrations: int, visitors: int,
                       budget: float, label: str) -> None:
    conn.execute("INSERT INTO targets VALUES (?,?,?,?,?,?)",
                 (pd.Timestamp(effective_at).strftime("%Y-%m-%d %H:%M:%S"), identified, registrations, visitors, budget, label))
    conn.commit()
    log(conn, "target_version_added", f"{label} effective {effective_at}")


def update_setting(conn, key: str, value: float) -> None:
    old = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if old is None:
        raise ValueError(f"Unknown setting {key}")
    conn.execute("UPDATE settings SET value=? WHERE key=?", (float(value), key))
    conn.commit()
    log(conn, "setting_updated", f"{key}: {old[0]} -> {value}")


# ---------------------------------------------------------------- AI proposals (need user confirmation)
def create_proposal(conn, kind: str, payload: dict, rationale: str = "") -> int:
    if kind not in ("task", "budget_change"):
        raise ValueError("kind must be task or budget_change")
    if kind == "task" and not payload.get("title"):
        raise ValueError("task proposal needs a title")
    if kind == "budget_change":
        if not conn.execute("SELECT 1 FROM campaign_plan WHERE campaign_id=?", (payload.get("campaign_id"),)).fetchone():
            raise ValueError("budget_change needs a valid campaign_id")
        if float(payload.get("new_budget_inr", -1)) < 0:
            raise ValueError("new_budget_inr must be >= 0")
    cur = conn.execute("INSERT INTO proposals(kind,payload,rationale,status,created_at) VALUES (?,?,?,?,?)",
                       (kind, json.dumps(payload), rationale, "pending", now()))
    conn.commit()
    return cur.lastrowid


def pending_proposals(conn) -> pd.DataFrame:
    return pd.read_sql("SELECT * FROM proposals WHERE status='pending' ORDER BY proposal_id", conn)


def accept_proposal(conn, proposal_id: int) -> str:
    r = conn.execute("SELECT * FROM proposals WHERE proposal_id=? AND status='pending'", (int(proposal_id),)).fetchone()
    if r is None:
        raise ValueError("Proposal not found or already handled")
    p = json.loads(r["payload"])
    if r["kind"] == "task":
        tid = add_task(conn, p["title"], p.get("description", "") + (f"\n\nWhy: {r['rationale']}" if r["rationale"] else ""),
                       p.get("campaign_id"), p.get("idea_id"), p.get("owner"), p.get("due_date"),
                       p.get("priority", "medium"), "todo", source="ai")
        msg = f"Task #{tid} created: {p['title']}"
    else:
        update_campaign_plan(conn, p["campaign_id"], budget_inr=float(p["new_budget_inr"]))
        msg = f"Budget for {p['campaign_id']} set to Rs {float(p['new_budget_inr']):,.0f}"
    conn.execute("UPDATE proposals SET status='accepted' WHERE proposal_id=?", (int(proposal_id),))
    conn.commit()
    log(conn, "proposal_accepted", f"#{proposal_id}: {msg}", actor="user (confirmed AI proposal)")
    return msg


def reject_proposal(conn, proposal_id: int) -> None:
    conn.execute("UPDATE proposals SET status='rejected' WHERE proposal_id=? AND status='pending'", (int(proposal_id),))
    conn.commit()
    log(conn, "proposal_rejected", f"#{proposal_id}")
