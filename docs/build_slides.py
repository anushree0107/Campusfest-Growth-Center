"""Builds the 3-slide strategy PDF from the SAME model/database the app uses.
Usage: python docs/build_slides.py   (needs Microsoft Edge or Chrome for HTML -> PDF)"""
from __future__ import annotations

import base64
import io
import subprocess
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from growth import etl, model, metrics as M  # noqa: E402

NAME = "Anushree"          # <- put your full name here
OUT_HTML = ROOT / "docs" / "CampusFest_Growth_Strategy.html"
OUT_PDF = ROOT / "docs" / "CampusFest_Growth_Strategy.pdf"

conn = etl.get_conn()
b, walk = model.baseline_from_conn(conn)
base_p = model.project(b, walk, model.BASE.allocation, model.BASE)
str_p = model.project(b, walk, model.STRETCH.allocation, model.STRETCH)
base_ic = model.idea_contributions(b, walk, model.BASE)
str_ic = model.idea_contributions(b, walk, model.STRETCH)
ideas = model.make_ideas(conn)
ov = M.overview(conn, "2025-04-20")
f = M.campaign_funnel(conn, "2025-04-20")
tot = M.totals(f)
base_total = b.attended.sum() + walk
ss = model.scenario_summary(b, walk).set_index("scenario")


def n(x):
    return f"{x:,.0f}"


def png(fig) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=170, bbox_inches="tight", transparent=False, facecolor="white")
    plt.close(fig)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def bridge(ic, p, scen, title):
    scale = sum(ic[c] for c in ["C01", "C04", "C05", "C06", "C07"])
    cuts = ic["C02"] + ic["C03"]
    rec = ic["recovery"]
    walkins = scen.walk_ins - walk
    haircut = -(p["gross"] - base_total) * scen.overlap_haircut
    labels = ["2025\nactual", "Scale 5\nefficient\nchannels", "Cut Paid\n+ Creators", "No-show\nrecovery", "Walk-ins", "Overlap\nhaircut", title]
    vals = [base_total, scale, cuts, rec, walkins, haircut]
    fig, ax = plt.subplots(figsize=(5.8, 1.85))
    run = 0
    for i, v in enumerate(vals):
        if i == 0:
            ax.bar(i, v, color="#8a8f98")
            run = v
            ax.text(i, v + 40, n(v), ha="center", fontsize=8, fontweight="bold")
        else:
            ax.bar(i, v, bottom=run if v >= 0 else run + v, color="#1b9e4b" if v >= 0 else "#d62d3a")
            ax.text(i, max(run, run + v) + 40, f"{v:+,.0f}", ha="center", fontsize=8)
            run += v
    ax.bar(len(vals), run, color="#1f4e9a")
    ax.text(len(vals), run + 40, n(run), ha="center", fontsize=9, fontweight="bold")
    ax.set_xticks(range(len(labels)))
    ax.set_xticklabels(labels, fontsize=6.5)
    ax.set_ylim(1500, max(run, base_total + scale) * 1.12)
    ax.spines[["top", "right"]].set_visible(False)
    ax.tick_params(axis="y", labelsize=7)
    ax.set_title(f"Unique external attendees: 2025 -> {title}", fontsize=9, loc="left", fontweight="bold")
    return png(fig)


bridge_base = bridge(base_ic, base_p, model.BASE, "Base")
bridge_str = bridge(str_ic, str_p, model.STRETCH, "Stretch")

# ----- per-campaign 2025 table rows
camp_rows = ""
for cid, r in b.iterrows():
    camp_rows += (f"<tr><td>{r.campaign_name}</td><td>{n(r.spend_inr)}</td><td>{n(r.attended)}</td>"
                  f"<td><b>{r.cpa:,.0f}</b></td><td>{r.showup:.0%}</td></tr>")

paid_creator_share_spend = (b.loc[["C02", "C03"], "spend_inr"].sum()) / b.spend_inr.sum()
paid_creator_share_att = (b.loc[["C02", "C03"], "attended"].sum()) / b.attended.sum()

