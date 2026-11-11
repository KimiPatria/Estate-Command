# Build plan: forecast on the staging database, restyled to match chat

Status: built 2026-11-10, except the SageMaker run (written, not yet run: `aws login` had expired). Plan written the same day.

## What was built, and where it differs from the plan below

| Area | Result |
|---|---|
| Restyle | `forecast_static/theme.css` + `theme.js`; all three pages on the chat tokens, light and dark. Chart.js from the chat vendor folder, Leaflet vendored. |
| Extract | `forecast/build_staging_series.py`; gate passes for EA, EB, EC. `STATE = 'D'` rows left out (13 rows, 214 bunches). |
| April 2025 EC gap | One day, 2025-04-02: live holds 164,822 bunches, staging 88,303. Every month from May 2025 on agrees to the bunch. |
| Models | Staging estates registered as `stg_ec`, `stg_eb`, `stg_ea`. Built locally on CPU in 642 s. |
| Selection | Rule `staging-default-2026-11-10` picked **Chronos-2 estate weekly** (mean MAE ratio 0.58 against naive last-3). |
| Router | `source=live|staging` on `/forecast/data` and `/forecast/model-health`; `/forecast/sources`; `forecast_sources.py`. |
| Selector | Database chip on all three pages, shared with chat through the `chat-db` key. |
| Tests | `test_forecast_theme.py`, `test_forecast_staging.py`, `command_static/test/forecast_page.mjs`. |

