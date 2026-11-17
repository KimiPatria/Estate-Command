# Build plan: dual-source forecast pipeline, MLOps fixes and the accuracy challenger

Status: built 2026-11-12. Phases 0-7 are built and tested; 5.3 (SageMaker run) waits for an `aws login`. The log below says what differs from the plan.

## Build log

**Serve a candidate on demand from Model health, built 2026-11-13.**
- On the candidate scoreboard, a candidate row (click it or press Serve) serves that model on the scope in view: one estate, the staging group (all three estates), or K3 on the live group view. After the server accepts it, the browser opens `/forecast?db=<source>&estate=<id>`. "Back to rule pick" undoes it, and so does picking the rule's own model.
- A pick is a manual override (`forecast/overrides.py`). It is stored in `forecast/runs/serving_overrides.json`, outside any run folder, so a refresh keeps it. Every set and clear is appended to `serving_overrides_log.jsonl`. A pick beats both the rule and K3's manual promotion. The selection files and `RULE_ID` are not touched.
- The server refuses a pick (409) unless the model builds a current, non-stale forecast for every estate in the scope. If a pick later stops building, the page serves the rule's pick and shows a "Manual pick not served" banner, and the refresh health records a `manual_pick` alert.
- Where it shows: the forecast page's model pill says "· manual pick" (hover for the date and the rule's pick). Model health tags the served row "manual pick" and the rule's model "rule pick". A staging group with different picks per estate is labelled "Mixed: ...". refresh.py records the picks in `run.json` (`overrides`), in MLflow params (`override_<source>_<estate>`) and in health (`chosen_by`).
- Test: `test_forecast_ci_pipeline.py::test_a_candidate_can_be_served_on_demand_and_returned_to_the_rule`, run on synthetic data through the real endpoint.