# ================================================================= SLIDE 1
slide1 = f"""
<section class="slide s1">
  <div class="top">
    <div><div class="kicker">RISA Growth APM Assignment | Challenge A: Attendee Growth | {NAME}</div>
    <h1>CampusFest 2026, Bengaluru: aim for <span class="hl">{n(str_p['total'])}</span> unique attendees (base {n(base_p['total'])}) vs {n(base_total)} in 2025</h1></div>
    <div class="badge">Stretch +{str_p['uplift_pct']:.0%}<br><small>Base +{base_p['uplift_pct']:.0%} on Rs {n(base_p['total_budget'])}</small></div>
  </div>
  <div class="grid3">
    <div class="card">
      <h2>Why believe it: 2025 baseline (SOURCED)</h2>
      <p class="s">Provided dataset, external people only, duplicates removed, attended = unique admitted + verified. Spend {n(tot.spend)} vs budget 3,00,000.</p>
      <table class="t">
        <tr><th>Stage</th><th>2025</th><th>Goal</th><th>Conv.</th></tr>
        <tr><td>Identified people</td><td>{n(ov['identified'])}</td><td>3,600</td><td></td></tr>
        <tr><td>Registered</td><td>{n(ov['registered'])}</td><td>3,200</td><td>{ov['registered']/ov['identified']:.0%}</td></tr>
        <tr><td>Confirmed</td><td>{n(ov['confirmed'])}</td><td></td><td>{ov['confirmed']/ov['registered']:.0%}</td></tr>
        <tr><td><b>Unique attendees</b></td><td><b>{n(ov['attended_unique_visitors'])}</b></td><td>2,200</td><td>{ov['attended_unique_visitors']/ov['registered']:.0%}</td></tr>
      </table>
      <p class="s">Cost per attendee Rs {ov['cost_per_attendee']:,.0f}. We reached 93% of the unique-visitor goal and 87.5% of the registration goal, and spent 91% of budget: the gap is conversion and show-up, not reach.</p>
      <table class="t">
        <tr><th>2025 channel</th><th>Spend</th><th>Attended</th><th>Rs/att.</th><th>Show-up</th></tr>{camp_rows}
      </table>
      <p class="key"><b>Insight:</b> Paid Social + Creators take {paid_creator_share_spend:.0%} of spend but deliver {paid_creator_share_att:.0%} of attendees at 3-5x the cost and 50% show-up. Cheap, high-show-up channels are under-funded. Strategy = reallocate + fix no-shows, not buy reach.</p>
    </div>
    <div class="card">
      <h2>Target audiences and why (sizes are ASSUMPTIONS)</h2>
      <table class="t">
        <tr><th>Audience</th><th>Potential size (A)</th><th>2026 base regs</th><th>Why prioritised</th></tr>
        <tr><td>Partner colleges / clubs</td><td>~400 active-fest clubs; sign 40</td><td>{n(base_p['table'].loc['C04','registered'])}</td><td>Group commitment: 86% show-up, Rs 81</td></tr>
        <tr><td>Ambassador peer networks</td><td>60 ambassadors x ~500 peers</td><td>{n(base_p['table'].loc['C01','registered'])}</td><td>Largest efficient volume (390 att.)</td></tr>
        <tr><td>Friends of registrants</td><td>~3,000 registrants x ~8 friends</td><td>{n(base_p['table'].loc['C05','registered'])}</td><td>Lowest cost: Rs 69, 79% show-up</td></tr>
        <tr><td>Search + community groups</td><td>~50k students (search / WhatsApp / Discord)</td><td>{n(base_p['table'].loc[['C06','C07'],'registered'].sum())}</td><td>Rs 72-97, 78-82% show-up</td></tr>
        <tr><td>Paid + creator audiences</td><td>~120k reachable</td><td>{n(base_p['table'].loc[['C02','C03'],'registered'].sum())}</td><td>Cut to retargeting + pay-per-registration</td></tr>
      </table>
      <p class="s"><b>City: Bengaluru</b> - densest tier-1 cluster of colleges, hostels and student communities, strong creator and club ecosystem, so partner-group and referral loops can saturate quickly. Serviceable audience assumed ~250k students (A); 4,330 identified people (stretch) = 1.7% penetration, so headroom is not the constraint, spend efficiency and show-up are.</p>
      <h2 style="margin-top:8px">Model (calibrated on 2025)</h2>
      <p class="s mono">identified(B) = identified25 x (B / spend25)^e &nbsp; (e = 0.5-0.8, diminishing returns, A)<br>attended = identified x reg-rate25 x show-up' &nbsp; (reminders lift show-up +8 pts base / +15 stretch on 50% channels, A)<br>total = 2025 + (campaigns + walk-ins - 2025) x (1 - 10% overlap), capped at venue cap 3,500 (A)</p>
    </div>
    <div class="card">
      <h2>Base case vs ambitious stretch</h2>
      <table class="t">
        <tr><th></th><th>2025</th><th>Base</th><th>Stretch</th></tr>
        <tr><td>Anonymous reach needed</td><td>{n(b.reach.sum())}</td><td>{n(ss.loc['Base case','reach_needed'])}</td><td>{n(ss.loc['Stretch target','reach_needed'])}</td></tr>
        <tr><td>Identified people</td><td>{n(ov['identified'])}</td><td>{n(ss.loc['Base case','identified']+walk)}</td><td>{n(ss.loc['Stretch target','identified']+150)}</td></tr>
        <tr><td>Registered</td><td>{n(ov['registered'])}</td><td>{n(ss.loc['Base case','registered'])}</td><td>{n(ss.loc['Stretch target','registered'])}</td></tr>
        <tr><td><b>Unique attendees</b></td><td><b>{n(base_total)}</b></td><td><b>{n(base_p['total'])}</b></td><td><b>{n(str_p['total'])}</b></td></tr>
        <tr><td>Budget (Rs)</td><td>{n(tot.spend)}</td><td>{n(base_p['total_budget'])}</td><td>{n(str_p['total_budget'])}</td></tr>
        <tr><td>Cost / attendee</td><td>Rs {ov['cost_per_attendee']:,.0f}</td><td>Rs {base_p['blended_cost_per_attendee']:,.0f}</td><td>Rs {str_p['blended_cost_per_attendee']:,.0f}</td></tr>
        <tr><td>Capacity use (cap 3,500, A)</td><td>{base_total/3500:.0%}</td><td>{base_p['total']/3500:.0%}</td><td>{str_p['total']/3500:.0%}</td></tr>
      </table>
      <img src="{bridge_base}" class="chart"><img src="{bridge_str}" class="chart">
      <p class="s">Base = same Rs 3.0L revised budget, reallocated (-Rs 48k Paid, -Rs 34k Creators, +Rs 26k unspent 2025 budget; +Rs 83k to the five efficient channels, Rs 15k recovery, Rs 10k test reserve). Stretch needs +Rs 60k (sponsor-funded, ties to Challenge B) and ideas outperforming (e + 0.10, +15 pt show-up lift). Both bracket uncertainty; the dashboard re-prices them live.</p>
    </div>
  </div>
  <div class="foot">SOURCED = provided CampusFest 2025 dataset (fictional). A = assumption (README). Sizes are order-of-magnitude, to be validated with AISHE/AICTE and club lists.</div>
</section>
"""

