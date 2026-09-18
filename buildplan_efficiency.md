# Estate Command: from catalogue to operating rhythm

Plan for adding the five core plantation operations as first-class menus, each
carrying the ledger of what was done and the assignment for tomorrow.

Written against the build as it stands on 2026-09-14: 291 real EC block
polygons, real harvest 2025-01-01 to 2025-05-23, real Sentinel-2 NDRE, real
Copernicus terrain, real Open-Meteo rainfall, sixteen synthetic CSV feeds, 34
panels and a 14-tool copilot.

---

## Build status

All seven phases built on 2026-09-14. **35 of 43 features live**, 41 panels,
a 29-tool copilot. Every endpoint returns 200, the smoke test walks every
panel without a console error, and `test/ops.mjs` edits a plan, re-runs it,
drafts the assignment and reads it back.

| Phase | State | Landed as |
|---|---|---|
| 1 Supply side | done | `ec_crews.csv` (67 crews: 28 gangs carried through under their gang codes, 23 upkeep crews, 7 spray teams, 7 P&D teams), `ec_attendance.csv` (9,581 crew-days) |
| 2 Harvest ledger | done | `ec_harvest_orders.csv`, 7,656 orders; `gis/ops.py`; `/gis/ops/harvest/ledger`; the Ledger tab |
| 3 Assumption register | done | `gis/assumptions.py`, 22 assumptions in six groups, overrides in `decisions.db`; the Governance panel; `/gis/assumptions` |
| 4 Scheduler | done | `gis/models/scheduler.py`; Tomorrow and Why tabs; `GET`/`POST /gis/ops/{op}/plan` |
| 5 Replication | done | `ec_upkeep_orders.csv` (3,662: prune, circle weed, path, spray), `ec_pest_orders.csv` (632: census, treatment, follow-up); three more menus on the same renderer |
| 6 Transport | done | `ec_dispatch_orders.csv`, 7,278 orders, the plan side of the trip ledger; the dispatch plan chains off the harvest plan |
| 7 The loop | done | Four columns on the decision log, four assignment artifact kinds, the Did-it-work panel, five copilot tools |

### The calibration constraints, measured

| Constraint | Result |
|---|---|
| Harvest actuals reconcile to the real bunch counts | 8,182,591 in the ledger, 8,182,591 in the export. Exact. |
| Attendance reproduces `ec_labour.csv` | Every division-month within 0.01% of the labour feed's rate, Lebaran dip included |
| Actuals do not match plan | Harvest 78.0% bunch-weighted; prune 73.8%, weed 78.5%, spray 52.1%, pest 78.5%, dispatch 81.0% |
| Misses correlate with real drivers | Harvest adherence 87% on dry days, 81% at 5-25 mm, 59% at 25-45 mm, 35% over 45 mm, on real Open-Meteo daily rain |
| Slippage chains | 4,467 orders carried forward; upkeep chains run 2 to 12 days; 266 of 1,164 block-activities overdue at the anchor |

### The plan for 2025-05-24

> Saturday 24 May: 28 gangs on shift, 595 present, 56 blocks, 1,208 ha,
> 460.7 t. Not reached: 128 blocks, deferring costs 394.2M IDR this week.

Crew capacity binds: 400 cutter-days against 1,648 needed for everything
due. Contiguity costs 11% of deferral value and turns 4 adjacent pairs into
44. The greedy plan sits 14.7% under the relaxed bound, and the Why tab says
why: the bound ignores contiguity, travel and whole blocks.

### Corrections to this plan, found by building it

- **The daily rainfall did not exist.** `ec_rainfall.json` carried monthly
  totals only. `build_rainfall.py` now keeps the daily series and the archive
  was re-pulled; the fallback of spreading a month's real total across its
  real rain-day count is kept and named in the note if it is ever used.
- **The existing feeds disagreed about when a block was cut.** The trip
  ledger and the worker-day feed each drew their harvest days from the
  running generator, so the same block was cut on different days in each,
  and both spread May's cut over 31 days of a month the export records 23 of.
  `_trip_days` is now seeded per block and month, phased per block, and
  clamped to the export's last day; every feed uses the same days, and
  `ec_rotation.csv` is read back from them rather than drawn.
- **The rotation target is the block's own round.** Median realised interval
  is 5.2 days in January and 7.7 in May in the client's own day counts. The
  target is now the interval each block held over January-February, so "days
  past the round" at the anchor measures the stretch the export already shows.