**Seasonally adjusted Chronos arm, built 2026-11-13 (challenger; the switch is the user's).**
- Why: EA, EB and EC swing 1.8-3.3x within a year (peak in May) and have 17-22 months of history, so zero-shot Chronos carries the recent level through every seasonal turn. The served staging arm's error grows from 12% at step 1 to 49% at step 6. K3 has 3+ years and a weaker cycle. The database is not the cause: live and staging behave the same. Experiment: `forecast/experiments/seasonal_chronos_2026-11-13/`.
- `chronos2_weekly_seasadj`: the estate weekly input divided by `seasonal_profile`'s index (fitted on complete months ending on or before the cutoff, pooled within source and region), Chronos-2 on what is left, the index multiplied back. Same checkpoint, settings and cut as estate weekly. Until the estate has seen all 12 months of the year it forecasts the plain series and says so in its notes (live EB: 8 of 12).
- Rule `chronos-seasadj-h6-2026-11-13` is written and NOT in force: the arm is a served candidate and rows are steps 1-6. Dry run on run `20261113-114148-live-staging`: staging would serve the adjusted arm (mean ratio 0.32 vs 0.63 for estate weekly); live keeps `chronos2_monthly` (0.418 vs 0.437). Under the rule in force the arm is a reported challenger: staging 0.35 vs served 0.51 at steps 1-3, live 0.55 vs 0.39. To switch: `RULE_ID` in `select_source.py`, then a refresh.
- The frozen `staging-default-2026-11-10` rule leaves out artifact arms added after it (`select_staging.ADDED_LATER`).
- Monitor `seasonal_shape` alerts when the pool's peak month moves 2+ months, or the estate's log peak-to-trough swing changes by more than 30%, between runs. All estates were ok on the first run.
- Shadow log: the arm is logged with every refresh (72 rows on the first).
- Tests: `test_forecast_seasadj.py` (16, in CI), plus a pipeline check in `test_forecast_ci_pipeline.py` that the arm is built on synthetic data with the adjustment on and is not served. The full forecast suite passes (131 checks).
- The pipeline arm reproduces the experiment's backtest (median difference 0.2-0.9% per estate).
- Caveats: 20 staging estate-origins (5 each for EA and EB); step 1 is slightly worse (15% vs 12%); the gain is mostly at seasonal turns, and live has no scored origin past June 2026.

**Phase 0, built 2026-11-12.**
- The shadow log records all six estates, and every row carries `source`. The first issue of EB and the three staging estates is 2026-10 (EB 2026-01).
- `shadow.py backup` is written but has never run: the AWS session had expired, and the dashboard env lacks `botocore[crt]`, now declared in `requirements.txt`. Run `aws login` and `pip install "botocore[crt]"`, then `shadow.py backup`.
- The Chronos checkpoint is pinned to `29ec3766d36d6f73f0696f85560a422f50e8498c`. Rebuilding the 20 monthly forwards after pinning gave identical numbers.
- `requirements.txt` gained the forecast stack. A fresh venv also exposed an older bug: SQLAlchemy 2.1 makes psycopg 3 the default driver, so it is now capped below 2.1. A fresh venv from `requirements.txt` now serves both sources.
- No shadow row has resolved on any estate yet. K3's export has not been refreshed since 2026-05, so its Gate B cannot move until it is.

**Phase 1, built 2026-11-12.**
- The rule as first written (`chronos-ctx12-2026-11-12`) chose the block sum on live. That choice was driven by live EB's 3 rows from 2 origins; the block sum is the worst arm on EC.
- It was superseded the same day, before anything was served, by `chronos-ctx12-evidence-2026-11-12`. That rule keeps low-evidence estates out of the score, while still serving and flagging them. The first rule stays runnable with `--rule`.
- Served now:
  - staging: `chronos2_estate_weekly`
  - live EC and EB: `chronos2_monthly`
  - K3: `chronos2_monthly_cov`, under manual promotion `manual-promotion-2026-11-12`
  - live EA: unavailable (1 complete month)
- Seasonal-profile pooling is by source and region. K3 is its own region: it peaks Sep-Nov, while EA, EB and EC peak Apr-Jun.
- The live group view stays K3, as the page's existing "Group rollup" rule says.
- The live roster lists BA, K3, EC, EB and EA, and the count is "N of 5".
- Model health takes `?estate=` from the forecast page's link, and shows the D10 flags as one-line warnings.
- On live K3 the 12-month view is gone: Chronos serves steps 1-6. It comes back only on the ensemble fallback.
- Not changed: one static caption on model health still describes the served model as a recursive ensemble.
- Tests: `test_forecast_chronos_serving.py` (12), plus the updated page test. In total 52 pytest checks and both browser suites pass.

**Phase 2, built 2026-11-12.**
- Backtests of arms with a real interval now carry `lower` and `upper`. The two monthly arms (month grain) and estate weekly (week grain) were rebuilt, and their point forecasts are unchanged.
- The drawn band and the headline accuracy are measured on origins with the rule's `min_context_months` (12), falling back to every origin below 3. Old-rule files (no threshold) keep every origin.
  - Staging EC: band 14.5 / 22.9 / 28.5% (was 17.0 / 30.8 / 45.8), headline 21.4% (was 30.6%).
  - Staging group: band 10.3 / 16.5 / 24.6%, headline 15.9% (was 19.3%).
- Outer 80% range: the model's own 10th-90th percentile where it held 70-90% of backtest actuals on ≥ 10 rows, else the 80th percentile of |actual - forecast| / forecast.
  - Live EC uses the model's percentile (held 81% of 21).
  - K3's percentile held 93%, too wide for 80%, so K3 is empirical.
  - Staging's served arm has no month-grain interval, so it is empirical.
  - Group sums never add quantiles.
- Deviation: the outer-band choice is computed per payload, not written into the selection file. It appears in `interval.outer`, on model health and in the chart tooltip.
- Page: the outer band is drawn underneath in the same fill, a "80% range" legend item is added, and the band tile reads "held N of M".
- Model health: the interval card shows the 80% range columns for Chronos payloads.
- Tests: `test_forecast_bands.py` (9), plus page checks. In total 61 pytest checks and both browser suites pass.

**Phase 3, built 2026-11-12.**
- New files:
  - `forecast/estates.yaml` and `forecast/estate_config.py`: one estate list for the extract, the Lab registry, `block_weekly.SOURCES` (keys `live_<CODE>` and `stg_<CODE>`) and the page.
  - `forecast/contract.py`: core columns, a shared item vocabulary, `validate`, `columns_block`, and `monthly` and `gate` moved over unchanged.
  - `forecast/adapters/`: `staging_sqlserver`, `live_postgres`, `live_k3_file` and `file_drop`.
  - `forecast/build_series.py --source live|staging`: atomic writes of `oph.csv`, `monthly.csv`, `daily.csv`, `meta.json` and `columns.json`, plus extras.
- `build_staging_series.py` is now a wrapper around `build_series.py`.
- The live pipeline writes its own copy to `forecast/live/<CODE>/` (gitignored). The map keeps `gis/data/estates/`, unchanged. The Lab's live estates now read `forecast/live/`.
- Checks passed:
  - The staging core columns match the old files byte for byte, and so does `monthly.csv`.
  - Live EC, EB and EA match the map snapshot block-day for block-day across all 16 bunch and loose-fruit columns.
  - The daily block series of all four live estates are identical, including K3's.
  - A file-drop round trip gives identical files on both sources.
  - P1 builds and gates from a config entry alone.
  - Every served forecast is unchanged to the bunch.
- Found while building:
  1. The live databases hold more than the map snapshot: harvest point, harvest card, foreman and checker codes (100% filled), GPS (20-26%), and rain flags (under 1%). The comparison is measured from the databases, not the snapshot.
  2. Staging `ZBRONDOLAN` is a varchar column (P1 holds '1000.00'), so it is now read with `TRY_CAST`. Every value parses (0 non-numeric rows).
  3. An export missing a grade column crashed the K3 adapter. A missing column now counts as zero in sums and shows "not available" in the stats.
- Live estates now carry a data-through date and harvest-day counts on the page, from their own `meta.json` and `monthly.csv`.
- Refresh routine: `python forecast/build_series.py --source staging|live`, then `build_artifacts.py --source <s>` in the Chronos env, then `select_source.py --source <s>`.
- Tests: `test_forecast_contract.py` (24). In total 85 pytest checks and both browser suites pass.

**Phase 4, built 2026-11-12.**
- The view is at `/forecast/compare`, reached from a "Forecast / Compare sources" switch in the forecast page's title bar.
  - Deviation: it is a sibling page, not a tab inside `forecast.html`. That page is about 2,000 lines, and the switch reads as a tab.
  - Data comes from `/forecast/compare-data`, built by `forecast_compare.py` from the extracts' `meta.json` and `columns.json`, the selection files and the Lab backtests. It is cached on file times.
- Six cards:
  - Data available: 30 data items, each with the source's own column header and its status. An estate toggle shows one estate's view.
  - History: timelines, with partial months hatched and "cannot forecast" reasons.
  - Agreement: a chart plus the months that differ by more than 0.5%.
  - Accuracy: the served model on its own window, the served model on the shared window, and the same model on the shared window.
  - Forecast: both sources' forecasts with their 80% ranges.
  - Running cost: connections, estates, data dates, extract time and gate results. `build_series` now records `extract_seconds`.
- Colours: live is slot 1 (blue) and staging slot 2 (orange); the pair was validated on both surfaces. Status dots use the accent, gold and hollow, always with a text label.
- What the page shows the client (2026-11-12 data):
  - Live databases also hold harvest cards and GPS. Staging holds harvester ids and patchy weighbridge tonnes. Both have harvest point, foreman, checker and loose fruit.
  - EC agrees in 16 of 18 months (Feb 2025 +1.1%, Apr 2025 −3.9%). EB differs in Sep 2025 (−1.2%) and Oct 2025 (−0.6%).
  - The same model on the same months scores nearly the same on both sources: EC Chronos monthly 26.8% vs 26.6%, the combination 25.6% vs 24.7%. The gap between the sources comes from history length and the arm served, not from the data values.
  - The live EC snapshot ends in July 2026. Its forecast for Jul-Sep is flat near the June peak (~1.7M), while staging already shows those months falling to about 0.75M.
  - Live needs 3 databases plus 1 export file per refresh (about 13 s). Staging needs 1 database (about 43 s for three estates).
- Tests: `test_forecast_compare.py` (5) and `command_static/test/forecast_compare.mjs` (17 checks). In total 93 pytest checks and three browser suites pass.

**Phase 5, built 2026-11-12.**
- `forecast/refresh.py --source live|staging|all [--remote]`, plus `--list` and `--rollback <run_id>`.
  - A run is a complete snapshot `forecast/runs/<run_id>/`: `series/<source>/<CODE>/` for every source, `lab/`, and `run.json`.
  - Steps run as subprocesses pointed at `<run_id>.partial` through `FORECAST_RUN_DIR`: snapshot, extract, inference (Chronos env), select, health, then publish (rename, then an atomic replace of `forecast/runs/current.json`).
  - After publishing, best effort: shadow record and backup, then MLflow.
  - A failed step keeps serving the published run and keeps `<run_id>.failed`.
  - A lock allows one refresh at a time. A killed refresh's folder becomes `<run_id>.killed` on the next run.
  - Six published runs are kept, and the served run is never pruned.
- Readers resolve paths at call time: `estate_config.series_dir` / `lab_dir`, `registry.ARTIFACT_DIR` (module `__getattr__`) and `block_weekly.source_path`. The page caches key on path plus mtime.
- Runs so far: `20261112-163019-staging` and `20261112-164922-live`, both published and both health-checked.
  - The staging rebuild reproduced all 30 staging artifacts and the three extracts byte for byte.
  - A live refresh killed mid-inference left the served run untouched. The next refresh took over the lock and kept `…-live.killed`.
- MLflow: experiment `ffb-forecast-refresh`, registered models `forecast-staging` v1 and `forecast-live` v1.
  - Logged from the Chronos env: `mlflow.db` is on MLflow 3.16's schema, set by that env's R&D runs, and the dashboard env's 3.14 cannot open it.
  - No database migration was done.
- Shadow backups to S3 now run on every refresh (`forecast-runs/shadow/…`).
  - Note: they authenticate with the static AWS keys in `.env` (the Bedrock keys), which the refresh loads. If those keys should not write to S3, give the backup its own profile.
- Not done:
  - 5.3, the `--remote` SageMaker run. The Chronos env's `aws login` session had expired, and the `.env` keys were not used for paid jobs. After `aws login`: `python forecast/refresh.py --source staging --remote`.
  - Weather re-fetch, deliberately. K3's weather file is a legacy file that does not describe K3 (see `fetch_nasa_weather.py`), and K3's served model was backtested on it. Replacing it is a modelling decision.
- Tests: `test_forecast_refresh.py` (8). In total 101 pytest checks and three browser suites pass on the published runs.

**Phase 6, built 2026-11-12.**
- The live refresh has an `ensemble` step: `refresh.py --precompute-ensemble` fits the ensemble for K3 and EC at horizons 3, 6 and 12 and writes `lab/ensemble/<estate>_<h>.json` into the run.
- `forecast_router.get_forecast` reads those files, keyed on path plus mtime. `?refresh=1` re-reads; it never refits.
  - Before the first published run (legacy layout) it still fits in-process, as before.
  - With a run published, a missing payload is refused with the refresh command, never fitted.
- Run `20261112-170126-live` (registered `forecast-live` v2) carries the six payloads. The stored K3 payload equals a fresh fit, apart from `generated_at` and the per-request roster.
- Tests: `test_forecast_precompute.py` (5). `_compute` is made to raise, then every route that used to fit is called: the live forecast for K3 and EC, live model health, the comparison, and the Lab's K3 and EC estates. In total 106 pytest checks and three browser suites pass.

**Phase 7, built 2026-11-12.**
- CI is `.github/workflows/forecast.yml`. It fails if any data folder is tracked, installs `requirements-forecast-ci.txt` (forecast stack only, no Chronos), lints for errors only, then runs the CI-safe test files.
  - It is not yet run on GitHub. A clean checkout (tracked files plus new files, no data) with a fresh venv from that file passed every step locally: 72 passed, 12 skipped (the tests that need company data).
- `forecast/fixtures/make_synthetic.py` builds two synthetic estates in both shapes: a live per-record export, and a staging file drop carrying the staging adapter's own registry. It can inject an emptied column and a volume drop.
- Hooks for CI: `FORECAST_ESTATES_CONFIG`, `FORECAST_RUNS_DIR`, `FORECAST_LEGACY_LAB_DIR`, `FORECAST_STUB_INFERENCE` (a flat level forecast in place of Chronos, inside `_chronos_job`), and `refresh.py --stub-inference --skip-after`.
- `test_forecast_ci_pipeline.py` runs the real refresh on synthetic estates, both sources, end to end. It then shows the column-drift and volume monitors firing on injected problems, and checks the realised-error, Chronos-vs-naive, freshness and volume rules on hand-made inputs.
- Monitoring is `forecast/monitor.py`, run inside the refresh health step:
  - realised error: outside the drawn band 2 months running, or the rolling 3-month error above 1.5x the backtest
  - Chronos against naive (D10)
  - freshness: older than 40 days
  - volume: the newest full month beyond ±60% against the previous month, or ±40% against the same month a year earlier
  - column-status drift against the previous run
- Results go to `<run>/health.json` and show on model health (one-line alerts) and on the compare page (Running cost, "Monitoring alerts").
- Deviations:
  - The "new undeclared column" check is not built. The database adapters do not list undeclared columns.
  - Realised error applies the band as measured now to logged forecasts, because the shadow log keeps the model's own interval, not the drawn band.
- First real run with monitoring: `20261112-172854-live-staging` (`forecast-live` v3, `forecast-staging` v2).
  - Three alerts, all freshness on live: K3 (data through 2026-05-29), EC (2026-07-22), EB (2026-01-31).
  - Staging passed every check.
  - Realised-error checks wait for resolved months: no shadow row has resolved yet.
- Tests: 114 pytest checks and three browser suites pass.

**State at the end of the build (2026-11-12).** Phases 0-7 are built. Open items:
1. The SageMaker `--remote` run (5.3) needs `aws login` in the Chronos env, then `python forecast/refresh.py --source staging --remote`.
2. The live snapshots are old (see the freshness alerts). Restoring fresher live databases is the main lever for the live side of the comparison.
3. K3's weather file is a legacy file that does not describe K3. Whether to replace it is a modelling decision (it changes the inputs of K3's served model).
4. The shadow log has no resolved rows yet, so Gate B and the realised-error monitors are still waiting for data.
5. The CI workflow has not run on GitHub yet; nothing has been committed or pushed.