# ================================================================= SLIDE 2
ranked = [i for i in ideas if i["rank"]]
short = {
    "I3": ("No-show recovery ladder", "Paid/Creator registrants show up 50% vs 75-86% elsewhere; reminders (T-7/T-2/T-0 WhatsApp + calendar add), waitlist back-fill, cancel-and-rebook convert people we already paid for.",
           "10% holdout; ship to 500 low-show-up registrants first", "Show-up lift vs holdout, reminder CTR, waitlist fill", "T-4w to T-0"),
    "I1": ("Referral engine with perks", "Cheapest channel (Rs 69, 79% show-up) but only Rs 19k spent. Personal links + double-sided perks unlocked only when the friend attends (pays for attendance, not clicks).",
           "A/B reward copy in confirmation email (2 variants x 300 regs)", "Invites/registrant, invite-to-reg %, Rs/attendee", "T-10w to T-0"),
    "I4": ("Ambassador scale-up, tiered rewards", "390 attendees at Rs 108: biggest efficient volume. Tiers + weekly leaderboard turn 60 ambassadors into ~11 regs each (2025: 520 regs).",
           "20 pilot ambassadors with tiers vs 20 without for 2 weeks", "Active-ambassador %, regs per ambassador", "T-12w to T-0"),
    "I2": ("College partnership blitz", "Group registration links with club leads: 86% show-up (best) at Rs 81. 40 groups x ~16 regs.",
           "Pilot 8 colleges in first 2 weeks before scaling to 40", "Groups signed, regs/group, show-up %", "T-11w to T-1w"),
    "I5": ("Organic search + community groups", "Always-on and cheap (Rs 72-97, 78-82% show-up). 8 search-intent pages + seeded WhatsApp/Discord groups. Small but low-risk; keeps the funnel top filled.",
           "Publish 3 pages first; track sessions-to-reg-start", "Organic sessions, community joins, reg rate", "T-12w to T-0"),
}
rows2 = ""
for r in ranked:
    iid = r["idea_id"]
    nm, why, test, kpi, tl = short[iid]
    inc_b = base_ic["recovery"] if iid == "I3" else sum(base_ic[c] for c in r["campaign_id"].split(","))
    inc_s = str_ic["recovery"] if iid == "I3" else sum(str_ic[c] for c in r["campaign_id"].split(","))
    bud_s = model.STRETCH_ALLOCATION[r["campaign_id"].split(",")[0]] if iid != "I5" else sum(model.STRETCH_ALLOCATION[c] for c in ("C06", "C07"))
    if iid == "I3":
        bud_s = model.STRETCH_ALLOCATION["C08"]
    rows2 += (f"<tr><td class='rk'>{r['rank']}</td><td><b>{nm}</b><br><span class='s'>{why}</span></td>"
              f"<td class='c'><b>+{inc_b:,.0f}</b><br><span class='s'>stretch +{inc_s:,.0f}</span></td>"
              f"<td class='c'>Rs {r['budget_inr']/1000:,.0f}k<br><span class='s'>stretch {bud_s/1000:,.0f}k<br>{tl}</span></td>"
              f"<td><span class='s'><b>Test:</b> {test}<br><b>KPIs:</b> {kpi}</span></td>"
              f"<td class='c'>{r['score']:.0f}<br><span class='s'>{r['confidence']:.0%} conf.<br>{r['effort']:.0f} wks</span></td></tr>")

