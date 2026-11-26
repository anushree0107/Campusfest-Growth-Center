# CampusFest Growth Center (Challenge A: Attendee Growth)

RISA Growth APM assignment. A working command center (dashboard + campaign/task manager + embedded AI analyst) and the strategy behind it.

- **Challenge chosen:** A - Attendee Growth. **City:** Bengaluru (densest tier-1 cluster of colleges, hostels, clubs and creators, so partner-group and referral loops saturate fast).
- **Framing:** the provided CampusFest 2025 dataset is the *baseline* that calibrates the model and powers all analytics. The plan, budgets, bets and tasks are for the *next edition* (CampusFest 2026, assumed 11-12 Apr 2026). The simulated "today" for task deadlines is 19 Jan 2026 (T-12 weeks), editable in the `meta` table.
- **Deliverables:** `docs/CampusFest_Growth_Strategy.pdf` (3 slides, name on slide 1), this app, this README.

## 1. Quick start

```bash
python -m venv .venv && .venv\Scripts\activate        # Windows (use source .venv/bin/activate on macOS/Linux)
pip install -r requirements.txt
set GEMINI_API_KEY=your-key                            # optional here; or paste it in the app sidebar
streamlit run app.py
```

The SQLite database (`data/growth.db`) is built automatically from `data/Attendee Dataset.xlsx` on first run. To reset all edits to the original data use *Settings & Data > Reset* or `python -m growth.etl`.

- Free Gemini key: <https://aistudio.google.com/app/apikey>. Locally put it in `.streamlit/secrets.toml` (see `secrets.toml.example`), in the `GEMINI_API_KEY` environment variable, or paste it into the sidebar.
- Tests: `pip install -r requirements-dev.txt && python -m pytest tests -q` (20 tests, ~3 min because each builds a fresh database).
- Rebuild the slides: `python docs/build_slides.py` (needs Edge or Chrome; set `NAME` at the top of that file).

### Deploy (Streamlit Community Cloud, free)
1. Push this folder to a GitHub repo (`git init`, commit, push; `data/growth.db` is git-ignored and rebuilt on boot).
2. share.streamlit.io > New app > pick the repo, main file `app.py`.
3. App settings > Secrets: `GEMINI_API_KEY = "..."`.
4. Note: the free tier's disk is ephemeral. Edits persist across reruns and sessions but are lost if the app container is rebuilt (see limitations; for durable storage swap `etl.connect()` for Supabase/Postgres).

## 2. What the product does (mapped to the brief)

| Brief requirement | Where | How |
|---|---|---|
| 1. Measure performance | **Measure** tab | Person-level funnel (identified > registered > confirmed > attended), targets vs actuals using the target/budget version effective on the snapshot date, campaign KPI table, growth-over-time chart, spend vs budget, projected attendance. A snapshot-date selector replays any day 15 Jan - 20 Apr 2025 without future leakage. |
| 2. Diagnose performance | **Diagnose** tab | Auto-generated insight feed, efficiency map (cost vs show-up), spend-share vs outcome-share, weakest funnel stage per campaign vs the portfolio median, week-on-week momentum, engagement counts. |
| 3. Support decisions | **Decide** tab | Scale / change / stop flags with the reason and editable thresholds, budget allocation planner (Base / Stretch) with projected attendees, cost per attendee and capacity check, ranked five bets with their rules. |
| 4. Manage execution | **Execute** tab | Campaign plan editor (budget, goal, status, owner), add campaigns, task manager (owner, due date, priority, status, overdue vs plan date), log new marketing activity (spend, clicks) that flows into every metric, change log. Everything is stored in SQLite. |
| 5. Embedded AI analyst | **AI Analyst** tab | Gemini function-calling over 13 tools that query the live database (details below). Proposes tasks and budget changes; nothing executes until the user clicks Confirm. |