Source: `review_forecast_mlops.md` (same date). Experiment code that every accuracy number below comes from: `forecast/experiments/staging_accuracy_2026-11-12/`.

## Goal

1. **Two pipelines, one page.** Every step runs on both histories, so the client can see side by side what each ingestion path gives and costs. That covers extract, contract, models, selection rule, band, shadow log and monitoring. The two histories:
   - **Live:** the per-estate operational EPMS Postgres databases, plus K3's export.
   - **Staging:** the central EPMSStagingDB on SQL Server.

   The ingestion path for production is not decided, and this comparison is meant to inform that choice.
2. **Show what data each source makes available.** Column headers, with how well each is filled, are the main exhibit. Differences in the numbers are expected and are part of the experiment.
3. **Serve Chronos-2 zero-shot as the forecast model on both sources, every estate, K3 included** (user decision 2026-11-12). The history is short here, and the user hears it is short in the production data too. A pretrained model that needs no training on our data suits that.
   - A dated rule picks the Chronos-2 arm per source: estate weekly, monthly, monthly plus weather, or block sum.
   - The seasonal profile and the Chronos + profile combination stay in the Forecast Lab as shadow-logged challengers. On staging they scored lower error than Chronos alone (11.3% and 13.7-14.9%, against 18.9%, steps 1-3 on origins with ≥12 months), so the evidence keeps accumulating, but they are not served.
   - The band's meaning is stated on the page.