cut_loss = base_ic["C02"] + base_ic["C03"]
slide2 = f"""
<section class="slide">
  <div class="top"><div><div class="kicker">Slide 2 | Where will you place your bets?</div>
    <h1>Five bets ranked by attendees x confidence / effort: fix show-up and fund cheap loops first</h1></div>
    <div class="badge">Net +{base_p['total']-base_total:,.0f}<br><small>base vs 2025 after overlap</small></div></div>
  <div class="card wide">
    <table class="t big">
      <tr><th>#</th><th>Bet and why it should work (evidence = 2025 data)</th><th>Incremental attendees</th><th>Budget / timeline</th><th>How we test it, KPIs</th><th>Score</th></tr>
      {rows2}
    </table>
  </div>
  <div class="grid3 low">
    <div class="card"><h2>Prioritisation framework</h2>
      <p class="s">Score = incremental attendees (model, base) x confidence (our certainty the 2025 efficiency holds at higher spend) / effort (person-weeks). Attendees come from the funnel model: spend elasticity x 2025 conversion x show-up. Ideas overlap (a referred friend may also see an ambassador), so a 10% haircut is applied to the sum. Dependencies: #1 needs registration data from #2-#5 to target reminders; #2 and #4 share the same campus relationships and owner (Meera), so they are staggered.</p></div>
    <div class="card"><h2>Double down vs deprioritise</h2>
      <p class="s"><b>Double down:</b> #1 recovery (Rs 15k, highest score, uses already-paid registrations) and #2 referrals (Rs 42k, 2.2x its 2025 budget).<br>
      <b>Staged bets:</b> #3 ambassadors and #4 colleges scale only after pilots pass the thresholds on slide 3.<br>
      <b>Keep small:</b> #5 organic/community.<br>
      <b>Reject / avoid:</b> scaling Paid Social at Rs 274/attendee (cut Rs 93k to 45k, retargeting only), flat-fee creators at Rs 337/attendee (Rs 59k to 25k, pay-per-registration only; costs {abs(cut_loss):,.0f} attendees, already netted in the target), and buying more reach: reach is anonymous and does not prove attendance.</p></div>
    <div class="card"><h2>Constraints and risks considered</h2>
      <p class="s">Budget cap Rs 3.0L (stretch needs Rs 60k sponsor money); venue cap ~3,500 (A); team of ~5 owners; 12-week window. Diminishing returns on cheap channels (e 0.5-0.7) mean doubling budget does not double attendees, which is why gains are +{base_p['uplift_pct']:.0%} not +100%. Rs 10k is held as test reserve so winning pilots can be scaled mid-campaign without breaching the cap.</p></div>
  </div>
  <div class="foot">Incremental attendees are model outputs under stated assumptions (README). Budgets shown for the base case, with the stretch figure beneath.</div>
</section>
"""