### Insight-to-action workflow (demo script)
1. *Diagnose* tab: insight card "Paid Social: 34% of spend but 18% of attendees".  Click **Create task from this insight**, or ask the analyst.
2. AI Analyst: "Why are we missing the registration target? Diagnose it." > it calls `get_overview`, `get_campaign_metrics`, `get_scale_change_stop`, `get_momentum`, answers with evidence / calculation / assumption labels.
3. Follow-up: "If I move Rs 20,000 from Paid Social to Student Referrals, what happens?" > `simulate_budget_change` (assumption-based model, caveat stated).
4. "Create a task for the owner of the weakest campaign." > `propose_task` > proposal card with **Confirm** > task appears in the Execute tab with `source = ai` and the change log records the confirmation.
5. Change data (e.g. add Rs 40,000 spend to a campaign in Execute > Log marketing activity) and re-ask: the answer and the flags change.
6. Ask "What was our revenue per attendee?" > the analyst states the data cannot answer it (no revenue/ticket data) and offers the closest answerable metric (cost per attendee).

## 3. AI analyst design (grounding and honesty)

- **Tools** (`growth/analyst.py`): `get_overview`, `get_campaign_metrics`, `get_scale_change_stop`, `get_trend`, `get_momentum`, `get_projection`, `get_engagement`, `get_plan_and_tasks_summary`, `simulate_budget_change`, `list_tasks`, `run_sql` (single read-only SELECT on a read-only connection, 200-row cap, schema in the prompt, convenience views), `propose_task`, `propose_budget_change`.
- **Grounding:** every number must come from a tool result; the UI shows a *Data used* panel with each tool call, arguments and raw result.
- **Uncertainty:** the system prompt requires EVIDENCE / CALCULATION / ASSUMPTION labels, forbids causal claims (observational data), says reach and impressions are not people, and requires "the data cannot answer this" for out-of-scope questions (revenue, ticket price, demographics, college names, cancellation reasons, real capacity, sponsor impact, dates outside 15 Jan - 20 Apr 2025). Date tools refuse out-of-range dates with an explicit error.
- **Execution safety:** the model can only create *proposals* (`proposals` table). The UI applies them on Confirm. Applying twice raises an error.
- **Changed data:** tools read SQLite on every call (no caching), so edited spend, targets, thresholds, tasks and budgets are reflected in the next answer. Original data is preserved in `data/Attendee Dataset.xlsx` and restored by Reset.
- **Model:** `gemini-2.5-flash`, falling back to `gemini-2.0-flash`; temperature 0.2.
- **What I could and could not verify:** tools, SQL guard, proposal flow and the `ask()` wiring are unit-tested with a fake client (no network). I did not have a Gemini key in the build environment, so live model phrasing was not exercised; run the question list in section 8 once with your key.

## 4. Data and definitions (all enforced in `growth/metrics.py`, verified in tests)

Source: `Attendee Dataset.xlsx` (CampusFest 2025, fictional, synthetic). Key rules from its Read me and how they are applied:

| Rule | Implementation |
|---|---|
| Duplicate import rows share a stable ID | De-duplicated by `activity_id` (2 removed), `history_id` (4), `event_id` (20); logged in `etl_log`. |
| Names are not identities | All joins use IDs. |
| Latest eligible full-state version | Registration status = latest `reg_history` row with `effective_at <=` snapshot; targets = latest version effective at the snapshot. |
| Snapshot = end of day IST, no leakage | Every query filters `<= YYYY-MM-DD 23:59:59`. Test: attended = 0 on 11 Apr; spend on 11 Apr (Rs 2,76,500) is higher than final (Rs 2,74,000) because later credits are not visible earlier. |
| External attendance only | `person_type = 'external'`; internal (40), staff (10), test (10) excluded. |
| Actual attendance | `result = admitted AND verification = verified`, **unique person** (3,250 admitted rows = 2,050 people). Denied (50) and pending/unverified (30) excluded. |
| Reach is not people | Impressions, reach, sessions, registration starts shown only as anonymous activity; funnel stages for diagnosis use identified people. |
| Spend | Whole INR, net of ledger credits, `activity_date <=` snapshot. |

**Attendee definitions.** 2,050 unique external attendees = **1,925** attributed to a campaign through their registration + **125 walk-ins** (check-in with no registration/campaign, `first_touch_source = walk_in`). Campaign tables show 1,925; headline unique visitors uses 2,050 (matches the `unique_external_visitors_goal`). Attribution is the check-in's registration campaign; the alternative (first-touch campaign of the person) gives the same per-campaign totals in this data.

**2025 headline (sourced):** identified 3,625 (goal 3,600), registered 2,800 (goal 3,200), confirmed 2,500, unique attendees 2,050 (goal 2,200), spend Rs 2,74,000 (budget Rs 3,00,000), cost per attendee Rs 134.

