"""Tests: metrics are verified against an independent pandas computation on the raw Excel."""
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from growth import etl, metrics as m, model, store  # noqa: E402


@pytest.fixture(scope="module")
def raw():
    return pd.read_excel(etl.XLSX_PATH, sheet_name=None)


@pytest.fixture()
def conn(tmp_path):
    c = etl.build_database(tmp_path / "t.db")
    yield c
    c.close()


def test_dedupe_counts(conn):
    log = pd.read_sql("SELECT * FROM etl_log", conn).set_index("table_name")
    assert log.loc["daily_marketing", "duplicates_removed"] == 2
    assert log.loc["reg_history", "duplicates_removed"] == 4
    assert log.loc["checkins", "duplicates_removed"] == 20


def test_final_headlines_match_independent_calc(conn, raw):
    people = raw["People"].drop_duplicates("person_id")
    ext = set(people[people.person_type == "external"].person_id)
    ck = raw["Check-ins"].drop_duplicates("event_id")
    ck = ck[(ck.result == "admitted") & (ck.verification == "verified") & ck.person_id.isin(ext)]
    spend = raw["Daily Marketing"].drop_duplicates("activity_id").marketing_spend_inr.sum()
    o = m.overview(conn, "2025-04-20")
    assert o["identified"] == len(ext) == 3625
    assert o["attended_unique_visitors"] == ck.person_id.nunique() == 2050   # unique people, not rows
    assert len(ck) > ck.person_id.nunique()                                  # repeat admissions exist
    assert o["spend_inr"] == spend == 274000
    assert o["registered"] == 2800                                           # internal registrants excluded


def test_no_future_leakage(conn):
    early = m.overview(conn, "2025-04-11")
    assert early["attended_unique_visitors"] == 0          # event has not started
    mid = m.overview(conn, "2025-03-01")
    final = m.overview(conn, "2025-04-20")
    assert mid["registered"] < final["registered"]
    assert mid["spend_inr"] < final["spend_inr"]
    # cancellations after the snapshot must not appear before they happen
    assert mid["cancelled"] <= final["cancelled"]


def test_target_version_switches_on_effective_date(conn):
    assert m.targets_as_of(conn, "2025-03-14")["registration_goal"] == 2800
    assert m.targets_as_of(conn, "2025-03-15")["registration_goal"] == 3200   # effective 15 Mar 09:00, end-of-day snapshot


def test_latest_status_not_double_counted(conn):
    df = m.campaign_funnel(conn, "2025-04-20")
    # leena kapoor: confirmed -> cancelled -> confirmed; still one registration
    assert df.registered.sum() == 2800
    assert df.confirmed.sum() + df.cancelled.sum() + df.waitlisted.sum() + df.interested_only.sum() == 2800


def test_walk_ins_are_unattributed(conn):
    df = m.campaign_funnel(conn, "2025-04-20")
    assert df.loc["UNATTR", "attended"] == 125
    assert df.drop(index="UNATTR").attended.sum() == 1925


def test_flags_post_event(conn):
    f = m.flags(conn, "2025-04-20")
    assert f.loc["C05", "decision"] == "SCALE"
    assert f.loc["C03", "decision"] == "STOP"
    assert f.loc["C02", "decision"] == "CHANGE"


def test_edit_flows_into_analytics(conn):
    before = m.campaign_funnel(conn, "2025-04-20").loc["C05"]
    store.add_daily_marketing(conn, "C05", "2025-04-10", marketing_spend_inr=40000, clicks=100)
    after = m.campaign_funnel(conn, "2025-04-20").loc["C05"]
    assert after.spend == before.spend + 40000
    assert after.cost_per_attendee > before.cost_per_attendee
    assert m.flags(conn, "2025-04-20").loc["C05", "decision"] != "SCALE"   # flag reacts to the changed data


def test_settings_change_flags(conn):
    store.update_setting(conn, "scale_cpa_max", 70)
    assert m.flags(conn, "2025-04-20").loc["C05", "decision"] == "SCALE"   # 69 <= 70
    assert m.flags(conn, "2025-04-20").loc["C06", "decision"] == "HOLD"    # 72 > 70


def test_task_crud_and_proposal_confirmation(conn):
    n0 = len(store.list_tasks(conn))
    pid = store.create_proposal(conn, "task", {"title": "Test task", "campaign_id": "C02"}, "because")
    assert len(store.list_tasks(conn)) == n0            # nothing happens without confirmation
    store.accept_proposal(conn, pid)
    t = store.list_tasks(conn)
    assert len(t) == n0 + 1 and t.iloc[:].query("title=='Test task'").source.iloc[0] == "ai"
    with pytest.raises(ValueError):
        store.accept_proposal(conn, pid)                # cannot be applied twice
    store.update_task(conn, int(t.query("title=='Test task'").task_id.iloc[0]), status="done")
    assert store.list_tasks(conn, status="done").shape[0] == 1


def test_budget_proposal_updates_plan(conn):
    pid = store.create_proposal(conn, "budget_change", {"campaign_id": "C05", "new_budget_inr": 60000})
    store.accept_proposal(conn, pid)
    assert conn.execute("SELECT budget_inr FROM campaign_plan WHERE campaign_id='C05'").fetchone()[0] == 60000


def test_model_scenarios(conn):
    b, w = model.baseline_from_conn(conn)
    assert int(b.attended.sum() + w) == 2050
    s = model.scenario_summary(b, w).set_index("scenario")
    assert s.loc["Base case", "attendees"] < s.loc["Stretch target", "attendees"]
    assert s.loc["Base case", "attendees"] > 2050
    assert s.loc["Base case", "budget_inr"] == 300000
    # same allocation as 2025 with no recovery = baseline
    p = model.project(b, w, b.spend_inr.to_dict(), model.Scenario("flat", walk_ins=125, overlap_haircut=0))
    assert round(p["total"]) == 2050