# ================================================================= SLIDE 3
sig = {
    "I3": ("Lift in show-up from reminders", "Reminder opt-in low / WhatsApp template rejection", "Reminder CTR >= 25% in first 500 sends", "Cohort show-up >= holdout + 8 pts -> roll to all channels", "Lift 3-8 pts -> change timing/copy", "Lift <= 0 after 1,000 sends -> stop, reallocate Rs 15k"),
    "I1": ("Invites per registrant x invite conversion", "Perks cheap-to-game, low sharing", "Invites/registrant >= 0.5 by day 7", "Invites >= 0.8, conv >= 20%, Rs/att. <= 100 -> +25% budget", "Invites < 0.5 at day 10 -> change reward", "Rs/att. > 180 or conv < 8% for 7 days -> stop"),
    "I4": ("Active ambassadors x regs each", "Ambassador churn, uneven campuses", "Active rate >= 60% after week 2", "Regs/ambassador >= 10 -> recruit 20 more", "Active < 50% -> change onboarding/rewards", "Rs/att. > 160 for 10 days -> cap programme"),
    "I2": ("Groups signed x regs per group", "Club leads slow to commit; exam weeks", "8 pilot colleges signed in 14 days", "Regs/group >= 12 and show-up >= 80% -> +10 groups", "Regs/group < 8 at T-5w -> change to leader incentives", "Rs/att. > 160 for 10 days -> stop onboarding"),
    "I5": ("Search sessions -> registration starts", "SEO lag; communities go quiet", "3 pages indexed + 200 sessions by day 14", "Start-per-session >= 30% -> add 5 pages", "Sessions flat 14 days -> change topics", "Rs/att. > 150 for 14 days -> pause paid support"),
}
rows3 = ""
for r in ranked:
    cv, risk, early, sc_, ch_, st_ = sig[r["idea_id"]]
    rows3 += (f"<tr><td class='rk'>{r['rank']}</td><td><b>{short[r['idea_id']][0]}</b></td><td>{cv}</td><td>{risk}</td>"
              f"<td>{early}</td><td class='g'>{sc_}</td><td class='o'>{ch_}</td><td class='r'>{st_}</td></tr>")
pace = {d: M.overview(conn, d)["registered"] / 2800 for d in ("2025-03-14", "2025-03-28", "2025-04-04")}
slide3 = f"""
<section class="slide">
  <div class="top"><div><div class="kicker">Slide 3 | How will you know what is working?</div>
    <h1>Leading indicators decide scale / change / stop early; dashboard and AI turn them into tasks</h1></div>
    <div class="badge">Weekly loop<br><small>measure - diagnose - decide - act</small></div></div>
  <div class="card wide">
    <table class="t big">
      <tr><th>#</th><th>Bet</th><th>Critical variable</th><th>Biggest risk</th><th>Early signal (leading)</th><th>SCALE if</th><th>CHANGE if</th><th>STOP if</th></tr>
      {rows3}
    </table>
  </div>
  <div class="grid3 low">
    <div class="card"><h2>Portfolio guardrails (from 2025 pacing, SOURCED)</h2>
      <p class="s">In 2025 registrations reached <b>{pace['2025-03-14']:.0%}</b> of final at T-4w, <b>{pace['2025-03-28']:.0%}</b> at T-2w and <b>{pace['2025-04-04']:.0%}</b> at T-1w. For a base plan of {n(ss.loc['Base case','registered'])} registrations we need >= {n(ss.loc['Base case','registered']*pace['2025-03-14'])} at T-4w. <b>If pace &lt; 85% of that curve (&lt; {n(ss.loc['Base case','registered']*pace['2025-03-14']*0.85)}), trigger reallocation</b>: move Rs 20-30k from lowest-ranked live idea to the best SCALE channel. Pre-event flags compare cost per confirmed registration with the portfolio median (SCALE &lt;= 0.9x, CHANGE &gt; 1.25x, STOP &gt; 2x) because spend is front-loaded.</p></div>
    <div class="card"><h2>What happens when numbers move</h2>
      <p class="s"><b>Better than threshold:</b> add budget in 10-15% steps from the Rs 10k reserve + STOP channels; re-check marginal cost per extra attendee (model says it rises).<br>
      <b>Inside the change band:</b> change one variable at a time (reward, copy, timing) for 7 days, keep a holdout.<br>
      <b>Below stop line or CPA &gt; Rs 180 portfolio-wide:</b> freeze, reallocate, log the decision. Cancellations &gt; 10% of registrations: tighten reminder cadence and waitlist back-fill first.</p></div>
    <div class="card"><h2>Dashboard + AI in the loop</h2>
      <p class="s"><b>Dashboard</b> (Streamlit, saved to SQLite): funnel, targets vs actuals by effective version, campaign KPIs, weakest-stage diagnosis, scale/change/stop flags with editable thresholds, budget planner, task manager. Edit spend or a threshold and every number updates.<br>
      <b>AI analyst</b> (Gemini + tools on live data): answers "why are we below target?", simulates budget moves, says "data cannot answer" for revenue/demographics, and <i>proposes</i> tasks or budget changes that execute only after user confirmation.</p></div>
  </div>
  <div class="foot">Thresholds are starting points set from 2025 channel performance; they are editable in the app (Settings) and re-tuned after the first 2 weeks of live data.</div>
</section>
"""