| Campaign | Identified | Registered | Attended | Spend (Rs) | Cost/attendee | Show-up |
|---|---|---|---|---|---|---|
| Campus Ambassadors | 600 | 520 | 390 | 42,000 | 108 | 75% |
| Paid Social | 960 | 680 | 340 | 93,000 | 274 | 50% |
| Creator Partners | 490 | 350 | 175 | 59,000 | 337 | 50% |
| College Partnerships | 500 | 450 | 385 | 31,000 | 81 | 86% |
| Student Referrals | 410 | 350 | 275 | 19,000 | 69 | 79% |
| Organic Search | 290 | 250 | 195 | 14,000 | 72 | 78% |
| Community Groups | 250 | 200 | 165 | 16,000 | 97 | 82% |

## 5. Strategy model and calculations (`growth/model.py`, same code feeds the PDF and the app)

For campaign *c* with budget *B*:

```
identified_c(B) = identified_2025_c * (B / spend_2025_c) ^ e_c            # diminishing returns
registered_c    = identified_c * (registered_2025_c / identified_2025_c)   # observed 2025 rate
attended_c      = registered_c * showup_c'                                 # showup' = 2025 show-up + reminder lift
total           = 2025_total + (sum_c attended_c + walk_ins - 2025_total) * (1 - overlap)   # capped at venue cap
```

| Parameter | Value | Status |
|---|---|---|
| 2025 identified / registered / attended / spend per campaign | from data | **Sourced** |
| Elasticity e (Ambassadors .70, Paid .80, Creators .70, Colleges .65, Referrals .60, Organic .50, Community .60) | judgement | **Assumption** |
| Stretch elasticity | e + 0.10 (cap .95) | Assumption |
| Show-up lift from no-show recovery (channels with 2025 show-up < 65% / others) | +8 / +3 pts base; +15 / +5 stretch; cap 80% / 92% | Assumption |
| Overlap haircut between ideas | 10% | Assumption |
| Walk-ins | 125 base (2025 actual), 150 stretch | Assumption |
| Venue capacity | 3,500 unique external attendees | Assumption |
| Budget | Base Rs 3,00,000 (= 2025 revised budget, incl. Rs 10k reserve). Stretch Rs 3,60,000 (+Rs 60k sponsor-funded) | Assumption |
| Audience sizes on slide 1 (400 clubs, 250k serviceable students, etc.) | order of magnitude | **Assumption** - validate with AISHE/AICTE and club lists |

Allocations (Rs, base / stretch): Ambassadors 60k / 72k, Paid Social 45k / 48k, Creators 25k / 25k, Colleges 52k / 68k, Referrals 42k / 58k, Organic 24k / 27k, Community 27k / 32k, Recovery 15k / 20k, Reserve 10k / 10k.

**Results:** Base 2,450 attendees (+19% vs 2,050; Rs 122 each; 70% of the capacity assumption). Stretch 3,007 (+47%; Rs 120 each; 86%). Bridge to base: 2,050 + 553 (scale five efficient channels) - 229 (cut Paid and Creators) + 120 (no-show recovery) - 44 (10% overlap haircut) = 2,450. Funnel (base): ~6.4 lakh anonymous reach > 3,783 identified > 3,034 registered > 2,450 attendees.

**Prioritisation framework:** score = incremental attendees (model, base) x confidence / effort (person-weeks). Ranking: (1) no-show recovery 48, (2) referral engine 39, (3) ambassador scale-up 28, (4) college blitz 27, (5) organic + community 24. Rejected / avoided: scaling Paid Social at Rs 274 per attendee, flat-fee creators at Rs 337, buying more reach. Their effect (-229 attendees) is already netted in the target. Confidence and effort values are judgement calls.

**Leading indicators and thresholds** are on slide 3 and in the `ideas` and `settings` tables. In 2025 registrations were 54% / 78% / 90% of final at T-4w / T-2w / T-1w, which gives the pacing alarm (< 85% of that curve at T-4w triggers reallocation).

