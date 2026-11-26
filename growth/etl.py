"""ETL: load the provided Attendee Dataset.xlsx into SQLite.

Rules applied (from the dataset 'Read me'):
  * Exact repeats that share a stable ID (import retries) are de-duplicated.
  * Timestamps are kept as naive ISO text in Asia/Kolkata (source is +05:30).
  * Person type is preserved so metrics can restrict to `external` people.
Raw tables are loaded as-is (after de-dup); business logic lives in metrics.py.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
XLSX_PATH = DATA_DIR / "Attendee Dataset.xlsx"
DB_PATH = DATA_DIR / "growth.db"

# sheet -> (table, stable id column)
SHEETS = {
    "Event": ("event", None),
    "Targets": ("targets", "effective_at"),
    "Campaigns": ("campaigns", "campaign_id"),
    "Daily Marketing": ("daily_marketing", "activity_id"),
    "People": ("people", "person_id"),
    "Interest and Engagement": ("interactions", "interaction_id"),
    "Registrations": ("registrations", "registration_id"),
    "Registration History": ("reg_history", "history_id"),
    "Check-ins": ("checkins", "event_id"),
}

# Tables that hold the immutable source data (re-created on reset).
SOURCE_TABLES = [t for t, _ in SHEETS.values()]
# Tables that hold the team's working state (kept across data reload).
APP_SCHEMA = """
CREATE TABLE IF NOT EXISTS campaign_plan (
    campaign_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    channel TEXT,
    owner TEXT,
    budget_inr REAL NOT NULL DEFAULT 0,
    attendee_goal INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active',
    kind TEXT NOT NULL DEFAULT 'dataset',   -- dataset | proposed
    notes TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS tasks (
    task_id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    description TEXT DEFAULT '',
    campaign_id TEXT,
    idea_id TEXT,
    owner TEXT,
    due_date TEXT,
    priority TEXT DEFAULT 'medium',
    status TEXT NOT NULL DEFAULT 'todo',    -- todo | in_progress | blocked | done
    source TEXT DEFAULT 'manual',           -- manual | ai
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS proposals (
    proposal_id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,                     -- task | budget_change
    payload TEXT NOT NULL,                  -- JSON
    rationale TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending', -- pending | accepted | rejected
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value REAL NOT NULL,
    description TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS ideas (
    idea_id TEXT PRIMARY KEY,
    rank INTEGER,
    name TEXT NOT NULL,
    concept TEXT,
    reach INTEGER, impact REAL, confidence REAL, effort REAL,
    budget_inr REAL, expected_attendees INTEGER,
    start_date TEXT, end_date TEXT,
    kpi TEXT, scale_rule TEXT, change_rule TEXT, stop_rule TEXT,
    campaign_id TEXT,
    decision TEXT DEFAULT 'invest'          -- invest | test | reject
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS change_log (
    log_id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    actor TEXT,
    action TEXT NOT NULL,
    detail TEXT
);
"""


def _iso(df: pd.DataFrame) -> pd.DataFrame:
    """Datetime columns -> ISO text ('YYYY-MM-DD HH:MM:SS'); dates stay sortable."""
    out = df.copy()
    for col in out.columns:
        if pd.api.types.is_datetime64_any_dtype(out[col]):
            out[col] = out[col].dt.strftime("%Y-%m-%d %H:%M:%S")
    return out


def connect(db_path: Path | str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def load_source(conn: sqlite3.Connection, xlsx_path: Path | str = XLSX_PATH) -> dict:
    """(Re)load the immutable source tables. Returns a de-dup log."""
    sheets = pd.read_excel(xlsx_path, sheet_name=None)
    log = {}
    cur = conn.cursor()
    for t in SOURCE_TABLES:
        cur.execute(f"DROP TABLE IF EXISTS {t}")
    for sheet, (table, key) in SHEETS.items():
        df = sheets[sheet].dropna(how="all")
        n_raw = len(df)
        if key and key in df.columns:
            df = df.drop_duplicates(subset=[key], keep="first")
        log[table] = {"rows_raw": n_raw, "rows_loaded": len(df), "duplicates_removed": n_raw - len(df)}
        _iso(df).to_sql(table, conn, index=False, if_exists="replace")
    cur.executescript(
        """
        CREATE INDEX IF NOT EXISTS ix_people_id ON people(person_id);
        CREATE INDEX IF NOT EXISTS ix_reg_id ON registrations(registration_id);
        CREATE INDEX IF NOT EXISTS ix_hist_reg ON reg_history(registration_id, effective_at);
        CREATE INDEX IF NOT EXISTS ix_chk_person ON checkins(person_id);
        CREATE INDEX IF NOT EXISTS ix_dm_c ON daily_marketing(campaign_id, activity_date);
        """
    )
    pd.DataFrame(
        [{"table_name": k, **v} for k, v in log.items()]
    ).to_sql("etl_log", conn, index=False, if_exists="replace")
    conn.commit()
    return log


def create_views(conn: sqlite3.Connection) -> None:
    """Convenience views for the chatbot's SQL tool (final-state, external only)."""
    conn.executescript(
        """
        DROP VIEW IF EXISTS v_reg_latest;
        CREATE VIEW v_reg_latest AS
        SELECT r.registration_id, r.person_id, r.campaign_id, r.registered_at,
               (SELECT h.status FROM reg_history h
                 WHERE h.registration_id = r.registration_id
                 ORDER BY h.effective_at DESC, h.history_id DESC LIMIT 1) AS latest_status
        FROM registrations r JOIN people p ON p.person_id = r.person_id
        WHERE p.person_type = 'external';

        DROP VIEW IF EXISTS v_attendees;
        CREATE VIEW v_attendees AS
        SELECT c.person_id, MIN(c.occurred_at) AS first_admitted_at,
               MAX(c.campaign_id) AS campaign_id, MAX(c.registration_id) AS registration_id
        FROM checkins c JOIN people p ON p.person_id = c.person_id
        WHERE p.person_type = 'external' AND c.result = 'admitted' AND c.verification = 'verified'
        GROUP BY c.person_id;

        DROP VIEW IF EXISTS v_campaign_final;
        CREATE VIEW v_campaign_final AS
        SELECT cp.campaign_id, cp.campaign_name,
          (SELECT COUNT(*) FROM people p WHERE p.person_type='external' AND p.first_campaign_id=cp.campaign_id) AS identified,
          (SELECT COUNT(*) FROM v_reg_latest r WHERE r.campaign_id=cp.campaign_id) AS registered,
          (SELECT COUNT(*) FROM v_reg_latest r WHERE r.campaign_id=cp.campaign_id AND r.latest_status='confirmed') AS confirmed,
          (SELECT COUNT(*) FROM v_attendees a WHERE a.campaign_id=cp.campaign_id) AS attended,
          (SELECT COALESCE(SUM(marketing_spend_inr),0) FROM daily_marketing d WHERE d.campaign_id=cp.campaign_id) AS spend_inr
        FROM campaigns cp;
        """
    )
    conn.commit()


DEFAULT_SETTINGS = [
    # Post-event (outcome) rules
    ("scale_cpa_max", 120, "SCALE if cost per attendee <= this (INR) ..."),
    ("scale_showup_min", 0.70, "... and show-up rate (attended / registered) >= this"),
    ("change_cpa_max", 150, "CHANGE if cost per attendee > this (INR) ..."),
    ("change_showup_min", 0.65, "... or show-up rate < this"),
    ("stop_cpa_max", 300, "STOP if cost per attendee > this (INR)"),
    # Pre-event (leading indicator) rules
    ("scale_cpc_ratio", 0.90, "Pre-event: SCALE if cost per confirmed registration <= this x portfolio median ..."),
    ("scale_regrate_min", 0.80, "Pre-event: ... and registered / identified >= this"),
    ("change_cpc_ratio", 1.25, "Pre-event: CHANGE if cost per confirmed registration > this x portfolio median"),
    ("change_cancel_max", 0.10, "Pre-event: CHANGE if cancellation rate > this"),
    ("stop_cpc_ratio", 2.00, "Pre-event: STOP if cost per confirmed registration > this x portfolio median"),
    ("min_sample", 100, "Minimum identified people before a flag is trusted"),
    ("assumed_showup", 0.75, "ASSUMPTION: show-up rate of confirmed registrants used for pre-event projections"),
    ("capacity_cap", 3500, "ASSUMPTION: max unique external attendees the venue can host over 2 days"),
]


def seed_app_state(conn: sqlite3.Connection, force: bool = False) -> None:
    """Seed campaign plan, settings, ideas and starter tasks (idempotent unless force)."""
    from . import model  # local import to avoid cycles

    conn.executescript(APP_SCHEMA)
    cur = conn.cursor()
    if force:
        for t in ("campaign_plan", "tasks", "proposals", "settings", "ideas", "change_log", "meta"):
            cur.execute(f"DELETE FROM {t}")
    if cur.execute("SELECT COUNT(*) FROM meta").fetchone()[0] == 0:
        cur.executemany("INSERT INTO meta VALUES (?,?)", [
            ("next_event_date", "2026-04-11"),   # ASSUMPTION: next edition, same April weekend
            ("plan_date", "2026-01-19"),         # simulated 'today' = T-12 weeks
            ("city", "Bengaluru"),
        ])
    if cur.execute("SELECT COUNT(*) FROM settings").fetchone()[0] == 0:
        cur.executemany(
            "INSERT INTO settings(key,value,description) VALUES (?,?,?)", DEFAULT_SETTINGS
        )
    if cur.execute("SELECT COUNT(*) FROM campaign_plan").fetchone()[0] == 0:
        camps = pd.read_sql("SELECT * FROM campaigns", conn)
        base_budget = model.BASE_ALLOCATION
        goals = model.base_goals_by_campaign(conn)
        for _, r in camps.iterrows():
            cur.execute(
                "INSERT INTO campaign_plan VALUES (?,?,?,?,?,?,?,?,?)",
                (r.campaign_id, r.campaign_name, r.channel, r.owner_name,
                 base_budget.get(r.campaign_id, 0), int(round(goals.get(r.campaign_id, 0))),
                 "active", "dataset", ""),
            )
        cur.execute(
            "INSERT INTO campaign_plan VALUES (?,?,?,?,?,?,?,?,?)",
            ("C08", "No-show Recovery", "reminders", "Dev Malhotra",
             base_budget.get("C08", 0), 0, "planned", "proposed",
             "WhatsApp/SMS reminders, waitlist back-fill and cancel-and-rebook flow. "
             "Does not acquire people; lifts show-up of existing registrants."),
        )
    if cur.execute("SELECT COUNT(*) FROM ideas").fetchone()[0] == 0:
        for row in model.make_ideas(conn):
            cur.execute(
                "INSERT INTO ideas VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (row["idea_id"], row["rank"], row["name"], row["concept"], row["reach"],
                 row["impact"], row["confidence"], row["effort"], row["budget_inr"],
                 row["expected_attendees"], row["start_date"], row["end_date"], row["kpi"],
                 row["scale_rule"], row["change_rule"], row["stop_rule"], row["campaign_id"],
                 row["decision"]),
            )
    if cur.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0:
        now = pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S")
        plan_date = pd.Timestamp(cur.execute("SELECT value FROM meta WHERE key='plan_date'").fetchone()[0])
        for t in model.build_starter_tasks(plan_date):
            cur.execute(
                "INSERT INTO tasks(title,description,campaign_id,idea_id,owner,due_date,priority,status,source,created_at,updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (t["title"], t["description"], t["campaign_id"], t["idea_id"], t["owner"],
                 t["due_date"], t["priority"], t["status"], "manual", now, now),
            )
    conn.commit()


def build_database(db_path: Path | str = DB_PATH, xlsx_path: Path | str = XLSX_PATH,
                   fresh: bool = True) -> sqlite3.Connection:
    """Create the database from the Excel file."""
    db_path = Path(db_path)
    if fresh and db_path.exists():
        db_path.unlink()
    conn = connect(db_path)
    load_source(conn, xlsx_path)
    create_views(conn)
    seed_app_state(conn)
    return conn


def reset_database(conn: sqlite3.Connection, xlsx_path: Path | str = XLSX_PATH) -> None:
    """Restore the original dataset and the seeded plan/tasks/ideas/settings (discard all edits)."""
    load_source(conn, xlsx_path)
    create_views(conn)
    conn.executescript(APP_SCHEMA)
    seed_app_state(conn, force=True)


def get_conn(db_path: Path | str = DB_PATH) -> sqlite3.Connection:
    """Open the DB, building it on first run."""
    if not Path(db_path).exists():
        return build_database(db_path)
    conn = connect(db_path)
    conn.executescript(APP_SCHEMA)
    # forward-compatible: add any new default settings without touching user edits
    conn.executemany("INSERT OR IGNORE INTO settings(key,value,description) VALUES (?,?,?)", DEFAULT_SETTINGS)
    conn.commit()
    return conn


if __name__ == "__main__":
    c = build_database()
    print(pd.read_sql("SELECT * FROM etl_log", c).to_string(index=False))
