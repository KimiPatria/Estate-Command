# Estate Command: learning the four numbers the plan guesses

Plan for replacing four hand-set inputs of tomorrow's assignment with fitted
models: who turns up, how much of the plan gets done, what a crew can do in a
man-day, and whether it will rain.

Written against the build as it stands on 2026-09-14: 67 crews and 9,581
crew-days of attendance, 7,656 harvest orders, 3,662 upkeep, 632 pest and 7,278
dispatch orders, a 22-assumption register, the greedy scheduler, the Did-it-work
panel, a 29-tool copilot, and four fitted models already live (productivity
OLS, lagged forecast, shrinkage IsolationForest, HDBSCAN clusters).

---

## Build status

All six phases built on 2026-09-14. Four models live behind the plan, a
**Tomorrow's outlook** window in the Governance domain (44 features, 36 live,
42 panels), a Forecast section in every operation window, forecast checks in
Did-it-work, a 34-tool copilot, and ten new register entries in a Forecasting
group. `test/models.mjs`, `test/ops.mjs` and `test/smoke.mjs` pass (97 window
sections, no console errors).

Every figure travels with a sentence a non-specialist can act on, and every
model with a one-word trust grade from its backtest: **Reliable** (25%+ better
than the method it replaces), **Fairly reliable** (10–25%), **Rough guide**
(up to 10%), or **Not better than the old way** (not used).

| Model | Landed as | Held-out result | Grade |
|---|---|---|---|
| Rain | `gis/models/rain.py`, `gis/build_rain_forecast.py` → `ec_rain_forecast.json` (954 days of real forecasts) | Chance of 15 mm+ 14.4% more accurate than the month's usual rain and 36.6% more than the raw forecast, over 861 days; 25 mm+ 7.1% better | Fairly reliable |
| Headcount | `gis/models/headcount.py` | Off by 1.81 people per crew-day against 2.32 for the 14-day average (22% better); Sundays 2.32 against 4.83; the week after Lebaran 2.03 against 3.31; estate daily total 53 against 105; the 80% range held on 87% of crew-days | Fairly reliable |
| Work done | `gis/models/slippage.py` | At six in the morning, off by 18.2 points per order against 19.7 (8% better). Knowing the rain that fell, 13.2 against 17.8 (26% better): what is left is mostly not knowing the weather | Rough guide |
| Speeds | `gis/models/rates.py` | Harvest block pace 19.5% better than taking every block as average; weeding 6.5%, spraying 6.3%, pruning 2.2%; 16.1% weighted by orders | Fairly reliable |

### The planted-truth checks, measured

| Check | Result |
|---|---|
| Headcount: Sunday, Lebaran, heavy rain, and no payday effect | 6 of 6, each within 0.04 of what the generated data carries |
| Work done: rain per mm, road effects, no crew effect, block record | 8 of 8 testable. Harvest 1.1% per mm against the rule's 1.3%; block record against the hidden quality field 0.52 |
| Speeds: pruning and weeding crews against the hidden `skill` | 0.95 and 0.90 correlation |
| Speeds: spraying teams | **0.65 across 7 teams, under the 0.7 bar.** 104 orders is too few; the check stays red |
| Speeds: harvest block pace repeats between January–Lebaran and after | 0.57 across 284 blocks |
| Answer key | No payload carries `skill`; the loader strips it and the test asserts it |

### Tomorrow, 2025-05-24, in the words on screen

> Rain: 54% chance of enough to wash off spraying; heavy rain is unlikely (20%).
> People: expect 1,069 to 1,148 of 1,267 to turn up.
> Harvest: expect about 81% of the plan done (66% to 92%): about 392 t of the 485 t planned.
> Spraying: hold (54% chance of wash-off, break-even 24%).

Replaying two accepted plans each on 2025-04-22 and 2025-05-12, Did-it-work
found 11 of 12 forecasts landed where they said. The miss was a spraying plan
that finished 100% against a 0–97% range.

### Corrections to this plan, found by building it

- **The rain was measured on misaligned days.** The recorded archive sums days
  in Asia/Jakarta time; the first comparison used Asia/Jayapura. Aligned, the
  raw forecast's correlation is 0.49, not 0.29. The Previous Runs archive also
  reaches back to 2024-02-01, so the model is scored on 861 days, not 136.