- **The worker-day feed did not sum to the bunch.** Integer truncation in the
  per-worker split lost 0.4% of the crop. Largest-remainder rounding; exact now.
- **Spray teams sized as if every day were dry were 78% overdue by May.**
  Southern Papua's wet season takes a third of the sprayable days. Teams are
  now sized against the sprayable fraction of the real daily series, with
  slack, and the rain-out rows are what remain.
- **Pruning is a multi-day job.** A crew's first block of the day may be one
  it merely starts; without that rule eleven of twenty-three upkeep crews
  stood idle because no whole block fitted a sixteen-man day.
- **Vehicles run more loads than turnaround implies.** The trip ledger shows
  eight loads a day on busy days against four from working hours over mean
  turnaround. Capacity takes the larger, and says which.
- **Gang territories were interleaved.** The old round-robin gave every gang
  a scattered set of blocks; a scheduler that rewards contiguity has to start
  from a roster that has it. Territories are contiguous runs now.
- **Contiguity has to seed from somewhere.** The bonus also applies to a
  block adjacent to the crew's home block, or the first pick of the day is a
  pure ranking and the second cannot be adjacent to it.

### What replay means for "did it work"

A plan can be run for any date inside the ledger. The outcomes panel then
reads what the ledger recorded on the planned blocks that day, and a planned
block the estate did not work counts as zero. Replaying 2025-05-12 gives
26% realisation: the scheduler's choice of blocks and the estate's differ,
which is exactly the comparison the panel exists to make, and the reading is
stated on the panel rather than smoothed over. A plan accepted for
2025-05-24 stays pending until a ledger extract covers it.

---

## 0. What changes

[buildplan_operations.md](buildplan_operations.md) §0 states the current
objective plainly: *"Not an operations tool. A feature catalogue that runs."*
That objective was correct for winning the data extract and it has been met —
28 of 36 features live, every one carrying its ask.

This plan changes the objective. The app stops answering *what is the state of
the estate* and starts answering *what does each crew do tomorrow, and did
yesterday's plan work*. The catalogue survives underneath as the evidence layer.

The sentence this build is engineered to support:

> "Division 1 has four gangs on shift tomorrow. G1-01, 24 men present, takes
>  blocks 14 to 19 — 62 ha, six days past the round, an estimated 19.8 tonnes.
>  Here is the same plan for the last 140 days, what was actually cut against
>  it, and the 8.4 tonnes a week the estate loses by cutting in the order it
>  currently does."

### The gap this plan closes

The build has demand signals on every domain and no supply side at all.

| Exists | Missing |
|---|---|
| `ec_gangs.csv` — 30 gangs, headcount, harvesters, kerani, mandor | Nothing reads it. No feed consumes a crew. |
| `ec_upkeep.csv` — days overdue per block per activity | State, not events. No who, no when, no how long. |
| `ec_harvester_day.csv` — 59,161 worker-days of output | Output with no plan behind it. Nothing was assigned. |
| `ec_weighbridge.csv` — 19,966 executed trips | Execution with no dispatch plan it was executed against. |
| `ec_pest_treatment.csv` — 238 treatments with follow-up dates | Events with no crew and no scheduling. |
| `ec_rotation.csv` — gang per block, as at 2025-05-23 | One frozen snapshot. No history, never recomputed. |

Every panel ranks blocks independently and stops. The manager is the
integration layer, and the integration is the work.

### The anchor date

The real harvest export ends **2025-05-23**. So "tomorrow" in this build is
**2025-05-24**: the first day beyond the client's own data. The ledger covers
the window they can verify line by line against EPMS; the plan begins exactly
where their records stop. That boundary is worth naming on screen, because it
is the difference between a demo and a claim.

---

## 1. The one new object: the work order

Five operations, one row shape. This is the whole architecture.

```
order_id            WO-2025-0512-H-031
date                2025-05-12          the day it was worked
operation           harvest | prune | weed | spray | pest | dispatch
division_code       1
block_code          14
crew_code           G1-01
headcount_plan      26
headcount_actual    24
planned_qty         58.0                unit depends on operation
actual_qty          51.2
unit                ha | bunches | palms | tonnes
man_days_plan       26.0
man_days_actual     24.0
status              completed | partial | not_started | weathered_off
started / finished  07:10 / 14:55
carried_to          WO-2025-0513-H-047  what a partial became
```