CSS = """
@page { size: 1280px 720px; margin: 0; }
* { box-sizing: border-box; }
body { margin:0; font-family: 'Segoe UI', Arial, sans-serif; color:#1b2330; background:#fff; }
.slide { width:1280px; height:720px; padding:14px 22px 8px; page-break-after: always; overflow:hidden; position:relative; background:#f5f7fb; }
.top { display:flex; justify-content:space-between; align-items:flex-start; margin-bottom:8px; }
.kicker { font-size:11px; color:#5b6678; letter-spacing:.4px; text-transform:uppercase; }
h1 { font-size:21px; margin:2px 0 0; line-height:1.2; max-width:1040px; }
.hl { color:#1f4e9a; }
.badge { background:#1f4e9a; color:#fff; padding:6px 14px; border-radius:8px; font-weight:700; font-size:17px; text-align:center; }
.badge small { font-weight:400; font-size:11px; }
.grid3 { display:grid; grid-template-columns: 1.05fr 1.05fr 1.15fr; gap:10px; }
.grid3.low { grid-template-columns: 1fr 1fr 1fr; margin-top:8px; }
.card { background:#fff; border:1px solid #dfe4ec; border-radius:8px; padding:8px 10px; }
.card.wide { width:100%; }
h2 { font-size:12.5px; margin:0 0 4px; color:#1f4e9a; }
.t { border-collapse:collapse; width:100%; font-size:10.5px; margin-bottom:4px; }
.t th { background:#eef1f7; text-align:left; padding:3px 4px; font-size:10px; border-bottom:1px solid #cfd6e2; }
.t td { padding:2.5px 4px; border-bottom:1px solid #edf0f5; vertical-align:top; }
.t.big { font-size:12.6px; } .t.big th { font-size:11.5px; padding:5px; } .t.big td { padding:4.5px 6px; }
.t.big .s { font-size:12px; }
.low .s { font-size:11.6px; line-height:1.34; }
.low h2 { font-size:14px; }
.s1 .t { font-size:11.4px; } .s1 .t th { font-size:10.6px; } .s1 .s { font-size:11.2px; } .s1 .key { font-size:11.6px; } .s1 .mono { font-size:10.4px; }
.rk { font-weight:800; color:#1f4e9a; font-size:15px; text-align:center; }
.c { text-align:center; white-space:nowrap; }
.g { background:#e6f6ec; } .o { background:#fff3dc; } .r { background:#fde8ea; }
.s { font-size:10.5px; color:#3c4656; margin:3px 0; line-height:1.32; }
.key { font-size:10.8px; background:#fff3dc; border-radius:5px; padding:5px 7px; margin:4px 0 0; line-height:1.3; }
.mono { font-family: Consolas, monospace; font-size:9.8px; background:#f1f4f9; padding:5px; border-radius:5px; }
.chart { width:100%; margin-top:2px; }
.foot { position:absolute; bottom:5px; left:22px; font-size:9.5px; color:#6b7587; }
"""

html = f"<html><head><meta charset='utf-8'><style>{CSS}</style></head><body>{slide1}{slide2}{slide3}</body></html>"
OUT_HTML.write_text(html, encoding="utf-8")

browsers = [r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", r"C:\Program Files\Google\Chrome\Application\chrome.exe"]
exe = next((p for p in browsers if Path(p).exists()), None)
if not exe:
    raise SystemExit("Install Edge or Chrome, or open the HTML and print to PDF (1280x720, no margins).")
subprocess.run([exe, "--headless=new", "--disable-gpu", "--no-pdf-header-footer", f"--print-to-pdf={OUT_PDF}", OUT_HTML.as_uri()], check=True, timeout=120)
print("wrote", OUT_PDF)