- **Spray-or-hold counted labour.** Labour is paid either way, so it cancels.
  The first version held spraying at any chance above 7% in a wet estate, which
  meant never. Holding is now priced as the deferral cost times the expected
  wait for a drier day (1 / (1 − the month's usual chance)), against the
  herbicide a washed round wastes. The herbicide figure was lowered to a
  100,000 IDR per planted hectare placeholder, still to agree with the estate.
- **Harvest gangs cannot dip.** The generator floors a gang's attendance at the
  cutters it used that day, so their Sunday and Lebaran dips are 76% and 84%,
  not the rule's 62% and 72%. The model has separate terms for them, and every
  recovery row now shows three figures: the rule, what the data carries after
  the generator's own adjustments, and what the model found.
- **The expected share was biased high.** A lognormal expectation over lopsided
  errors overstated work done by about 12 points in April. It now averages over
  the model's own past errors, and a Lebaran term was added.
- **No road here is impassable when wet.** The generator has the rule, but the
  estate's road feed never uses the condition. Those rows read "not in the data".
- **Harvest blocks do have a pace.** The plan expected none beyond the
  productivity target, but whole-cutter gang sizing leaves one, and it predicts
  unseen weeks 20% better. The check became: does it repeat from one period to
  the next?
- **Pest rates are not learned.** Pest man-days are generated as palms over the
  method's rate, so a learned rate would only rediscover the book.
- **Capacity already read the answer key.** `ops.capacity` multiplied every
  crew's capacity by `skill`. It no longer reads the column at all.
- **Replays changed meaning.** With the forecasts on, a replayed day is planned
  on the forecast headcount and rain, and what actually happened is shown beside
  it. With them off, a replay uses the recorded figures, as before.
- **Did-it-work scored work done against the wrong base.** It compared output on
  the planned blocks with the plan's quantity, so a block the estate did not work
  counted as a forecast miss. It now compares with the ledger's own done over
  planned on the planned blocks that were worked.
- **The risk flag was too broad.** At 5 in 10, most harvest blocks qualified,
  because an order only counts as finished at 85%. The flag is now 7 in 10.
- **Where it lives.** Endpoints landed under `/gis/forecast/*` (`outlook`,
  `rain`, `headcount`, `work-done`, `speeds`, `accuracy`), not the names in
  section 6. The operation windows got a **Forecast** group rather than
  **Models**, and a separate outlook window was added for people who want all
  four at once.

---

## 0. What changes

The scheduler is not the problem. It is arithmetic that can be defended to a
mandor line by line, and it stays that way. What it plans on is four guesses:

| Input | Today | Where it fails | Model |
|---|---|---|---|
| Men present tomorrow | Trailing N-day mean attendance times the roll, `ops._expected_present` | Lags every step change. The week after Lebaran it still carries the dip; it plans a Sunday like a Tuesday | **Headcount** |
| How much of the plan gets done | Ledger adherence in the day's rain bucket, `scheduler._weather` | One figure for every crew and block. A gang on an impassable-when-wet road and one on a good road get the same expectation | **Slippage** |
| Output per man-day | Productivity OLS for harvest; a flat literature figure for prune, weed, spray and pest | Never moves. A crew that does 20% more than the book is planned as if it did the book | **Rates** |
| Rain on the day | Recorded rain on a replay, "unknown" for tomorrow, or typed in | See below | **Rain** |

Nothing here turns the scheduler into a model. It gets better inputs, each
with a switch in the assumption register and the current guess as fallback.

### A correction this plan makes: replay reads the answer

A plan replayed for a past date reads the rain recorded **that day**. At six in
the morning nobody had that figure. The spray go/no-go on a replay is therefore
perfect by construction, and Did-it-work on replayed spray plans overstates the
plan.

What was actually available the evening before, measured on 2026-09-14 against
Open-Meteo's Previous Runs API (`precipitation_previous_day1`, the forecast
issued the day before) for the 136 days from 2025-01-05 to 2025-05-20:

| | Recorded (archive) | Forecast the day before | Both |
|---|---|---|---|
| Total rain | 1,831 mm | 873 mm | |
| Days at or above 15 mm (spray washes off) | 54 | 14 | 4 |
| Days at or above 25 mm | 18 | 1 | 0 |
| Correlation, daily mm | | | 0.29 |

The raw forecast halves the rain and catches 4 of 54 wash-off days. That is
why the rain model below is a calibration with a probability, not a
threshold on the forecast figure.

---

## 1. The rules every model here follows

1. **Beat the input it replaces, on held-out time, or ship the input.** Every
   model is backtested against the guess it replaces. If it does not beat it,
   the register switch stays on the fallback and the Models section says so.
2. **Only what was known the evening before.** Every feature, every baseline
   and every replay reads data dated before the plan's date. This includes the
   baseline: today's rain-bucket adherence reads the whole ledger, future days
   included, and has to be rebuilt to compete fairly.
3. **Recovery is not discovery.** Three of the four models train on generated
   feeds whose rules are known. A model that finds them has passed a test, not
   learned something about the estate. Each is scored the way
   `shrinkage.py` is: injected against recovered, plus effects that were
   deliberately **not** planted, which must not come back.
4. **Written on a whiteboard.** numpy, coefficients a supervisor can read, the
   same stance `productivity.py` takes. A scikit-learn challenger may be fitted
   and its score reported in the Models section; it does not drive the plan.
5. **A predicted figure wears its own badge.** A new provenance term,
   `predicted`, always paired with what it was trained on:
   `predicted · trained on synthetic`, `predicted · trained on real`. It never
   borrows `real`, and it is distinct from `scheduled`.

### One harness: `gis/models/backtest.py`

Rolling origin, weekly. The first origin is 2025-02-03; each fold trains on
everything before the origin and predicts the next seven days, one day at a
time, reading only what was known the evening before. That gives about 16
folds inside a 143-day window. Every model reports, per fold and in total:

- its error and the error of the input it replaces, side by side
- the planted effects it recovered, with the value planted beside each one
- the unplanted effects it was offered, and what it found for them
- interval coverage, where the model gives an interval

The loader drops planted-truth columns before any model sees a row. The
harness asserts this, so reading the answer fails the build rather than
flattering the backtest.

### The planted truth, and where it lives

| Model | Trained on | Planted by the generator | Must come back | Must not come back |
|---|---|---|---|---|
| Headcount | `ec_attendance.csv` | Sunday ×0.62, Lebaran fortnight ×0.72, rain ≥25 mm ×0.93, noise sd 6%, normalised to `ec_labour.csv` monthly rates | Sunday and Lebaran ratios within ±0.05; heavy rain negative | A pay-cycle effect (none planted) |
| Slippage | The five order ledgers | Harvest: −1.2% per mm above 10 mm (floor 0.35), roads fair ×0.97, poor ×0.91 (×0.85 at ≥25 mm), impassable-when-wet ×0.86 (×0.65 at ≥15 mm), attendance ratio, latent block field ×(1+0.04q), log-noise sd 0.12. Upkeep: −1.0% per mm above 8 mm, the same roads, ×(1+0.05q), sd 0.22 | Rain slopes within ±0.003 per mm; the road-by-rain interaction; learned block effect correlates with q at ≥0.5 | A crew effect beyond attendance (none planted in adherence) |
| Rates | Order ledgers, `ec_harvester_day.csv` | Upkeep, spray and pest crews: `RATES × skill`, skill sd 0.08 clamped 0.8–1.2, written to `ec_crews.csv`. Harvest: slope and age through crew size, worker skill sd 0.13 sampled per day | Learned crew rate over literature rate correlates with `skill` at ≥0.7 | A persistent harvest block effect beyond the productivity target |
| Rain | Open-Meteo forecast and archive | Nothing. Real on both sides | | |

`skill` sits in `ec_crews.csv` in plain sight. It is the answer key: the
recovery test reads it, and the model loader never does.

---

## 2. Headcount

**Question.** How many of G1-03's twenty-four will turn up tomorrow, and how
sure is that?

**Model.** A binomial GLM per crew-day: present out of on-roll, logit link,
fitted by IRLS in numpy.

| Feature | Known the evening before because |
|---|---|
| Crew intercept, ridge-pooled toward its division | Fitted on prior days |
| Day of week | Calendar |
| Holiday: inside a holiday window, days since its end | Calendar (Indonesian public holidays and cuti bersama, 2025 table) |
| Days since payday | Register, `payday_day_of_month` |
| Probability of rain ≥25 mm | Rain model, section 5 |
| Crew's trailing 14-day rate, as a deviation from its intercept | Prior days |

**Output.** Expected present, with an 80% interval from the binomial with an
overdispersion factor fitted on residuals. The plan's crew table shows
`21 (18–23)`.

**Bar.** Lower mean absolute error in men per crew-day than the trailing mean,
overall and separately on Sundays, the Lebaran fortnight and the week after
it. The 80% interval covers between 75% and 85% of held-out crew-days.

**What it cannot know yet.** There is one Lebaran in the window. Folds whose
origin falls before 24 March have never seen a holiday, so the holiday effect
cannot be learned out of sample. Until a fold has seen one, the holiday factor
comes from the register (`holiday_attendance_factor`); once it has, the fitted
value is shown marked "fitted from one occurrence". Two years of real
attendance removes the caveat.

**Real data that makes it yours.** EPMS `t_attendance` at employee-day grain
and `m_gang_employee`, two years, so more than one Lebaran is in the target.

---

## 3. Slippage

**Question.** Of what G1-03 is planned to cut on blocks 14 to 19 tomorrow, how
much gets done, and which of those blocks is most likely to be carried?

**Model.** Two parts per order, both on features known the evening before:

1. **Will it be worked at all?** Logistic regression for `weathered_off` or
   `not_started`.
2. **If worked, what share?** Linear regression on log adherence
   (`actual_qty / planned_qty`, clipped to 0–1.15). Log because the generator
   and the field both multiply: rain, road and turnout compound, they do not
   add.

| Feature | Notes |
|---|---|
| Rain on the day, hinged at 5, 10 and 25 mm | Trained on recorded rain; served on the rain model's distribution |
| Road condition, one-hot, and road × rain ≥15 mm, road × rain ≥25 mm | `impassable-when-wet` is the case a mandor already knows about |
| Present over expected | Trained on recorded; served on the headcount model's distribution |
| Operation and activity | |
| Block's trailing adherence, pooled toward the operation mean | Strictly before the date |
| Crew's trailing adherence, pooled | Offered so the backtest can show it adds nothing |
| Days the job has already been carried | From `carried_to` chains |
| Day of week | |

**Training on the day, serving on the evening before.** The model learns what
rain and turnout **did** from recorded values, which is where the effect is
measurable. At plan time those values do not exist, so the plan integrates
over them: 200 draws of tomorrow's rain from section 5 and of each crew's
turnout from section 2, the order's adherence evaluated on each draw, then
averaged. That gives the expected share done and a 10–90% band.

**Bar.** Lower mean absolute error on order adherence than the rain-bucket
mean rebuilt to read only prior days, and lower error on the estate's daily
actual quantity. Calibration of "not worked" within 5 points per decile.

**What changes in the plan.** The scheduler's value density becomes deferral
value times expected share done, behind the register switch
`plan_on_expected_adherence`. On a likely-wet day the impassable-when-wet
blocks fall down the ranking and gangs go to good roads first. The Ranking
section already shows the arithmetic per block; it gains the expected-done
factor and its drivers.

**Real data that makes it yours.** The plan side of EPMS work records, which
the EC export does not carry, and a road-condition survey by block.

---

## 4. Rates

**Question.** What can this crew actually do per man-day on this block, now
that the ledger holds weeks of what it did?

**Model.** Empirical Bayes shrinkage. No fitting library, one formula a
supervisor can check:

```
posterior = (k × prior + man_days × observed) / (k + man_days)
```

| Operation | Unit of learning | Prior |
|---|---|---|
| Harvest | Block | `productivity.block_targets` |
| Prune, weed, path upkeep, spray | Crew × activity | The register's literature rate |
| Pest census and treatment | Crew × activity | The register's literature rate |

`k` is the prior's weight in man-days, `rate_prior_man_days` in the register,
chosen by backtest and shown with the curve that chose it.

**Two traps the observed rate must avoid.**

1. **Censoring.** A crew that finished its planned quantity stopped because
   the work ran out, not the day. Its rate is a lower bound. Completed orders
   are excluded from the observation and the count excluded is shown. The
   backtest compares bias with and without them; a censored likelihood
   replaces the exclusion only if the exclusion measurably biases the rate.
2. **Double counting weather.** The plan's expected output is
   present × rate × expected share done. Rain belongs to slippage. A rate
   learned from wet days charges the rain twice, and the plan turns
   pessimistic in a way a mandor will spot at once. So the observed rate is
   divided by the slippage model's condition factor for that day: the rate
   learned is the dry-day, good-road rate.

**Guardrails.** A rate moves at most `rate_max_weekly_change_pct` (default
10%) per week. `use_learned_rates` switches back to the prior. Every rate
shows prior, observed man-days, posterior and its last four weekly values.

**Where Did-it-work comes in.** Each accepted plan read back against the
ledger adds its orders as observations labelled "from an accepted plan". The
Plans section shows what each one moved: "circle weeding, U2-04: 1.10 to
1.18 ha per man-day after 3 plans, 41 man-days".

**Bar.** Lower held-out error on next week's per-order man-day rate than the
prior, for upkeep, spray and pest crews. For harvest: no worse than the
productivity target, and at least 90% of blocks within ±5% of it, since no
persistent block effect was planted.

**Real data that makes it yours.** The same work records with man-days, and
EPMS `t_oph` at employee grain for harvest.

---

## 5. Rain

The only one of the four that is real end to end.

**Question.** What is the chance that tomorrow's rain washes off the spray, or
stops work?

**Data.** `gis/build_rain_forecast.py` pulls hourly
`precipitation_previous_day1` and `precipitation_previous_day2` from Open-Meteo's
Previous Runs API at the estate point (−7.02361, 140.86947, local time
Asia/Jayapura), summed to local days, into
`gis/data/ec_rain_forecast.json`, provenance `real:open-meteo`. Verified
complete for January to May 2025. Step one of the phase finds where the series
starts, because every earlier year adds training days.

**Truth.** The Open-Meteo archive that `ec_rainfall.json` already holds. That
is ERA5 reanalysis, itself a model, and the Rain section says so. The estate's
own rain gauges are the real ground truth.

**Model.** Logistic regression per threshold that matters: ≥15 mm (spray
washes off), ≥25 mm, ≥45 mm (`rain_cutoff_mm`, work stops).

| Feature | Source |
|---|---|
| log(1 + day-1 forecast mm) | Previous Runs |
| Day-2 forecast, and the spread between day 1 and day 2 | Previous Runs; a forecast that keeps changing is less sure |
| Climatological frequency of the threshold for the calendar month | Six years of archive |

It deliberately does not use yesterday's recorded rain: the archive trails
real time by six days, so live it would not exist. With a gauge feed it would
become a feature.

**Bar.** Brier skill score above zero against climatology, on held-out folds.
If the calibrated forecast cannot beat climatology — possible, given a
correlation of 0.29 — the go/no-go runs on climatology and the section says
that too. A month-aware probability is still better than "unknown".

**The go/no-go becomes a cost decision.** Spray tomorrow if

```
P(≥15 mm) < C_defer / (C_defer + C_waste)
```

where `C_defer` is `scheduler.cost_of_deferral('spray', blocks, 1)` (already
built) and `C_waste` is herbicide plus man-days on the planned hectares, with
herbicide from a new register assumption `herbicide_idr_per_ha`. The Why
section prints the two costs, the threshold and tomorrow's probability. A
typed `rain_mm` on the plan still overrides everything.

**Replay.** A replayed date reads the forecast issued the evening before.
Recorded rain is shown beside it as "what fell", and Did-it-work compares the
two.

**Real data that makes it yours.** The estate's daily rain-gauge book by
division.

---

## 6. Wiring

### The chain

```
rain (5) ──► headcount (2) ──► slippage (3) ──► expected done per order
   └────────────────────────────►┘                    │
rates (4), dry-day ─────────────────────────────────► expected qty = present × rate × share done
```

`GET`/`POST /gis/ops/{op}/plan` gains an `inputs` block. Each of the four
inputs names its source ("model" or "fallback") and, for a fallback, why: the
switch is off, the bar failed, or there is not enough history.

### Endpoints

| Endpoint | Returns |
|---|---|
| `GET /gis/ops/forecast/headcount?date=&crew=` | Expected present and interval per crew, drivers, basis |
| `GET /gis/ops/{op}/slippage?date=` | Expected share done per planned order, its band, effects |
| `GET /gis/ops/{op}/rates` | Prior, observed, posterior and weekly history per block or crew |
| `GET /gis/ops/weather?date=` | Threshold probabilities, raw forecast, recorded rain on a replay, skill |
| `GET /gis/models/backtest?model=` | Folds, error against the replaced input, planted against recovered |

### The windows: a Models group

Each operation window gets a fourth menu group, **Models**, beside Tomorrow,
Ledger and Why, with sections Headcount, Slippage, Rates and Rain. Each section
has four parts:

- tomorrow's prediction
- the backtest against the input it replaced
- planted against recovered
- the extract that would make it real

Items show a warn count when the model is running on its fallback.

Changes to existing sections:

| Section | Change |
|---|---|
| Tomorrow › plan crew table | Headcount with interval, expected share done, and `predicted` badges |
| Why › ranking | The expected-done factor per block |
| Why › objective | The spray cost threshold |
| Did-it-work | A new Forecasts section: headcount, share done and rain predicted against recorded, for accepted and replayed plans |

### Assumption register: a Models group

| Key | Default | Kind |
|---|---|---|
| `use_headcount_model` | auto | on only when the backtest bar passed |
| `use_slippage_model` | auto | as above |
| `use_learned_rates` | auto | as above |
| `use_rain_model` | auto | as above |
| `plan_on_expected_adherence` | on | assumed |
| `rate_prior_man_days` | 20, then backtest-chosen | derived |
| `rate_max_weekly_change_pct` | 10 | assumed |
| `holiday_attendance_factor` | 0.72 until fitted | assumed, flagged |
| `payday_day_of_month` | 25 | assumed |
| `herbicide_idr_per_ha` | to be set with the client | assumed |

### Copilot

Five tools: `headcount_forecast`, `slippage_risk`, `crew_rate`,
`rain_outlook`, `model_backtest`. The rule does not change: the model never
computes. Each tool returns the figure, its interval, its source and whether
it is a fallback.

### Tests

`test/models.mjs`:

- every endpoint returns 200
- each backtest reports model and baseline error
- planted effects recovered within tolerance, unplanted ones not
- switching a model off makes the plan's `inputs` say fallback
- a replayed spray day decides on the forecast, not the recorded rain

The smoke walk picks up the new window sections without changes.

---

## 7. Order of work

| Phase | Work | Why here |
|---|---|---|
| 1 | `backtest.py`, the `predicted` badge, the Models register group, the loader that drops planted columns, the prior-days-only rebuild of the rain-bucket baseline | Every model is judged by it. Without it there is no bar, only a claim. |
| 2 | Rain: the pull, the calibration, the cost-threshold go/no-go, the replay fix | Real data, depends on nothing, and fixes a hindsight error already on screen. |
| 3 | Headcount | Depends only on 2. The turnout forecast slippage needs to serve. |
| 4 | Slippage, and the scheduler's expected-done value | Needs 2 and 3 to serve. Defines the weather factor that rates must divide out. |
| 5 | Rates, and the Did-it-work read-back into them | Needs 4, or rain is counted twice. |
| 6 | The chain in the plan, the Forecasts section, copilot tools, `test/models.mjs` | Needs all four in place to wire them together. |

Each of phases 2–5 lands with its endpoint and its Models section, so every
model is visible and backtested before the plan relies on it.

---

## 8. Three things to get right

1. **Only what was known the evening before.** In features, in baselines and
   in replay. Replay is already wrong about this for rain. A model that trains
   on recorded rain and is scored on recorded rain will look excellent and be
   useless at six in the morning.

2. **Recovery is not discovery.** On generated feeds the honest claim is that
   the machinery works: it finds what was planted, invents nothing that was
   not, and never reads the answer key sitting in `ec_crews.csv`. The badge
   says `trained on synthetic` until the extract arrives, and the Models
   section names that extract.

3. **Each factor counted once.** Turnout lives in headcount, weather in
   slippage, capability in rates. Expected output is the product of the
   three. Let any two overlap and the plan drifts pessimistic or optimistic
   in a way the estate manager will catch before any backtest does.