`planned_qty` and `actual_qty` in one row is the point. Every efficiency number
in this plan is a ratio of those two columns, grouped differently:

- **adherence** — actual ÷ planned, per crew, per block, per operation
- **productivity** — actual ÷ man_days_actual, which is the quota question
- **slippage** — `carried_to` chains, which is where a round quietly stretches
  from 10 days to 16 without anyone deciding it should

None of those exist today at any grain.

### Four files, drawn on the real extract boundary

Per the convention in [gis/build_synthetic.py](gis/build_synthetic.py): one CSV
per eventual real extract, `# SYNTHETIC.` header naming the table it stands in
for, every row joined to a real block. The swap to real data stays a file
replacement.

| File | Stands in for | Covers |
|---|---|---|
| `ec_harvest_orders.csv` | EPMS `t_harvesting_plan` / `t_harvester_assignment` | harvest |
| `ec_upkeep_orders.csv` | EPMS `t_workplan` / `t_work_assignment` | prune, circle weed, path upkeep, spray |
| `ec_pest_orders.csv` | **nothing — no EPMS table exists** | census rounds, treatment, follow-up |
| `ec_dispatch_orders.csv` | SAP PM vehicle trip log, plan side | transport |

`ec_pest_orders.csv` having no counterpart is not a defect to hide. It is the
strongest single line in the data request: the client cannot schedule pest work
today because there is nowhere to record that they did.

### Two supply feeds

| File | Carries | Why |
|---|---|---|
| `ec_crews.csv` | crew_code, crew_type, division, establishment, skill, home base | `ec_gangs.csv` extended past harvest. Keeps `gang_code` as the id so the existing `ec_rotation.csv` join survives untouched. |
| `ec_attendance.csv` | crew, date, on_roll, present, absent_reason | Tomorrow's plan is capped by who turns up. `ec_labour.csv` only has attendance_rate at division × month, which cannot schedule a Tuesday. |

**Calibration constraint, non-negotiable.** `ec_attendance.csv` must reproduce
the division-month `attendance_rate` already in `ec_labour.csv`, including its
Lebaran dip in Mar–Apr, and `ec_harvest_orders.csv` `actual_qty` must sum back
to the real block-month bunch counts. Two feeds that contradict each other are
worse than one feed missing, and the client will check the one they can verify.

---

## 2. The five menus

Same panel component, five instances. One renderer parameterised by operation,
not five renderers.

| Menu | Demand signal (exists today) | Crew | Unit | Rate driver |
|---|---|---|---|---|
| **Harvesting** | ripeness pressure, `/gis/rotation` | harvest gang | bunches | adjusted target from [gis/models/productivity.py](gis/models/productivity.py), 97–187 |
| **Pruning** | `days_overdue`, 240-day round | upkeep crew | palms | palm age, frond load |
| **Weeding** | `days_overdue`, 75-day circle / 110-day path | upkeep crew | ha | ground cover, road access |
| **Pest control** | `followup_due`, `days_overdue`, spread risk | spray team | palms | census incidence, method |
| **Transport** | tonnage at collection points | vehicle + driver | tonnes | turnaround, road condition |

### Three tabs on every menu

**Tab 1 — Ledger.** What was worked, when, by whom, planned against actual.
Filterable by crew, block, division, date. This is the table you asked for, and
it is also the training set for everything in tab 2.

Columns that earn their place: date, crew, blocks, planned, actual, adherence,
man-days, output per man-day, status. Footer carries the aggregate: *"140 days,
1,847 orders, 78.4% adherence, 61 carried forward."*

**Tab 2 — Tomorrow.** The assignment. Editable before it becomes an artifact.

```
Tomorrow, Saturday 24 May 2025          Division 1        4 gangs, 96 present

G1-01   24/28 present   blocks 14-19    62.4 ha   6 d over round   ~1,180 bunches
G1-02   26/28 present   blocks 31-36    58.1 ha   4 d over round   ~1,240 bunches
G1-03   21/26 present   blocks 77-80    41.0 ha   9 d over round     ~880 bunches
G1-04   25/30 present   blocks 102-108  70.2 ha   2 d over round   ~1,310 bunches

Not reached: 11 blocks, 128 ha, deferring costs ~2.1 t this week
```

