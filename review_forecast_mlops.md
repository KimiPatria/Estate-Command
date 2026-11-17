# Forecast page: MLOps maturity and staging accuracy review

Date: 2026-11-12. Scope: `/forecast`, `/model-health` and `/experiment/forecast` on both sources, with most attention on the staging source (EPMSStagingDB: EA, EB, EC).
Experiment code: `forecast/experiments/staging_accuracy_2026-11-12/` (gitignored with the other lab artifacts). Rerun order: `classical.py` (dashboard env), `chronos_exp.py` (chronos-rd-py313 env), `analyse.py`, `robust.py`, `forward.py`.

> **Decision after this review (2026-11-12):** the user chose to serve Chronos-2 zero-shot on both sources, K3 included. The combination recommended in 3.5 stays a Forecast Lab challenger and is not served. See `buildplan_forecast_mlops.md`, decisions D1, D9 and D10.

## 1. Verdict

- **Maturity: Level 1 of 3 ("pipeline"), with Level-2 governance.** The evaluation and promotion discipline is stronger than most production teams': pre-registered dated rules, walk-forward backtests, a Diebold-Mariano test, leakage tests and a shadow log. Everything around it is manual and tied to one laptop. That includes the runs, the environments, artifact storage, the shadow log and monitoring.
- **Accuracy can be roughly halved without new data.** Averaging the served Chronos-2 estate-weekly forecast with a simple seasonal-profile model cuts steps 1-3 sMAPE on origins with a year of context from **18.9% to 11.3%** per estate. The group figure falls from **15.9% to 10.7%**, and the step-3 band from **±27% to ±14%**. The small sample means this needs the shadow log before it is served (section 3.4).
- **Plug-and-play is about 60% there.** The page already reads files through a source switch with one payload shape, and that is the right seam. What is missing sits between the database and those files: a written data contract, a source adapter interface, one refresh command, config-driven estates and pinned environments (section 4).

## 2. MLOps maturity scorecard

Levels: 0 manual, 1 pipeline, 2 CI/CD with automated tests and registry, 3 automated retraining and SLA-backed monitoring.

| Area | Level | What is there | Main gap |
|---|---|---|---|
| Data ingestion and validation | 1+ | `build_staging_series.py` checks a block-to-estate total reconciliation day by day, monthly-to-daily totals, the division join and negative counts. A failed gate writes nothing. Exclusion rule and meta.json are recorded. | It re-reads all of OPH every run, writes CSV, has no watermark, schema-drift check or volume-anomaly check, and runs only by hand. |
| Build and training pipeline | 1 | `build_artifacts.py` sends one batched Chronos run either locally or to SageMaker. | The refresh is 3-4 manual commands across two conda envs. The interpreter path is hardcoded in `shadow.py` and `experiment_router.py`. The SageMaker path has never been run end to end. |
| Experiment tracking | 1 | MLflow, a local sqlite store: 13 ensemble runs and 37 R&D runs. | Staging lab builds and selections are not logged to MLflow. They live in `manifest.json` and `staging_selection.json`. |
| Model registry and versioning | 0-1 | `registry.py` defines models in code. The selection file records rule, scores and reason. | MLflow has no registered models. Artifacts are overwritten in place, with no run id and no hash. Everything is gitignored and exists on one machine only. The `amazon/chronos-2` checkpoint is not pinned to a revision. |
| Evaluation and promotion governance | 2 | Walk-forward with no leakage (tested), yardstick models, a dated rule that may not be edited, DM-HLN, and a shadow gate before promotion. Promotion is a person's decision. | The staging rule scores origins with only 6 months of context (section 3.1). The band is labelled as uncertainty but covers only 50-60% of outcomes on both sources (section 3.3). |
| Serving | 1 | Staging serves precomputed files, a correct batch pattern with an mtime-keyed cache and a stale note. The payload shape is shared with live. | Live refits LightGBM inside the request on a cache miss (`forecast_router._compute` calls `fit_final`). Live also falls back silently to hardcoded accuracy numbers (10.7% sMAPE, MASE 0.61, 30 folds) if the joblib fails to load, so the page could show accuracy it never measured. `forecast/serving.py` imports a missing `daily_model`. |
| Monitoring | 0-1 | The model-health page shows freshness, gate result and selection reason. The shadow log covers K3 and EC live. | Staging is not in the shadow log, so no out-of-sample evidence is accumulating for the model actually served on staging. Drift is "not wired". There are no alerts and no schedule. |
| CI/CD and tests | 1 | 41 pytest checks pass (staging, lab, theme), plus node page tests. | No CI. `requirements.txt` lacks the forecast stack (pandas, numpy, lightgbm, statsmodels, joblib) and pins nothing, so a fresh install cannot run the forecast page. |
| Environment and reproducibility | 0-1 | `requirements-chronos-remote.txt` is curated for SageMaker. | Two hand-built conda envs, unpinned versions, no container image, and an unpinned model checkpoint. |
| Secrets and access | 1 | The staging login `epms_ro` is read-only. | The password sits in `.env`, with no secret store. |

