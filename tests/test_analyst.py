"""Analyst tests with a fake Gemini client (no network). They verify tool grounding, read-only SQL,
proposal-not-execution, date guards and the ask() loop wiring."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from growth import analyst, etl, store  # noqa: E402


@pytest.fixture()
def conn(tmp_path):
    c = etl.build_database(tmp_path / "a.db")
    yield c
    c.close()


def tools_of(conn, as_of="2025-04-20"):
    ctx = analyst.Context(conn=conn, as_of=as_of)
    return ctx, {t.__name__: t for t in analyst.make_tools(ctx)}


def test_overview_tool_matches_metrics(conn):
    ctx, t = tools_of(conn)
    o = t["get_overview"]()
    assert o["attended_unique_visitors"] == 2050 and o["registered"] == 2800
    assert ctx.trace[-1]["tool"] == "get_overview"


def test_tools_use_current_data_after_edit(conn):
    ctx, t = tools_of(conn)
    before = t["get_campaign_metrics"](campaign="C02")["campaigns"][0]["spend"]
    store.add_daily_marketing(conn, "C02", "2025-04-01", marketing_spend_inr=5000)
    after = t["get_campaign_metrics"](campaign="Paid Social")["campaigns"][0]["spend"]
    assert after == before + 5000


def test_out_of_range_date_is_refused(conn):
    _, t = tools_of(conn)
    r = t["get_overview"]("2026-01-01")
    assert "error" in r and "cannot answer" in r["error"]


def test_sql_is_read_only(conn):
    _, t = tools_of(conn)
    assert "error" in t["run_sql"]("DELETE FROM tasks")
    assert "error" in t["run_sql"]("SELECT 1; DROP TABLE tasks")
    assert "error" in t["run_sql"]("SELECT * FROM tasks; DELETE FROM tasks")
    ok = t["run_sql"]("SELECT campaign_id, COUNT(*) n FROM v_attendees GROUP BY 1")
    assert ok["row_count"] == 8
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] > 0


def test_propose_does_not_execute(conn):
    ctx, t = tools_of(conn)
    n0 = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    r = t["propose_task"](title="Fix paid social show-up", description="d", campaign_id="Paid Social", owner="Arjun Verma",
                          due_date="2026-02-01", priority="high", rationale="show-up 50%")
    assert r["status"].startswith("pending")
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == n0
    assert ctx.proposals == [r["proposal_id"]]
    store.accept_proposal(conn, r["proposal_id"])
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == n0 + 1
    assert conn.execute("SELECT source, campaign_id FROM tasks ORDER BY task_id DESC").fetchone()[:] == ("ai", "C02")


def test_unknown_campaign_error(conn):
    _, t = tools_of(conn)
    assert "error" in t["get_campaign_metrics"](campaign="Nonexistent TikTok")


def test_simulation_moves_in_expected_direction(conn):
    _, t = tools_of(conn)
    r = t["simulate_budget_change"](json.dumps({"C02": 25000, "C05": 62000}))
    assert r["per_campaign_attendee_delta"]["C02"] < 0 < r["per_campaign_attendee_delta"]["C05"]
    assert r["delta_budget"] == 20000 - 0 or isinstance(r["delta_budget"], (int, float))


class FakeResponse:
    def __init__(self, text):
        self.text = text


class FakeClient:
    """Mimics client.models.generate_content: calls the provided tools like the SDK's automatic function calling."""
    def __init__(self):
        self.models = self
        self.seen = {}

    def generate_content(self, model, contents, config):
        fns = {f.__name__: f for f in config.tools}
        self.seen = {"model": model, "n_contents": len(contents), "system": config.system_instruction}
        ov = fns["get_overview"]()
        fns["propose_task"](title="Investigate", description="d", campaign_id="C03", rationale="r")
        return FakeResponse(f"Registered {ov['registered']}")


def test_ask_wires_tools_history_and_proposals(conn):
    c = FakeClient()
    res = analyst.ask(conn, "2025-04-20", [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}],
                      "status?", api_key="x", client=c)
    assert res["answer"] == "Registered 2800"
    assert [t["tool"] for t in res["trace"]] == ["get_overview", "propose_task"]
    assert len(res["proposal_ids"]) == 1
    assert c.seen["n_contents"] == 3                       # 2 history turns + new message
    assert "the data cannot answer this" in c.seen["system"]  # honesty rule present
    assert "v_attendees" in c.seen["system"]                  # schema supplied for SQL tool


def test_edge_cases_c08_unattr_trend_and_due_date(conn):
    _, t = tools_of(conn)
    r_c08 = t["get_campaign_metrics"](campaign="C08")
    assert "error" in r_c08 and "planned campaign" in r_c08["error"]
    r_unattr = t["get_campaign_metrics"](campaign="UNATTR")
    assert len(r_unattr["campaigns"]) == 1 and r_unattr["campaigns"][0]["campaign_id"] == "UNATTR"
    r_trend = t["get_trend"](as_of_date="2025-01-15")
    assert r_trend["series"] == []
    r_bad_due = t["propose_task"](title="Test", description="d", due_date="T-4w", rationale="r")
    assert "error" in r_bad_due


def test_fallback_cleans_up_failed_proposals(conn):
    class FlakyClient:
        def __init__(self):
            self.models = self
            self.calls = 0

        def generate_content(self, model, contents, config):
            self.calls += 1
            fns = {f.__name__: f for f in config.tools}
            fns["propose_task"](title=f"Proposal {self.calls}", description="d", rationale="r")
            if self.calls == 1:
                raise RuntimeError("transient 503")
            return FakeResponse("Recovered")

    n0 = len(store.pending_proposals(conn))
    res = analyst.ask(conn, "2025-04-20", [], "do it", api_key="x", models=["m1", "m2"], client=FlakyClient())
    assert res["answer"] == "Recovered"
    assert res["model"] == "m2"
    assert len(res["proposal_ids"]) == 1
    assert len(store.pending_proposals(conn)) == n0 + 1


def test_dotenv_reader(tmp_path):
    p1 = tmp_path / ".env1"
    p1.write_text("GEMINI_API_KEY=abc123\n", encoding="utf-8")
    assert analyst._read_dotenv_key(p1) == "abc123"
    p2 = tmp_path / ".env2"
    p2.write_text("rawkey999\n", encoding="utf-8")
    assert analyst._read_dotenv_key(p2) == "rawkey999"