**Tab 3 — Why.** The demand ranking behind the assignment, the cost of
deferring each block another day, and the constraint that bound. A plan nobody
can interrogate is a plan a mandor overrides on the first wet morning.

### Where they live

Five new domains in `DOMAINS` in [gis/features.py](gis/features.py)? No —
**keep the six domains and add the menus inside them.** Harvesting, Upkeep and
Pest already exist and already carry the evidence panels. A parallel taxonomy
would split the rail against itself. Pruning and weeding are two panels inside
Upkeep; harvest scheduling sits beside rotation inside Harvesting.

The rail in [command_static/src/shell/rail.js](command_static/src/shell/rail.js)
needs no change. `PANEL_RENDER` in
[command_static/src/panels/registry.js](command_static/src/panels/registry.js)
gains five entries pointing at one parameterised renderer.

---

## 3. The scheduler

New `gis/models/scheduler.py`. Fitted offline where fitting is needed, cached,
**never fitted inside a request** — same rule as the other four models.

### Inputs

```
demand[]     block, operation, urgency, value_at_risk_per_day, area, palms
capacity[]   crew, date, present, rate_per_man_day, division, skill
geometry     block centroids, already in the layer stack
constraints  rain, road condition, blackout dates, follow-up windows
```

### The objective

Maximise value recovered per man-day, subject to capacity. Four terms, each
traceable to a number the app already computes:

| Term | Source |
|---|---|
| Value recovered | tonnes at risk × `FFB_PRICE_IDR_KG`, both in `build_synthetic.py` |
| Deferral decay | ripeness pressure for harvest; overdue curve for upkeep |
| Travel penalty | block centroid distance from crew base |
| Contiguity bonus | rewards a crew working adjacent blocks |

**Contiguity is not a nicety.** A plan that sends G1-03 to blocks 12, 87 and
203 is arithmetically optimal and operationally void — nobody executes it. The
contiguity term is what produces "blocks 14–19" instead of a scattered list,
and it is the difference between output a mandor follows and output a mandor
ignores. Weight it high enough to see, and show what it costs in the Why tab.

### The method

Greedy by value density, then a swap-improvement pass. Not an LP.

