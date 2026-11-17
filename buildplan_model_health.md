# Model health overview and the data-quality watchdog

Status: built 2026-11-16 (build log below). First agent from Part D of `buildplan_chat_centre.md`.

## What changed for the analyst

- `/model-health` opens on **every forecast at once**: one card per estate of each source, plus the staging group (8 today). Each card has a status (Action / Watch / Healthy / Not forecast), the served model and how it was chosen, a sparkline (12 actual months, the forecast and its 80% range), 3-month MASE / sMAPE / bias, the track record as 12 dots, a strip of the 11 checks and up to three issues in plain words. A click opens the detail view.
- Under the cards are a **health matrix** (every forecast × every check, each cell with a glyph, the reading and a tooltip), **accuracy against the benchmark** (3-month MASE bars with the 0.80 and 1.00 lines) and the **data-quality watchdog** table (status, forecast, check, finding, what it affects; Action + watch / Action / All; "Run checks").
- `/model-health?db=<source>&estate=<id>` is the detail view the page had before, rearranged on a 12-column grid that uses the full width. It gains a switcher (every forecast as a chip), a breadcrumb back to the overview, the watchdog's findings for that estate (in the Data quality card) and a **Live monitoring** card (every `forecast/monitor.py` row, not only alerts). The bias tile is now the 3-month figure, like the overview's card; the 1-month figure is in its subtitle.
- The forecast page's "Model health" link opens the detail of the forecast in view; Compare and the Forecast Lab link to the overview.

## Pieces

| File | What it does |
|---|---|
| `forecast/watchdog.py` | The data-quality checks on the served run's series files. `python forecast/watchdog.py [--out f] [--today d]`. Never changes a file or a forecast. |
| `forecast_fleet.py` | One card per forecast from the cached page payload, the run's `health.json` rows and the watchdog. Never refits, never builds the slow per-estate scoreboard. Watchdog report cached on the series files' mtimes and the date. |
| `forecast_router.py` | `GET /forecast/fleet`, `GET /forecast/watchdog?source=&estate=`, `POST /forecast/watchdog/run`; `/forecast/model-health` now carries `watchdog.findings` (and `monitoring` on the fallback path). |
| `forecast_sources.py` | `_accuracy` adds `pooled_bias_pct` (3-month bias, + = over-forecast); `health()` adds `bias_pct_h1_3`. |
| `forecast/refresh.py` | The health step runs the watchdog and keeps `data_quality.json` in the run (advisory: a failure is recorded in `run.json`, the run still publishes). |
| `dashboard_server.py` | Warms the overview in a background thread at start-up (about 30 s cold, then ~0.1 s). |
| `forecast_static/model_health.html` | Both views. Charts are inline SVG with one shared tooltip (plain text); no new library. |

## The watchdog's checks

Levels: alert = Action, warn = Watch, ok, info. Each finding says what it found (`title`, `detail`) and what it does to the forecast (`affects`). Thresholds sit in `watchdog.T`.

| Check | Dimension | Rule |
|---|---|---|
| freshness | timeliness | days since the last harvest: ok ≤ 40, watch ≤ 90 |
| extract_gate | consistency | the extract's own reconciliation checks (meta.json gate) |
| history_depth | sufficiency | complete months: ok ≥ 24, watch ≥ 12 |
| harvest_gaps | completeness | ≥ 7 calendar days with no harvest (action ≥ 14) |
| short_months | completeness | a complete month with < 70% of the usual harvest days |
| daily_spikes | plausibility | a day ≥ 2× the days around it (action ≥ 3×) adding ≥ 1% to its month; more than one such day a month = "posted in batches" (one info finding: K3) |
| block_coverage | completeness | blocks reporting < 75% of the trailing 6-month median (action < 50%) |
| duplicates | uniqueness | a day, or day + block, recorded twice |
| ticket_ratio | plausibility | bunches per ticket against the same month a year earlier, off by > 40% (it moves with the crop inside a year) |
| target_fill | completeness | share of tickets with a bunch count: ok ≥ 85% (about 10% of tickets are loose fruit only on every estate) |
| weight_per_trip | validity | staging tonnes per weighbridge trip outside 2-15 t (the FDN per-row repeat) |
| cross_source | consistency | monthly totals of an estate held in both databases: ok ≤ 1%, watch ≤ 5%; names the days behind the worst month |