## 3. Accuracy on the staging database

### 3.1 Why the current band is wide

The causes are measurement and series shape, not data quality. The previous diagnosis is in memory; in short:

- The band is the per-step sMAPE over every backtest origin from 6 months of context. Origins inside the first season score 50-70%. The zero-shot model holds the peak or extrapolates the decline, and EC origins in September and October 2025 forecast zero by step 11.
- EB and EC swing 3.4-4.0x within a year, against about 1.4x on K3.
- The staging database starts in January 2025 for EC and June 2025 for EA and EB. The older live snapshots start at the same dates, so there is no extra history to stitch on.

### 3.2 What was tested

Same walk-forward origins as the Forecast Lab and the same scoreable months, steps 1-6. Every method sees only data up to the origin, and that holds for all three estates. The figures below are scored on origins with **at least 12 months of context**, which is the regime production will be in from now on: 20 estate-origins, 51 rows at steps 1-3.

| Method | sMAPE steps 1-3 | MAE vs naive last-3 | Bias | Band at steps 1 / 2 / 3 |
|---|---|---|---|---|
| **Chronos-2 estate weekly + seasonal profile, 50/50** | **11.3%** | **0.29** | +1.7% | **8.5 / 12.3 / 14.0** |
| Same, profile on harvest-day rate | 11.8% | 0.32 | +0.7% | 8.7 / 11.7 / 16.2 |
| Seasonal profile alone (estate's own) | 13.7% | 0.34 | -4.5% | 14.6 / 12.8 / 13.4 |
| Seasonal profile alone (pooled across estates) | 14.9% | 0.41 | -2.5% | 15.6 / 14.9 / 13.7 |
| Chronos-2 estate weekly (**served today**) | 18.9% | 0.51 | +5.8% | 12.2 / 20.1 / 27.0 |
| Chronos-2 monthly + harvest-day covariate + cross-learning | 25.0% | 0.69 | +20.2% | 14.1 / 25.5 / 40.0 |
| Chronos-2 monthly (lab arm) | 26.5% | 0.78 | +24.5% | 15.0 / 27.3 / 42.1 |
| Chronos-2 block sum (lab arm) | 29.7% | 0.76 | +18.3% | 18.4 / 31.7 / 43.4 |
| Naive last-3 mean (yardstick) | 39.4% | 1.00 | +21.3% | 31.0 / 41.2 / 49.5 |

Group view, EA + EB + EC, steps 1-3:

| Origins | Served today | Combination |
|---|---|---|
| All 11 lab origins | 19.3% (bands 11.4 / 20.0 / 28.1) | 11.6% (bands 11.0 / 11.3 / 12.9) |
| 5 origins with ≥12 months | 15.9% (bands 10.3 / 16.5 / 24.6) | 10.7% (bands 7.4 / 12.7 / 13.5) |

The combination is also best on all lab origins, including the early ones, at 18.5% against 24.5%.

**The seasonal profile** fits log bunches as estate level plus estate amplitude times a month-of-year shape. Months not yet observed are interpolated around the year. The level comes from the last three deseasonalised months. It is about 60 lines of pandas with no training step. The pooled version shares the shape across EA, EB and EC, which helps at early origins. It makes little difference now that every estate has seen all twelve months.

**Why the combination works.** The two methods fail in opposite ways. Chronos holds a peak too long, as in June 2026 when it missed the August collapse. The profile knows the seasonal turn but is slow on level shifts. Averaging removes the large misses. The combination wins 12 of 20 estate-origins, so it is not better every month. But it cuts mean absolute percentage error by 8.6 points. DM-HLN p = 0.011, which is anti-conservative because origins overlap.

**What did not help:**
- **Harvest days as a covariate** gave at most 1 point, even with the actual harvest days as an oracle. Harvest days correlate with month-to-month changes (r = 0.4-0.57), but Chronos does not use the signal, and rate-per-harvest-day models were no better.
- **Chronos-2 cross-learning** across the three estates changed nothing measurable.
- **Block-level forecasts summed** were worse than the estate series, as on K3.

### 3.3 The band is not a prediction interval, on either source

Both pages draw ±(measured sMAPE at that step). On origins with a year of context, the share of actuals inside that band was:

| Model | Share inside the band, steps 1-6 |
|---|---|
| Served staging model | 0.50 / 0.76 / 0.71 / 0.64 / 0.75 / 0.80 |
| Combination | 0.55 / 0.41 / 0.57 / 0.55 / 0.38 / 0.80 |
| Live K3 ensemble | 0.63 / 0.48 / 0.57 (steps 1-3) |

The live "±10%" is therefore also a roughly 55% band, not a ±10% guarantee, and the two sources are compared like for like. The industry-standard fix is an empirical, conformal-style 80% interval, labelled with its coverage. For the combination it is about ±18 / 18 / 25% at steps 1-3, and still narrower than today's step-3 band from step 2 on.

### 3.4 How confident to be

- 20 estate-origins, of which only 5 each for EA and EB, and overlapping horizons.
- About 20 variants were scored on the same data, and the profile model was designed after looking at the series. The 11.3% is optimistic. A fair expectation is low-to-mid teens at steps 1-3, still clearly better than 18.9%.
- The current rules already handle this. Add the combination as a challenger under a **new dated rule**, start logging it and the served model in the shadow log for the staging estates, and switch after about 6 resolved months, or earlier at the user's discretion.
- The forward forecast differs most for EB in January and February 2027. The profile carries EB's +35% year-on-year level into the new season: 1.06M for January against 0.90M served and 0.79M last year. It is worth a sanity check with the estate before relying on it.

### 3.5 Ranked recommendations for accuracy

1. **Add "Chronos-2 estate weekly + seasonal profile, 50/50" as the staging challenger** under a new dated rule. Weights stay fixed at 0.5 and are not tuned on this data.
2. **Measure the served band on origins with at least 12 months of context**, again under a new dated rule. This changes no forecast, only the honesty of the band: EC would go from 17 / 31 / 46% to 15 / 23 / 29% with the current model.
3. **Replace the sMAPE band with an empirical 80% interval and print its coverage**, on both sources.
4. **Ask the client for pre-2025 OPH history.** On K3 the same zero-shot model went from 45% sMAPE with under a year of context to 10% in its third year. More history is the strongest lever and costs no modelling.
5. **Untested and worth a look:** NASA POWER weather for EA and EB, which helped on K3 through the `chronos2_monthly_cov` arm, and a nowcast of the current month from the days already recorded. November has 4 days in the database, and the page discards them today.
6. **Not worth more time:** harvest-day covariates, cross-learning, block sums, and fine-tuning. The earlier block LoRA failed on K3.

## 4. Roadmap to plug-and-play production

Production means the same page, pointed at the client's real database, refreshed on a schedule with no code change. The design for the connection itself is in memory as "Staging production ingestion" and is not repeated here. These are the steps that make it plug-and-play, in order.

### P0: do during research, because each is cheap now and expensive later

1. **Write the data contract down.** Set the schemas for `oph.csv`, `monthly.csv` and `meta.json`: columns, types, units, the exclusion rule and the gate checks. Use a pydantic or pandera schema, validated at the end of every extract. Anything that delivers files passing it is a valid source.
2. **Put a source adapter interface in front of the contract.** Each adapter implements `extract(estate, since) -> daily block frame`. Today's SQL Server pull becomes the first one. A restored backup, an S3 or SFTP drop and a direct production read become new adapters, not edits to the pipeline. Pick the adapter in config.
3. **Move estates to config.** One YAML lists estate code, `WERKS`, label and adapter. Today the list is hardcoded in four places: `build_staging_series.py`, `registry.py`, `forecast_sources.py` and `block_weekly.py`. Adding P1 or P2 should be a config line.
4. **One refresh command** running extract, gate, build, select, shadow record and health check. It writes a run manifest with run id, data watermark, row counts, gate result, checkpoint revision and timings. Each step's output goes to `runs/<run_id>/` and a `current` pointer flips only after every step passes, so a failed refresh leaves the last good forecast serving. It returns a non-zero exit code on failure, so any scheduler can run it.
5. **Pin the environments.** Add the forecast stack to `requirements.txt`, write lock files for both envs, pin the `amazon/chronos-2` revision, and replace hardcoded interpreter paths with an env var or config. Build one container image for the build job and reuse it on SageMaker.

### P1: before the first production user

6. **Extend the shadow log to the staging estates**, for both the served model and the challenger. Back it up off the machine, to S3 or the MLflow artifact store. It is the only evidence that cannot be regenerated.
7. **Use MLflow as the registry.** Log each refresh as a run and register the served staging forecast as a model version tagged with rule id and data watermark. The page reads the version it serves from there.
8. **Fix the live serving path.** Precompute live forecasts in the refresh job as staging does, so nothing trains inside a request. Fail visibly instead of falling back to hardcoded accuracy, and delete or repair `forecast/serving.py`.
9. **Add CI**, for example GitHub Actions: pytest, the schema contract tests, and a build smoke test on a small fixture extract, on every PR.
10. **Monitor what matters for a monthly forecast.** Distribution tests on features do not fit here. Track realised error from the shadow log against the band. Alert when an actual falls outside the band two months running, when data freshness passes N days, when an extract's row count moves more than X% against the same month last year, or when the gate fails.

### P2: when it moves to the client environment

11. A scheduler: Windows Task Scheduler or cron first, then EventBridge Scheduler with ECS or Step Functions, or Prefect or Dagster, whichever the client runs.
12. Secrets in AWS Secrets Manager, not `.env`.
13. A Parquet landing zone partitioned by estate and month, in place of CSV.

Done this way, connecting to production means writing or configuring one adapter, adding the connection secret and scheduling one command.