The reason is not that an LP is hard — it is that the output has to be defended
line by line to a mandor who disagrees with it, and "this block scored higher
than that one, here is the arithmetic" survives that conversation where a
simplex tableau does not. [gis/layers.py:872](gis/layers.py#L872) already does
this shape for vendor allocation against capacity; generalise it rather than
adding a solver dependency.

Record the gap to a relaxed upper bound so the greedy choice is accountable. If
it is within a few percent, say so and stop.

### Endpoints

Following the existing split — deterministic endpoints never call a model and
never fail; generated ones return `{"available": false, "reason"}`.

```
GET  /gis/ops/{operation}/ledger      history, filterable
GET  /gis/ops/{operation}/plan        tomorrow's assignment
POST /gis/ops/{operation}/plan        re-run with edited constraints
GET  /gis/ops/{operation}/adherence   planned vs actual, by crew and block
GET  /gis/ops/capacity                crews, attendance, effective man-days
```

---

## 4. Closing the loop

The order ledger *is* the loop. An order carries what was planned and what was
done, so adherence is measured rather than assumed, and after a season the rate
coefficients in §3 stop being assumptions and become fitted.

Two small changes make it explicit.

**The decision log gains four columns.**
[gis/decisions.py:104](gis/decisions.py#L104) currently ends at `epms_table` —
the system proposes, a human accepts, and nothing ever checks whether it worked:

```
due_date          when the accepted action should have happened
expected_effect   quantity and band, from the scheduler
order_ref         the work orders this decision generated
observed_effect   read back from the ledger once the date passes
```

**One new Governance panel: Did it work.** *"43 plans accepted, 31 executed,
expected +18.2 t, observed +14.6 t, 80% realisation."* This is the only thing in
the build that improves with use, and the only evidence the tool did anything.
It is also the answer to the renewal question.

---

## 5. Fabricating the data

Zero client data, so all six new feeds are generated. Follow the existing
conventions exactly — one seed, `# SYNTHETIC.` header, every row joined to a
real block, and **extend `_latent_field`, never draw independently**. That
smooth spatially-autocorrelated surface anchored to each block's real yield is
why the existing synthetic layers corroborate the real harvest instead of
contradicting it.

Four rules specific to an operations ledger, each of which will be got wrong on
the first pass:

1. **Actuals must not match plan.** A ledger where `actual_qty == planned_qty`
   is decoration. Target ~78% mean adherence on harvest, wider spread on
   upkeep, with the misses correlated to real drivers: rainfall on the day
   (real, from `ec_rainfall.json`), road condition (`ec_roads.csv`), and crew
   attendance. A miss that correlates with nothing teaches the panel nothing.

2. **Harvest orders must reconcile to the real bunch counts.** Generated down
   from the real block-month totals, exactly as `gen_harvester_days` already
   does. The client can check this column against EPMS, and it is the one they
   will check.

3. **Attendance must reproduce `ec_labour.csv`.** Including the Lebaran dip.

4. **Slippage must chain.** A partial order carries forward, the carried block
   appears in a later order, and the round visibly stretches. That chain is the
   finding — it is how a 10-day round becomes 16 without anyone deciding it
   should — and it only exists if the generator writes it.

### One assumption register

Every rupiah and tonne figure in tab 2 rests on an assumption: FFB price, loss
per day past the round, pruning rate in palms per man-day, spray coverage per
tank. Do not bury them in module constants.

Build `gis/assumptions.py` in the same shape as
[gis/readiness.py](gis/readiness.py) — value, unit, source (`agronomy
literature` / `client-supplied` / `assumed`), and every derived figure on screen
traceable to one. Surface it as a panel and let the client edit the values live.

This is what keeps `reasoning.audit_figures` working once the app starts quoting
money, and it converts "you invented a number" into "here is the one number we
need you to agree with us on", which is a far stronger position.

---

## 6. The copilot

Keep the rule that holds the whole thing together: **the model never computes.**
Add tools that return plans computed server-side.

| Tool | Returns |
|---|---|
| `ops_plan(operation, date)` | tomorrow's assignment, every figure pre-computed |
| `ops_ledger(operation, crew, from, to)` | the history slice, aggregates included |
| `crew_capacity(date)` | who is available and what they can do |
| `cost_of_deferral(block, operation, days)` | what waiting costs |
| `replan(constraint, value)` | re-run with a crew sick or a road closed |

The figure-audit constraint applies unchanged: a tool must return every number
its narrative will quote. A tool that returns block assignments but not the
hectares will have its narrative flagged unverified.

This is where the app becomes worth talking to. *"G1-03 is four men short
tomorrow, replan"* is a question the current 14 read-only tools cannot touch.

---

## 7. Order of work

| Phase | Work | Why here |
|---|---|---|
| 1 | `ec_crews.csv`, `ec_attendance.csv` | The supply side does not exist. Nothing else can be built without it. |
| 2 | `ec_harvest_orders.csv` + ledger endpoint + ledger tab | One operation end to end. Proves the row shape before it is copied four times. |
| 3 | `gis/assumptions.py` and its panel | Must land before the first rupiah figure, not after. |
| 4 | `scheduler.py` on harvest + Tomorrow and Why tabs | The demo moment. Contiguity and the deferral cost are both here. |
| 5 | `ec_upkeep_orders.csv`, `ec_pest_orders.csv` — prune, weed, spray, pest | Same component, four more instances. Should be days, not weeks. |
| 6 | `ec_dispatch_orders.csv` — transport | Last because `ec_weighbridge.csv` already carries the execution side; this adds the plan it was executed against. |
| 7 | Decision-log columns, Did-it-work panel, copilot tools | Needs a ledger with history in it to read back from. |

Phases 1–4 are the plan. 5 is replication. 6–7 are what make it a system rather
than five tables.

---

## 8. Three things to get right

1. **Tomorrow's plan must be executable, not optimal.** Contiguous blocks, one
   crew, a realistic day. The moment an estate manager says "we would never do
   that", every other number on the screen is in question too.

2. **The ledger must show the estate losing.** 78% adherence, 61 orders carried
   forward, rounds stretching under rain. A generated history where everything
   went to plan proves the app can draw a table. One that shows the slippage the
   client recognises from their own estate proves it can find it.

3. **Never let a scheduled figure wear a real badge.** The provenance discipline
   that runs through the build now has to carry money and tonnes. The badge is
   how the client tells what they already own from what they would be
   commissioning, and a single mislabelled figure in tab 2 puts the whole plan
   in doubt.