4. **Make production a configuration change** whichever path is picked: one adapter, one secret, one scheduled command.
5. **Close the hygiene gaps** that would bite first in production. They are unpinned environments, artifacts on one machine, training inside a request, accuracy figures that were never measured, and no CI.

Out of scope: the network side of the production connection. It is designed in memory under "Staging production ingestion", applies to either path, and becomes Phase 8 once the client says how data can reach us.

## The two sources today

| | Live | Staging |
|---|---|---|
| Where it comes from | One restored Postgres DB per estate (`EPMS EC/EA/EB`, table `t_oph`), snapshotted by `gis/build_estate_data.py`. K3 comes from `forecast/epms_production_daily.csv`. | One SQL Server DB for every estate (`OPH`, `FDN`, `BLK` and more), extracted by `forecast/build_staging_series.py`. |
| Connections needed in production | One per estate | One |
| Estates and months | K3 2023-01..2026-05. EC 2025-01..2026-07. EB 2025-06..2026-01. EA 2025-06..07 only. | EC 2025-01..2026-11. EB and EA 2025-06..2026-11. P1 and P2 present but not built. No K3. |
| Bunch columns | `bunches_total` plus 14 grade columns (ripe, overripe, underripe, unripe, wet, rotten, long stalk, empty, dirty, unfresh, old, two pest-damaged, diseased) and `loose_fruits`. Ripe is 99.0-99.9% of bunches, so most grade headers hold near-zero data. | Ripe bunches only (`ZMASAK`) and loose fruit (`ZBRONDOLAN`, well filled). |
| Other harvest columns | Division and block | Harvester (`EMPNR`), harvest point (`TPH`), method, carrier distances. `IS_RAIN` and `RAIN_DURT` exist but are effectively empty. |
| Weight | None in the harvest table | Weighbridge tonnes in `FDN` (`WB_QTY_TOTAL`, `BRUTO`, `TARRA`), patchy: EA has gaps, EB starts 2026-01, EC 2026-10 |
| Joined from outside | NASA POWER weather (K3, EC), workdone (K3), block polygons | None yet |
| Forecast page today | K3 ensemble, and an EC ensemble trained only through 2025-04 | Chronos-2 estate weekly on EA, EB and EC |
| Forecast Lab today | k3, ec, eb, ea, built from the snapshots (EC to 2026-06) | stg_ec, stg_eb, stg_ea |

The page's live EC is not the Lab's live EC: the page reads an older feature file. Phase 1 puts both sources' EA, EB and EC through the same arms.

## Decisions to take before starting

Each has a recommended default. The plan assumes the default unless you say otherwise.

| # | Question | Recommended default | Why it matters |
|---|---|---|---|
| D1 | **Decided 2026-11-12:** what the page serves | **Chronos-2 zero-shot on both sources.** The arm is picked per source by the new rule (Phase 1.4). The combination and the seasonal profile are Lab challengers only. Revisiting that needs a new dated rule and is your call. | Chronos needs no training on short histories. On staging the combination scored better, so it is kept measured. |
| D2 | How the band is drawn | **Keep the measured sMAPE band as the drawn band.** Measure it on origins with ≥12 months of context, print its coverage, and add a lighter outer 80% band. Same on both sources. | The sMAPE band was a deliberate client-facing choice, documented in `forecast_router._measured_smape_band`. |
| D3 | Where off-machine copies go | The existing SageMaker S3 bucket under `forecast-runs/`, server-side encrypted. | The shadow log cannot be regenerated, and it is company data. |
| D4 | CI platform | GitHub Actions on the existing `origin` repository, synthetic fixtures only. | No company figures may enter CI. |
| D5 | Alert channel | A status file plus a banner on `/model-health`. | Enough until there is a production user. |
| D6 | What the live source serves for EA, EB and EC | The **same Lab arms and rule as staging**, built from the live snapshots: a Chronos-2 arm picked by the live rule. K3 moves to Chronos under D9. The old EC ensemble (trained through 2025-04) is retired from the page and kept in the Lab. | Like for like: same estate, same models, different source. |
| D7 | Estates one source cannot forecast (EA on live has 2 months; K3 is not in staging) | Show them in the comparison as **"not forecastable from this source"**, with the month count. | That is a drawback the client should see, not something to hide. |
| D8 | How to compare accuracy fairly when the windows differ | Two numbers per estate: each source on its **full window**, and both on the **shared window** (the same origins and target months, where both have data). | The shared window separates "different values" from "more history". |
| D9 | K3: switch from the ensemble to `chronos2_monthly_cov` now, before the shadow gate of rule 2026-09-23 has passed? | **Yes, as a recorded manual promotion.** Gate A passed (MAPE 10.45% against 14.29%), and Gate B needs 6 resolved months it does not have yet. Serve steps 1-6, as the rule says. Keep the ensemble computed as the fallback for when the Chronos forecast is missing or stale, and keep shadow-logging both. | `promotion.py` says promotion is a person's decision. This records that the person made it before Gate B, and why. |
| D10 | An estate where the picked Chronos arm does not beat naive last-3 | **Still serve Chronos**, and flag that estate on `/model-health` and in the comparison tab. The old rule served the naive baseline instead. | Keeps one model family on the page, while the client still sees where it is weak. |