Months the model already imputes (K3's features file: `is_underrecorded` / `exclude_from_model`) turn a gap or short month into information: the data is short, the forecast is not misled by it.

## The overview's checks (statuses)

Same thresholds as the detail view's tiles, so a number reads the same way on both. A forecast's status is its worst check.

Accuracy (3-month MASE 0.80 / 1.00) · Error (3-month sMAPE 10 / 20%) · Bias (2 / 5%) · Evidence (≥ 12 origins; fewer caps at Watch, since only more data fixes it) · Track record (≥ 75% / 50% of the last 12 one-month-ahead forecasts within 10%) · Live errors and Drift (monitor rows) · Serving (fallback, stale or a failed pick = Action; a manual pick = Watch) · Freshness, Data quality, Live vs staging (watchdog).

## Build log

**Built 2026-11-16.**
- What the watchdog found on the served run (`20261113-114148-live-staging`): 6 action, 14 watch, 50 passed. Action: live data stale (K3 171 days, EC 117, EB 289, EA 484) and too little history on live EB (8 months) and EA (1). Watch: under two years of history on every staging estate; live EC's 2025-04-02 (164,822 bunches, 2.2× the days around it), found both as a spike and as the live-vs-staging difference (staging has 88,303 that day); EC's bunches per ticket fell from about 53 to 14 between January 2025 and January 2026 on both databases (a recording change worth asking the estate about); K3's 2023 gaps.
- Tuned on the real data before shipping: bunches per ticket first compared with the median, which flagged half the months (it moves with the crop), so it compares a year earlier and reports once per estate; ticket fill first warned below 95% and fired everywhere; K3's daily file posts in batches (about 500 bunches on most days, 300-470 tickets on a few), so 5.6 "spikes" a month became one "posted in batches" finding. K3's short months match the months its features file imputes exactly (2023-01/06/07/08, 2024-01, 2025-12, 2026-02/04/05), which is now how those findings are labelled.
- Statuses today: 4 action (live K3, EC, EB on stale data; staging EC on its track record, 4 of 12 within 10%), 3 watch (staging group, EA, EB: manual picks and short history), 1 not forecast (live EA). 5 of 6 scored estates beat the benchmark; median 3-month MASE 0.36.
- Speed: the detail view's scoreboard rebuilt the seasonal-profile models' backtests on every request (49 fits; 35 s for the staging group, 11 s for one estate). `forecast_sources._scored` now caches per process and is cleared with the registry's cache, so a detail opens in 0.3-0.8 s once built. The start-up warm-up builds the overview and then every detail. Fixed a race found on the way: the start-up warm-up and the first request loaded `watchdog.py` at the same time and one saw it half-loaded (a 500); it now loads once under a lock.
- Tests: `test_forecast_watchdog.py` (15 checks on synthetic series, one planted problem each, plus 3 on the published run); `command_static/test/model_health_fleet.mjs` (33 checks: API, cards, matrix, bars, watchdog table, filters, hover, drill-down, switcher, breadcrumb, phone width, no console errors); `forecast_page.mjs` now opens the detail with `?estate=`.

## Not done

- No LLM in the watchdog: the findings are deterministic templates. A Nova summary of the whole report ("three things to fix this week") is a possible next step, kept outside the checks.
- The watchdog reads the extracted files, not the source databases: it cannot see rows the extract dropped by design (STATE = 'D' is reported by the extract itself).
- Feature drift (PSI / KS) is still not monitored; the target series' seasonal shape, columns and volume are (Live monitoring).
- No alerting outside the page (email / Slack): that is the weekly-digest agent in Part D.