Differences from the plan:
- **Selection score.** Mean of the per-estate MAE ratios, not pooled MAE, so EC and EB (three times EA's volume) do not decide it alone. Fixed before any staging backtest was run.
- **Band.** The page draws the measured per-step sMAPE band (the live page's method), not the model's own percentiles: the selected model has no real interval at the month grain, and the group sum has none at all.
- **Build path.** `build_artifacts.py` now collects every forecast request, runs the model once, then writes. `--remote` sends that one run to SageMaker; without it the same run happens locally. A local rebuild of two K3 artifacts reproduced the old files exactly.
- **Estate tiles.** Each tile now shows its own estate's forecast on both sources. Before, every modelled tile repeated the selected series' total (EC showed K3's 348.4K).
- **Not done:** the SageMaker run itself, the fine-tune and TimesFM challengers, and a dark-surface run of the dataviz palette validator (the lab lifts low-contrast line colours on dark to 3:1 instead).

Refresh routine after new staging data:

    python forecast/build_staging_series.py
    <chronos-rd-py313 python> forecast/lab/build_artifacts.py --source staging --remote
    python forecast/lab/select_staging.py

Accuracy on staging, steps 1 to 3 (walk-forward, small samples):

| Scope | sMAPE | Origins | Band at step 6 |
|---|---|---|---|
| EA + EB + EC | 19.3% | 11 | ±48.0% |
| EC | 30.6% | 16 | ±68.7% |

## Goal

1. The forecast page can run on the EPMS staging database as well as the live files, chosen with the same Database control as the chat page.
2. `/forecast`, `/model-health` and `/experiment/forecast` look like `/chat`: same colours, type, topbar, controls, light and dark themes.

## Decisions taken (2026-11-10)

| Question | Decision |
|---|---|
| Which pages get the restyle | All three: `/forecast`, `/model-health`, `/experiment/forecast` |
| Shared stylesheet | Safer option: a forecast-side copy of the chat tokens; `chat.css` is not touched |
| Plasma schemes P1 and P2 | Left out for now |
| Deep-learning training | Runs as a SageMaker job, not on this machine |

## What the staging data holds

Measured read-only from `OPH` on 2026-11-10 (`SUM(ZMASAK)` by `BUDAT`, bunches, not tonnes).

| Estate | Data from | Data to | Full months | Weekly points (approx.) |
|---|---|---|---|---|
| EC | 2025-01-02 | 2026-11-04 | 22 | 95 |
| EA | 2025-06-02 | 2026-11-05 | 17 | 73 |
| EB | 2025-06-02 | 2026-11-05 | 17 | 73 |

- K3 is not in staging, so it stays a live-only estate.
- November 2026 is partial (3 to 4 recorded days) and is dropped from history.
- The served EC model today is trained on `forecast/EC/features_estate_monthly.csv`, which ends 2025-05 (4 usable months). Staging replaces that with 22 months.
- Staging EC against the old EC snapshot: January to March 2025 agree within 0.7%; April is 4.5% lower in staging. Cause unknown.
- `forecast/staging/**` is not covered by the existing gitignore rule (`forecast/*/*.csv` is one level too shallow). It needs its own line.

## Phase 1: extract and data gate

New `forecast/build_staging_series.py`, run in the dashboard env (it needs the SQL Server driver).

- Reads `OPH` through `data_sources.get("staging").engine` (the read-only `epms_ro` login).
- Writes to `forecast/staging/<ESTATE>/`:
  - `oph.csv`: daily bunches per block, in the same columns as `gis/data/estates/<CODE>/oph.csv`, so `block_weekly.load_daily_blocks` and the lab loaders read it unchanged. Division comes from `BLK` (current rows, `KDATE = '9999-12-31'`).
  - `monthly.csv`: month, bunches, recorded days, partial flag.
  - `meta.json`: data-through date, row counts, extract time.
- Gate, modelled on `build_daily_series.monthly_bridge_check`: daily sums must equal the monthly file, and block sums must equal the estate total. A failed gate stops the build.
- Open questions to close before any model runs:
  1. What `STATE`, `SAP_FLAG` and `DELIV` mean, and whether any value marks a cancelled or duplicate ticket that must be excluded.
  2. Why April 2025 EC is 4.5% lower than the old snapshot.
- Add `forecast/staging/` to `.gitignore`.

## Phase 2: models, trained and scored on SageMaker

### What runs where

| Work | Where | Why |
|---|---|---|
| Extract (Phase 1) | This machine | Needs the local SQL Server |
| Zero-shot Chronos-2 forward forecasts and walk-forward backtests | SageMaker job | GPU; one job covers every estate, grain and origin |
| Any fine-tune (Chronos-2 LoRA) or other DL model (TimesFM) | SageMaker job | Training |
| Naive baselines, serving, the page | This machine | Cheap, no GPU |

Zero-shot Chronos has no training step: it is inference only, and the current local build takes about 6 minutes. A SageMaker job adds a few minutes of start-up, so for the estate-level zero-shot run alone the gain is small. The gain is real for the block-level track (hundreds of series per origin) and for any fine-tune. One job for everything keeps a single code path.

### How

- Reuse `chronos_finetune_sagemaker.remote_job` (the `@remote` wrapper already used by `chronos2_blocks_finetune.py`), with its defaults: account role, bucket `sagemaker-ap-southeast-3-430287291296`, region `ap-southeast-3`, `ml.g5.xlarge`.
- New entry point `forecast/lab/build_artifacts.py --remote`: packages the staging series, runs the existing producers on the instance, and downloads the forward JSON and backtest CSVs into `forecast/experiments/lab/`. Without the flag it runs locally as today.
- Keep the existing safety flows: `--local` smoke run first, `--recover-job <name>` when `aws login` expires mid-poll, `--max-runtime-s` cap.
- The account quota is one concurrent `ml.g5.xlarge` training job, so jobs run one after another.
- The job uploads staging series (bunch counts per block and day) to the account's S3 bucket, as the September fine-tune did with EC and EB.

### Arms

Registered in `forecast/lab/registry.py` under their own estate ids (`stg_ec`, `stg_ea`, `stg_eb`), so the live estates and the fixed K3 promotion rule are untouched.

| Arm | Status |
|---|---|
| Chronos-2 estate weekly, summed to months | Zero-shot, expected default |
| Chronos-2 monthly | Zero-shot |
| Chronos-2 block sum | Zero-shot |
| Naive last-3 mean | Baseline, the MASE yardstick here |
| Seasonal naive | Shown, but cannot be scored until an estate has more than 12 months before a target |
| Chronos-2 LoRA fine-tune (pooled EA+EB+EC blocks) | Optional challenger, not on the critical path |
| TimesFM | Optional challenger |

### Fine-tune caution

The September LoRA fine-tune on EC and EB failed (K3 step-1 MASE 0.553 to 0.904): the adapter learned the shared 2025 second-half decline. Staging covers the same estates over 17 to 22 months, which is still little more than one seasonal cycle. If the fine-tune is run:
- hold out the last weeks of every estate by time (there is no K3 to hold out);
- score per estate against zero-shot on identical folds, three seeds;
- ship nothing if any estate gets worse.

### Selection rule

Written and dated before the backtest results are read, like the K3 promotion rule:
- the staging default is the arm with the lowest pooled MAE over steps 1 to 3 across the three estates, provided it beats naive last-3 on each estate;
- if none does, staging serves naive last-3 and says so.

### Serving limits

- Steps 1 to 6 only.
- Band: the model's own 10th to 90th percentile, drawn only where it is a real interval for the number shown (the existing lab rule).
- No SHAP drivers and no weather covariates for staging.
- Accuracy is shown with its origin count: 10 to 16 origins is a noisy sample.

## Phase 3: router

`forecast_router.py`:
- `source=live|staging` on `/forecast/data` (default `live`), part of the cache key. Staging returns the same payload shape, so the page's `render()` is reused.
- `GET /forecast/sources`: id, label, available, data-through, built from `data_sources.available()` and `data_through()`.
- Modelled estates become per source: live is K3 and EC; staging is EA, EB and EC.
- Staging reads artifacts only and never refits. Missing artifacts return the page's existing N/A state with the build command.
- Staging can serve a true group total (EA+EB+EC over their common window), which the live source cannot.
- Not in v1 for staging: intelligence and investigator panels (they read the live DB and SHAP), block map and block table.
- `/forecast/model-health` gains the same `source` parameter; for staging it reports the backtest table, data-through and artifact age.

`experiment_router.py`: the catalog lists the staging estates; no new endpoints.

## Phase 4: one look across the three pages

New `forecast_static/theme.css`, a copy of the chat page's tokens and shared components. `chat.css` is not edited. All three forecast pages load it (the lab links it from `/forecast-static/`).

- Tokens: the chat `:root` and `[data-theme="dark"]` blocks, Inter, 12px radius, the `--s1` to `--s8` series colours.
- Theme: same pre-paint script and toggle as chat, same `chat-theme` storage key, so the choice carries across pages.
- Topbar and page nav as on chat; the sliding-pill nav goes.
- Controls: horizon toggle, scope chips, refresh, analyse and lab selectors become chat-style chips, pill buttons and popovers.
- Cards, tables, badges and N/A states move to the token colours; hard-coded `#fff`, `#f7faf9` and gold gradients go.
- Charts: gridlines, ticks, tooltips and band fills read the tokens and redraw on theme change.
- Libraries: Chart.js from `chat_static/vendor/`; Leaflet vendored too, so the pages work offline.
- Lab model colours were validated on the light surface only. Re-run the dataviz palette check on the dark surface and adjust if a pair fails.
- Drift guard: a small test that fails when the token blocks in `chat.css` and `theme.css` differ.

Page copy stays as it is. This phase has no data dependency and can run before or beside Phases 1 to 3.

## Phase 5: source selector

- A Database chip in the forecast titlebar, opening the chat-style popover with Live estates and Staging, each with its data-through date.
- Shares the `chat-db` storage key with chat; `?db=staging` deep link.
- Changing source reloads the scope bar and estate chips for that source.
- The lab gets the staging estates in its estate selector; model health follows the forecast page's source.

## Phase 6: tests

- `test_forecast_staging.py`: extract gate, `source` contract, payload shape parity with live, no leakage in staging backtests.
- `command_static/test/forecast_page.mjs` (CDP driver, as for chat): selector, theme toggle in both themes, staging estates render, N/A states.
- Regression: `forecast_lab.mjs` (33 checks), `test_forecast_lab.py` (9), `test_chat_experience.py`, `test_data_sources.py`.

## Order

1. Phase 4 restyle (independent).
2. Phase 1 extract and gate; close the two open data questions.
3. Phase 2 local smoke run, then the SageMaker job; fix the selection rule first.
4. Phase 3 router.
5. Phase 5 selector.
6. Phase 6 tests throughout; full regression at the end.

## Later

- Plasma P1 and P2.
- Tonnes from `FDN` (deduped per SPB) once EC's delivery data is dense enough.
- Block map and block table on staging.
- Re-anchoring Estate Command's synthetic EC world to the staging end date.