Data requests for the client, not code. They run in parallel with the build:
- OPH history before 2025, for either source.
- The planned harvest calendar, if one exists.
- Which delivery method their IT can offer, and for which database.

## Order and size

| Phase | What | Size | Depends on |
|---|---|---|---|
| 0 | Shadow log on both sources, plus hygiene quick wins | S, about 1 day | none |
| 1 | Chronos-2 served on both sources (K3 promoted, live EA/EB/EC onto the Lab arms); Chronos-only dated rule per source; seasonal-profile and combination as Lab challengers | M-L, 3-4 days | 0 |
| 2 | Band measured honestly, with coverage shown, on both sources | S-M, 1-2 days | 1 |
| 3 | Config-driven estates, a data contract with shared core plus native columns, adapters for both sources | M-L, 3-4 days | none (can run beside 1-2) |
| 4 | Source comparison view for the client | M, 2-3 days | 1, 2, 3 |
| 5 | One refresh command over both sources, versioned runs, MLflow registry | M-L, 3-4 days | 3 |
| 6 | K3's fallback ensemble precomputed, so nothing trains in a request | S-M, 1-2 days | 5 |
| 7 | CI and monitoring for both sources | M, 2-3 days | 5 |
| 8 | Production connection (deferred) | n/a | client answer |

Phase 4 is the client exhibit. Its data-availability half only needs Phase 3, so it can be shown early, before the accuracy work is finished.

---

## Phase 0: start the evidence clock, plus quick wins

### 0.1 Shadow log on both sources
- `forecast/lab/shadow.py`: change `SHADOW_ESTATES` to `("k3", "ec", "eb", "stg_ec", "stg_eb", "stg_ea")`. Live EA cannot be forecast (D7).
- Record every monthly model with a forward forecast for the estate. Each row carries `source`, so scoring can split by source.
- Rebuild live EC and EB forwards from the current snapshots before the first record. Live EC's Lab anchor is 2026-06, and EB's is 2026-01 because its snapshot ends there.
- Run `record` once now.

### 0.2 Off-machine backup of the shadow log
- Add `shadow.py backup`, which copies `shadow_log.csv` to the D3 location with a dated key. Make it part of the monthly routine.

### 0.3 Environments and model pinning
- `requirements.txt`: add pandas, numpy, scipy, statsmodels, lightgbm, joblib and scikit-learn with lower bounds matching the Anaconda env.
- Write `requirements-dashboard.lock` and `requirements-chronos.lock` with `pip freeze` from the two working envs.
- `forecast/lab/build_artifacts.py`: add `CHECKPOINT_REVISION`, the Hugging Face commit hash of `amazon/chronos-2`. Pass it to `from_pretrained` and write it into `manifest.json`.

### 0.4 No hardcoded interpreter paths
- `shadow.py` and `experiment_router.BUILD_COMMAND` read `CHRONOS_PYTHON` from `.env` or config. If it is unset, fail with a message naming the variable.

### 0.5 Never show unmeasured accuracy
- `forecast_router._compute`: drop the fallback of 10.7% sMAPE, MASE 0.61 and 30 folds. A failed joblib load reports accuracy as unavailable, with the reason.
- Delete `forecast/serving.py`. It imports a missing module and nothing imports it.

**Done when:**
- `shadow.py status` lists both sources' estates, each with a first issue.
- A fresh venv installed from `requirements.txt` serves `/forecast/data` for both sources.
- Renaming a joblib produces "accuracy unavailable", not 10.7%.
- `pytest test_forecast_*.py` passes.

---

## Phase 1: Chronos-2 served on both sources

### 1.0 What "serve Chronos" means for the build
- Zero-shot: nothing is trained on our data. A model version is the triple of checkpoint revision (Phase 0.3), arm (estate weekly, monthly, monthly plus weather, or block sum) and context window. It is recorded in every artifact, selection file and shadow row.
- A refresh is inference only. The full lab build takes about 11 minutes on this CPU and the monthly arm alone takes seconds. So local is the default, and `--remote` on SageMaker is optional for speed. The SageMaker-for-training rule does not apply, since there is no training step.
- Fine-tuning stays out of scope: the block LoRA failed on K3 (MASE 0.553 to 0.904).

### 1.1 Live EA, EB and EC on the Lab arms (D6)
- `forecast_sources.py` today serves only staging. Generalise it to serve any source from the Lab: page estate id to Lab id per source, read from config in Phase 3. Until then, a dict per source.
- The live source then serves:
  - K3 from `chronos2_monthly_cov` (1.1a).
  - EC and EB from the Chronos arm the live rule selects (1.4).
  - EA as unavailable, with the reason (D7).
- `forecast_router.page_payload` routes every live estate through the generalised module. It calls the ensemble only as K3's fallback. The payload shape stays the same.