**Scale / change / stop rules** (editable in the app). Post-event: SCALE if cost per attendee <= Rs 120 and show-up >= 70%; CHANGE if cost > Rs 150 or show-up < 65%; STOP if cost > Rs 300. Pre-event (spend is front-loaded, so absolute rupee thresholds mislead): compare cost per confirmed registration with the portfolio median: SCALE <= 0.9x and registration rate >= 80%; CHANGE > 1.25x or cancellations > 10%; STOP > 2x. Minimum sample 100 identified people, otherwise INSUFFICIENT DATA.

**Pre-event projection:** `confirmed now + (registrations/day over last 14 days x days to event) x confirmed ratio` then x an **assumed** 75% show-up. It is a naive run-rate: on 20 Mar 2025 it gives ~1,619 attendees vs the 2,050 actual (registrations accelerated late), so treat it as a conservative floor, not a forecast.

## 6. Research and sources

- All performance facts: the supplied CampusFest 2025 datasets (fictional). No external statistics were used for numbers in the model.
- Audience sizes and the Bengaluru justification are my own order-of-magnitude assumptions and are labelled as such.
- Tools: Python 3.11, Streamlit (free), SQLite, pandas, Plotly, Google Gemini API via `google-genai` (free tier key), pytest, matplotlib + Microsoft Edge (headless) to render the PDF, Cursor for development. No paid plans required.
- Reused: open-source libraries only. Written by me for this assignment: ETL, metrics, model, insight rules, management layer, analyst tools and prompts, tests, slides.

## 7. Verification performed

- `tests/test_metrics.py`: independent pandas recomputation of headline numbers from the raw Excel (3,625 / 2,800 / 2,050 / Rs 2,74,000), dedupe counts, no future leakage, target version switch on 15 Mar, latest-status logic, walk-in attribution, flag rules, data edits flowing into analytics, threshold edits changing flags, task CRUD, proposals needing confirmation, model baseline reproduction (same allocation as 2025 returns exactly 2,050).
- `tests/test_analyst.py`: tool outputs match metrics, tools reflect edited data, out-of-range dates refused, SQL is read-only (rejects DELETE and multi-statement), proposals do not execute until accepted and cannot be accepted twice, `ask()` passes history, tools and the honesty prompt.
- App smoke test with Streamlit `AppTest` at several snapshot dates (no exceptions).

## 8. Questions to try with the analyst (answers checkable from the data)

| Question | Expected grounded answer |
|---|---|
| What is Paid Social's cost per attendee? | Rs 273.5 = 93,000 / 340 |
| How many unique people attended? | 2,050 (1,925 campaign-attributed + 125 walk-ins) |
| Registrations and identified people on 20 Mar 2025? | 1,778 registered, 2,694 identified, spend Rs 2,12,000 |
| Which target version applied on 14 Mar vs 15 Mar? | Launch plan (2,800 registrations) vs revised plan (3,200) |
| Spend as at 11 Apr vs final? | Rs 2,76,500 vs Rs 2,74,000 (later credits) |
| What is the show-up rate by campaign, and where is the bottleneck? | 50% for Paid and Creators vs 75-86% others; weakest stage = confirmed > attended |
| What was revenue per attendee? / Which colleges attended? | The data cannot answer (no revenue or college fields) |
| What happened on 1 Jan 2026? | Out of range, cannot answer |
| After you add Rs 40,000 spend to Referrals and ask again | Cost per attendee for Referrals rises from Rs 69 to Rs 215; flag leaves SCALE |

## 9. Known limitations

- Free Streamlit hosting has an ephemeral disk; use Supabase/Postgres for durable multi-user persistence. SQLite writes are single-writer.
- The 2026 plan numbers are model outputs under assumptions (elasticities, lifts, overlap, capacity), not measured effects; the dataset has no experiment data, so campaign associations are observational.
- No revenue, demographic, college, ticket price or cancellation-reason data, so such questions are unanswerable by design.
- Pre-event projection is naive run-rate; it under-predicted the 2025 outcome.
- Attribution is the registration campaign; multi-touch paths are not modelled. Walk-ins are unattributed.
- The AI analyst depends on Gemini free-tier quotas and could not be exercised live in my build environment (see section 3). Model fallbacks and error messages are in place.
- Thresholds are starting points derived from 2025 channel performance and should be recalibrated after two weeks of live data.
- `tasks.due_date` uses a simulated plan date (19 Jan 2026) so overdue logic is meaningful for the next-edition plan; change `meta.plan_date` to your real date.