### 1.1a K3 promotion to Chronos (D9)
- In `promotion.py`, add a dated entry `manual-promotion-2026-11-XX` beside rule 2026-09-23. The rule itself is not edited. The entry records:
  - who decided, and the date
  - Gate A's result
  - Gate B's state at the time (resolved months so far)
  - the reason: short history, and a zero-shot model that needs no training
- The K3 page serves `chronos2_monthly_cov` for steps 1-6. Steps 7-12 stay labelled as seasonal outlook, as the rule recommends.
- **Fallback:** if the Chronos artifact is missing, or its anchor is older than the latest complete month, the page serves the ensemble with a visible "fallback" label. Model health shows which one is live.
- The shadow log keeps recording both the ensemble and Chronos on K3, so Gate B still resolves and the decision can be reviewed.
- Weather covariates: `chronos2_monthly_cov` reads NASA POWER history plus climatology-backfilled future values. `refresh.py` must re-fetch weather before the K3 build (`forecast/fetch_nasa_weather.py`). Model health shows weather freshness.

### 1.2 `seasonal_profile` model (Lab challenger, not served)
- New file `forecast/lab/seasonal_profile.py`, ported from `classical.py` in the experiment folder. No new dependencies.
- The model: log monthly bunches = estate level + estate amplitude × shared month-of-year shape. Unobserved months are interpolated around the year. The level is the mean of the last 3 deseasonalised months.
- **Pooling rule.** The shape is fitted on every estate of the same source, each truncated at the origin's calendar month. Live pools with live and staging with staging, never across, so each source stands on its own data.
- Register it in `registry.MODELS` with `source: "live"`, meaning computed in-process. Backtest it in `registry.backtest` and forecast it in `experiment_router._live`.

### 1.3 `combo_weekly_profile` derived model (Lab challenger, not served)
- Add a model source `"derived"` with `members` and `weights`, fixed at 0.5 and never tuned. The backtest is the weighted mean on shared (origin, step) rows. The forward forecast is the weighted mean of the members' steps. It is unavailable if the members' anchors differ.
- Implement it generically in `registry.backtest`, `registry.static_availability` and `experiment_router._live`.
- Colour: `FREE_COLOURS` is empty. Validate a new hue with the dataviz palette validator.

### 1.4 One selection module, one dated rule per source
- Rename `select_staging.py` to `select_source.py --source live|staging`, and keep a shim under the old name.
- Rule `staging-default-2026-11-10` stays as it is.
- Add `chronos-ctx12-2026-11-XX`, which applies per source on that source's estates (K3 excluded: it is fixed by 1.1a):
  - **Served candidates:** the Chronos-2 arms only: `chronos2_estate_weekly`, `chronos2_monthly`, `chronos2_monthly_cov` (where weather exists) and `chronos2_blocks`. They need a monthly backtest on every forecastable estate of the source.
  - **Rows:** steps 1-3, scoreable, from origins with ≥12 months of context. If an estate has fewer than 3 such origins, use its ≥6-month origins and say so in the file.
  - **Score:** the mean per-estate MAE ratio against naive last-3. Pick the lowest. If the pick does not beat the yardstick on some estate, it is still served and that estate is flagged (D10).
  - **Challengers:** the rule also scores `seasonal_profile` and `combo_weekly_profile` on the same rows and writes them to a `challengers` block in the file. They are never written to `served`.
- Output: `live_selection.json` and `staging_selection.json`, each naming the served Chronos arm, the rows behind it, and the challenger scores.
- Staging today: rerunning the 2026-11-10 numbers under this rule should still pick `chronos2_estate_weekly`. It had the lowest Chronos ratio, 0.58.
- Live: EC qualifies, with origins of ≥12 months from 2025-12 to 2026-05. EB has only 2 origins of ≥6 months (8 months of history). It is scored on those and flagged as low-evidence.

### 1.5 Shadow
- Add `seasonal_profile` and `combo_weekly_profile` to both sources' shadow records from their first build.

**Done when:**
- Every estate on both sources is served by a Chronos-2 arm (except live EA, which is unavailable), and the page's model label says so.
- K3 shows `chronos2_monthly_cov`. Removing its artifact switches the page to the ensemble with the fallback label.
- On staging, the challenger block reproduces the review within ±0.3 points: combination 11.3% at steps 1-3 per estate on ≥12-month origins, and 10.7% for the group.
- On live, the Chronos arms and the challengers are scored on EC and EB, and recorded in the selection file. There is no target figure for live, because this is the first time they are measured.

**New tests:**
- The rule can only write a Chronos-2 arm to `served`, on any input.
- It flags, and does not replace, an estate where the arm loses to naive.
- It scores only ≥12-month origins and falls back when there are fewer than 3.
- K3 falls back to the ensemble when the Chronos artifact is missing or stale, and labels it.
- The profile at an origin is unchanged when every later month in every estate is deleted (leakage). It never pools across sources.
- The derived model equals the weighted mean on shared rows.
- The old staging rule still selects `chronos2_estate_weekly`.
- Live and staging payloads keep one shape.

---

## Phase 2: the band, on both sources

- `forecast_sources._step_bands` takes `min_context` from the source's active rule. Rows used per step stay in the payload as `n_scored`.
- The page prints the coverage of the drawn band beside it, for example "typical error, covered 6 of 10 recent months". It adds a lighter outer band at the 80th percentile of |actual − forecast| / forecast per step, on the same rows.
- K3 under Chronos uses its 34 Lab backtest origins per step under the same rule. The ensemble's split/ACI conformal widths stay for the Investigator's band-breach trigger while the ensemble is computed as the fallback.
- **Chronos's own quantiles.** The monthly arms output q10-q90, and the estate-weekly arm does too at week grain, though not when its weeks are spread to months. Measure the q10-q90 coverage on the same backtest rows. If it lands at 70-90%, use it as the outer band for those arms, because it adapts to each forecast. Otherwise use the empirical 80% band. Record which one each served arm uses in the selection file.
- Model health: the interval card shows both bands, their coverage and the rows behind them, per source.

**Done when:**
- Staging EC under today's model shows about 15 / 23 / 29% at steps 1-3, instead of 17 / 31 / 46%.
- Both sources print coverage.
- `forecast_page.mjs` asserts the coverage text and the outer band on both sources and in both themes.

---

## Phase 3: config, data contract, adapters for both sources

### 3.1 One estate list
- New `forecast/estates.yaml`. One entry per (source, estate) holds:
  - lab id, label and page id
  - source code: the `WERKS` code on staging, the database env var on live
  - adapter name
  - forecastable or not, and the reason when not
- Load it from `forecast/estate_config.py`. Replace the hardcoded lists in `build_staging_series.py`, `registry.py`, `forecast_sources.py`, `block_weekly.py` and `gis/build_estate_data.py`'s forecast use. The chat's `config.ESTATES` is not touched.

### 3.2 Data contract: shared core plus native columns
New `forecast/contract.py` with a `CONTRACT_VERSION`.

- **Core columns**, required from every source: `harvest_date`, `division_code`, `block_code` and `bunches_total`, a non-negative bunch count. The forecast reads only these, so the models compare like for like.
- **Native columns**, declared per source, never dropped and never renamed into each other. Every column in the registry records:
  - its source name: `ZMASAK`, `ZBRONDOLAN`, `bunches_overripe` and so on
  - a plain label and a unit
  - its grain: block-day or trip
  - the core column it relates to, if any
- Extract outputs per source and estate:
  - `oph.csv`, with core plus native block-day columns. Live keeps its 14 grade columns and `loose_fruits`. Staging adds `loose_fruits` from `ZBRONDOLAN`, and harvester and harvest-point counts per block-day.
  - `monthly.csv`.
  - Staging only: `weight_monthly.csv`, with FDN tonnes per month deduplicated per trip as in `business_rules_staging.txt`, plus a coverage flag per month.
- **`meta.json` gets a `columns` block.** For every declared column it records whether the column is present, its fill rate (share of rows that are non-null and non-zero), its share of `bunches_total` where that applies, its first and last month with data, and a status:
  - populated: filled on ≥20% of rows
  - sparse: filled on 0.1-20%
  - empty: under 0.1%

  The thresholds are set in config. This block is what the comparison view reads.
- The gate checks move into `contract.py` unchanged, so every adapter is held to them. They are: block total equals estate total day by day, monthly totals match daily totals, every block has a division, a block sits in one division, and no counts are negative.
- Loaders validate on read and refuse a wrong contract version.

### 3.3 Adapters
New package `forecast/adapters/` with one base class:
- `extract_blocks(cfg, since=None)`: core plus native columns.
- `extract_daily_totals(cfg, since=None)`: the join-free total the gate compares against.
- `column_registry()`: the declared native columns.
- `describe()`: the source, for `meta.json`.

The adapters:
- `staging_sqlserver.py`: today's OPH SQL, moved verbatim, plus `ZBRONDOLAN` and counts of `EMPNR` and `TPH`. A separate FDN query feeds `weight_monthly.csv`.
- `live_postgres.py`: the `t_oph` query from `gis/build_estate_data.py` (minus `oph_is_deleted`, inside the recording window), one connection per estate.
- `live_k3_file.py`: reads `epms_production_daily.csv` (M/D/YYYY).
- `file_drop.py`: reads a delivered folder or S3 prefix in the contract's columns. This is the likely production path for either database.

`build_staging_series.py` becomes `forecast/build_series.py --source live|staging`, which reads config, runs the adapter and the gate, and writes the files.

**Done when:**
- The staging rebuild writes byte-identical core columns in `oph.csv`, and an identical `monthly.csv`.
- The live rebuild of EC, EB and EA matches the current `gis/data/estates/<E>/oph.csv` column for column.
- `file_drop` on exported files gives identical output for both sources.
- Adding P1 to `estates.yaml` builds and gates it with no code change. Remove it afterwards.
- Each `meta.json` lists every declared column with status, fill rate and window.

**New tests** in `test_forecast_contract.py`:
- Each core rule rejects a broken fixture.
- A native column can never be renamed to a core name.
- Fill-rate status thresholds behave at the boundaries.
- All adapters produce the same core frame from the same data.
- The loader refuses an old contract version.

---

## Phase 4: source comparison view (the client exhibit)

A new tab on `/forecast`, "Compare sources", next to the existing view. It follows the UI copy rule: a short label, a question subtitle, then data. Drawbacks appear as rows in tables, not as prose.

### 4.1 What data each source holds
Subtitle: "What data does each source give us?" Everything comes from the `meta.json` `columns` blocks, with nothing typed in by hand.

| Data item | Live: column | Live: status | Staging: column | Staging: status |
|---|---|---|---|---|
| Bunches harvested | `bunches_total` | populated | `ZMASAK` | populated |
| Ripe bunches | `bunches_ripe` | populated | `ZMASAK` | populated |
| Overripe bunches | `bunches_overripe` | sparse | not available | n/a |
| (one row per grade) | | | | |
| Loose fruit | `loose_fruits` | populated | `ZBRONDOLAN` | populated |
| Harvester | not available | n/a | `EMPNR` | populated |
| Harvest point | not available | n/a | `TPH` | populated |
| Weighbridge tonnes | not available | n/a | `WB_QTY_TOTAL` | sparse, with months covered |
| On-site rain | not available | n/a | `IS_RAIN`, `RAIN_DURT` | empty |
| Weather (external join) | NASA POWER | K3, EC | not joined | n/a |

- Each cell shows the source header in monospace, so the client sees the real column names. The status is a coloured marker with a text label: populated, sparse, empty or not available.
- Hovering or tapping a cell shows fill rate and first and last month.
- A per-estate toggle shows the same table for one estate. Availability differs by estate, for example FDN tonnes.

### 4.2 How much history each source holds
Subtitle: "How far back does each source go?" One horizontal timeline per estate, live and staging bars stacked, months on the x-axis. Partial months are marked, and estates a source cannot forecast are flagged with the month count (D7).

### 4.3 Do the sources agree?
Subtitle: "Where both have data, do the numbers match?" For estates in both sources:
- Monthly bunches from both sources on one chart.
- A table of months that differ by more than 0.5%, with the difference. EC April 2025 is −3.9%, from one day.

### 4.4 Which source forecasts better?
Subtitle: "Which source gives the more accurate forecast?" For EA, EB and EC, on the same models: the served Chronos-2 arm, plus the combination as a Lab reference column.

| Estate | Window | Live sMAPE steps 1-3 | Staging sMAPE steps 1-3 | Origins |
|---|---|---|---|---|
| EC | Full window per source | | | |
| EC | Shared window (D8) | | | |

The band width at steps 1-3 per source sits beside it. Forward forecasts from both sources go on one chart per estate.

### 4.5 Connection cost
Subtitle: "What does each path take to run?" A small table fed from config and the run manifests:
- databases or connections needed
- estates covered
- refresh time from the last run
- last data date

**Done when:**
- Every cell in 4.1 traces back to a `meta.json` field.
- Deleting a column from a fixture extract turns its cell to "not available" on the next build.
- The tab renders in light and dark, at phone width.
- `forecast_page.mjs` covers the tab with screenshots for the client deck.

---

## Phase 5: one refresh command over both sources

### 5.1 `forecast/refresh.py --source live|staging|all`
- Per source: extract, gate (plus a weather re-fetch for K3 on live), Chronos inference for every arm, select, shadow record, health check.
- Inference shells out to `CHRONOS_PYTHON`, locally by default (zero-shot, minutes on CPU), or with `--remote`.
- On live it also builds K3's fallback ensemble (Phase 6).
- Each run writes into `forecast/runs/<source>/<run_id>/`:
  - series files
  - lab artifacts and backtests
  - the selection file
  - `run.json`: watermark per estate, row counts, gate result, checkpoint revision, rule id, step timings, where the model ran, status.
- `forecast/runs/<source>/current.json` points at the last successful run and is rewritten only after every step passes. It is a JSON file because Windows symlinks need admin rights.
- Readers resolve through the pointer, falling back to today's paths during migration.
- Keep the last 6 runs per source and copy `run.json` plus the selection to D3.
- Exit with a non-zero code naming the failing step.

### 5.2 MLflow as the registry
- Log each refresh as a run in a new experiment, `ffb-forecast-refresh`, tagged with `source`:
  - params: rule id, checkpoint revision, watermark
  - metrics: per-estate ratios and sMAPE by step
  - artifacts: `run.json` and the selection file
- Register one model per source, `forecast-live` and `forecast-staging`, with one version per refresh.
- The page reads `run.json`, never MLflow.

### 5.3 Exercise SageMaker once
- Run `refresh.py --source all --remote` end to end, and record cost and time.

**Done when:**
- Killing a refresh mid-build leaves both sources serving their previous runs.
- Two refreshes on unchanged data give identical outputs.
- The monthly routine is one command.

---

## Phase 6: K3's fallback ensemble precomputed

Once K3 serves Chronos (1.1a), the ensemble is only the fallback and the shadow-log incumbent. It still must not train inside a request.

- `refresh.py --source live` fits K3's ensemble with `fit_final` plus `forecast_next` and writes the payload per horizon to the run folder.
- `forecast_router.get_forecast` reads it. `?refresh=1` becomes an admin-only rebuild through the refresh job.
- The Investigator, Intelligence and Lab paths read the same payload. `experiment_router._live` depends on `get_forecast` today.

**Done when:**
- The payloads equal today's, apart from `generated_at`.
- A test asserts that `fit_final` is never called while handling a request.

---

## Phase 7: CI and monitoring for both sources

### 7.1 CI (D4)
- `.github/workflows/forecast.yml` runs, on every push and PR: install `requirements.txt`, pytest for the forecast, contract and lab tests, and `ruff` on the forecast files.
- `forecast/fixtures/make_synthetic.py` generates synthetic estates in **both shapes**. The live shape has the 14 grade columns. The staging shape has `ZMASAK`, `ZBRONDOLAN` and an FDN trip table.
- CI builds both sources through `file_drop` and runs selection with a stubbed Chronos step: a seasonal naive in place of the model call. It then runs the contract tests and the comparison payload tests.
- The workflow fails if any file under `forecast/staging/`, `forecast/runs/`, `forecast/experiments/` or `gis/data/estates/` is tracked.

### 7.2 Monitoring (D5), per source
Written to `health.json` per run, and shown on `/model-health` and the comparison tab.

| Check | Alert when |
|---|---|
| Realised error | From the shadow log: an actual falls outside the drawn band 2 resolved months running, or rolling 3-month step-1 error exceeds 1.5× the backtest figure |
| Freshness | Data through is more than 40 days old, or the served Chronos artifact's anchor is behind the latest complete month (K3 then shows the fallback) |
| Chronos vs naive | The served arm's rolling 3-month step-1 error exceeds naive last-3 on an estate (D10) |
| Volume | An estate's monthly tickets or bunches move more than 40% against the same month last year, or more than 60% against the previous month |
| Column drift | A declared column changes status, for example populated to empty, or a new undeclared column appears |
| Gate | Any gate failure, with the failed run kept |

**Done when:**
- The workflow passes on a clean clone.
- Synthetic fixtures with an injected 50% drop, a column emptied, and two out-of-band months each raise their alert.

---

## Phase 8: production connection (deferred)

Covered by the "Staging production ingestion" memory note, which applies to either path:
- watermarked incremental extract (the `since` argument from Phase 3)
- Parquet landing
- a scheduler running `refresh.py`
- AWS Secrets Manager
- the adapter matching what the client's IT offers

After Phases 3 and 5, choosing a path is one config entry, one secret and one schedule. The path not chosen can be switched off in `estates.yaml`.

## Monthly routine once Phases 0-5 are built

    python forecast/refresh.py --source all [--remote]     # records and backs up the shadow log itself
    python forecast/lab/shadow.py status

Until Phase 5 is built, the old routine in `buildplan_forecast_staging.md` stays in force for staging. Live is refreshed with `gis/build_estate_data.py` and then `build_artifacts.py --source live`. After Phase 0, `shadow.py record --rebuild` and `shadow.py backup` are added for both.
