# Estate Command: stores, lead times and safety stock

Plan for a stores capability built on SAP MM records: how much of each input
the estate should hold, when to reorder it, and how long suppliers really take.

Written against the build as it stands on 2026-09-15: the four forecasting
models live, a 34-tool copilot, the assumption register, the decisions log,
and one stock table in the Nutrition panel that divides the annual programme
by 365 and compares it with a random lead time.

Scope agreed on 2026-09-15: **safety stock and SAP MM records only.** No SAP
SD, no external shipping, port or social data, no DeepAR. Gaps are filled with
synthetic data shaped like the SAP extract that would replace it.

---

## Build status

All six phases built on 2026-09-15. A **Stores** window in the Commercial
domain (47 features, 39 live), three copilot tools (37 in all), a `stores`
group of twelve register keys, two artifact kinds, and a Nutrition panel that
shows the store's order-by date beside its old flag. `test/stores.mjs`,
`test/smoke.mjs` (43 panels, 113 window sections), `test/models.mjs` and
`test/ops.mjs` pass. 25 of the 27 existing feeds are byte-identical to before
the build; the other two are the planned `ec_fertiliser_stock.csv` rewrite and
the manifest.

| Part | Landed as | Held-out result | Grade |
|---|---|---|---|
| MM records | `gis/build_materials.py` → six `ec_mm_*.csv`; 17 material master rows, 8 suppliers, 620 PO lines (71 rush), 12,689 movements, 5,820 reservations | Stock equals movements; fertiliser issues match `ec_fertiliser.csv` to the kilogram; 173 parts against 173 PM orders; nothing below zero | — |
| Lead time | `gis/models/leadtime.py` | Usual time off by 4.8 days against 10.5 for the quote, on 209 orders placed after each monthly cutoff (54.5% better); the one-in-ten line held on 90.4% | Reliable |
| Consumption | `gis/models/consumption.py` | Over each material's lead time: fertiliser 96–98% closer than SAP's trailing average, glyphosate 24%, parts 3–19% (ahead of Croston-SBA on seven of eight); metsulfuron −5.5%, rat bait −2.2% and diesel +0.9% are not better, and say so | Reliable overall |
| Safety stock | `gis/models/safety_stock.py`, `gis/stores.py` | The replay of SAP's settings reproduced 30 of 30 recorded rush buys. Learned reorder points win on 11 of 17 materials: Rp 1.38 bn against Rp 1.50 bn (7.6% less). Agrochemicals go from 72% to 96% of cycles clear, 14 rush buys to 1 | Rough guide |

### The planted-truth checks, measured

| Check | Result |
|---|---|
| Real time over the quote, seven suppliers | 7 of 7 within 10%, e.g. Pupuk Kaltim 1.46 against 1.40 planted |
| Wet season, as a ratio to other months | Sea 1.19 against 1.25; road 1.39 against 1.40; no month April–November more than 7% off |
| Very late orders, against what each supplier's orders carry | 7 of 7 within 5 points; Pupuk Kaltim 18% against 18% |
| Split deliveries, order size, weekday | Recovered; no effect found where none was planted |
| Handling loss (glyphosate, metsulfuron) and storage loss (urea, kieserite) | 5 of 5 |
| Answer key | `delay_cause` is stripped by the loader and absent from every payload; the test asserts it |

### Order on 2025-05-24, in the words on screen

> 5 materials to order this week: Metsulfuron-methyl 20 WG (Order now: 39 kg),
> Clutch plate (Order now: 8 ea), Injector pump (Order by Wed 28 May: 8 ea)…
> NPK 12-12-17-2: Covered. Next order by 31 July. PT Pupuk Kaltim quotes 30
> days; an order placed then most likely takes 41, and 1 in 10 takes 66 or more.

### Corrections to this plan, found by building it

- **The supplier factor absorbed the season.** Month factors are only defined
  against something. Anchored on the median month, the supplier factor is its
  time in a typical month, and the season is checked as the ratio it was
  planted as.
- **"Very late" cannot be checked against the planted 12%.** 16 of Pupuk
  Kaltim's 81 orders actually missed a sailing, and the ordinary spread puts a
  few orders past the line at every supplier (3 of Petrokimia's 21). Each
  supplier is checked against what its own orders carry, the way headcount
  recovery reports what the data carries after the generator's adjustments.
- **Fertiliser's buffer took three attempts.** Applying weekly forecast errors
  to a whole round made it look half as big again and held Rp 1.45 bn of NPK
  against SAP's Rp 0.68 bn. Capping the supplier's part at one lot left the
  round-start spike to rush buys. What worked: the full lead-time draw (a
  safety lead time) with the spread of past rounds' totals, which is how
  uncertain a programme really is.
- **The replay needed its own receipts.** A replayed order copies the receipt
  schedule of the nearest recorded order on the lane, split deliveries
  included, so replaying SAP's settings reproduces the history exactly.
- **Served consumption is the backtested model.** The plan had diesel and
  spraying served on forward drivers the backtest never scored. The window
  serves what was scored and shows the forward harvest forecast's diesel beside
  it as a check (44,528 L against 47,449 L for the next 30 days).
- **Parts are per tonne hauled, not per 1,000 trips.** Trips exist only from
  2025; tonnes go back to 2022.
- **The reservations file holds every reservation**, closed ones included, as
  RESB does. Timing and fulfilment are learned from rounds more than 120 days
  past their target date, so a round still being issued cannot make every round
  look early.
- **Rush premium is learned, not a register key.** It is measured from the rush
  POs (30% fertiliser, 25% agrochemicals, 40% parts) and shown in the window.
- **The switch is per material.** A material uses the learned reorder point only
  where its own replay costs less at the service level; urea, borate,
  metsulfuron and three parts keep SAP's settings.
- **Fertiliser projects 150 days, not 90.** Ninety days shows a flat line; the
  October round is day 144.
- **The daily tonnes series had a hole.** 24–31 May 2025 read zero between the
  export's end and the forward forecast's June, and dragged the diesel
  cross-check 23% low until it carried May's daily rate.
- **Not run: the full regeneration.** `build_synthetic.build()` now calls
  `build_materials.build()` last, but the full rebuild was not run, because it
  rewrites every feed. `python gis/build_materials.py` on its own is what was
  run and verified.

---

## 0. What changes

The estate's stores hold what the palms cannot grow: fertiliser, herbicide,
pest chemicals, diesel and spare parts. Fresh fruit is not in scope; it has to
be milled within about two days and is never stocked.

| Today | Where it fails | After |
|---|---|---|
| Days of cover = annual programme ÷ 365 ([layers.py:1235](gis/layers.py#L1235)) | Fertiliser goes out in two rounds a year, so the store is nearly empty after a round and full before one. The average daily draw describes neither. | Projected stock day by day from open reservations, recorded use and open purchase orders |
| Lead time is one random number per material (21–75 days) | A quoted lead time is not a measurement. The spread matters more than the average, and the table has no spread at all. | A lead-time distribution per supplier, learned from purchase-order history |
| "Runs dry first" if cover < lead time | A status flag, not a policy: it never says how much to hold, when to order, or what holding it costs | Reorder point and safety stock per material at a service level from the register, with the cost of that choice |
| Four fertiliser materials | Diesel, chemicals and parts stop work too | 16 materials in four groups |

Nothing here writes to SAP. A reorder becomes a drafted purchase requisition,
and a wrong setting becomes a drafted master data change, both in the
decisions log beside every other artifact.

---

## 1. The rules this build follows

The five rules in [buildplan_ml.md](buildplan_ml.md) §1 apply unchanged: beat
the method replaced on held-out time or keep it; only what was known at the
time; recovery is not discovery; numpy on a whiteboard; a predicted figure
wears its own badge. Four more are specific to stores.

1. **The old way is SAP's own settings.** The baseline is not a straw man. It
   is the reorder point (MARC-MINBE), safety stock (MARC-EISBE) and planned
   delivery time (MARC-PLIFZ) the store runs on today, plus "order the round
   PLIFZ days before its target date" for fertiliser.
2. **Judge the policy on history, not on the model.** The headline test replays
   twelve months of recorded issues against both policies (§5). A model that
   grades its own simulation proves nothing.
3. **A purchase order still open is not a short lead time.** At any cutoff, the
   slowest orders are the ones not yet received. Dropping them makes suppliers
   look faster than they are, and the heavy-tail supplier looks fastest of all.
4. **Each factor counted once.** The wet season lengthens lead times and
   changes how much gets sprayed. Lead time owns the first, consumption the
   second, and the safety stock combines them once.

---

## 2. The SAP MM records, and what fills the gaps

The EC export carries no MM data ([readiness.py:212](gis/readiness.py#L212)).
Everything below is generated, shaped as the SAP extract that replaces it, so
the swap is a file replacement, as with every other synthetic feed.

### The files

All under `gis/data/synthetic/`, written by a new `gis/build_materials.py`.

| File | Stands in for | Grain | Key columns |
|---|---|---|---|
| `ec_mm_materials.csv` | MARA, MAKT, MARC, MBEW | Material at plant EC, one central store | `matnr`, `maktx`, `matkl` (group), `meins`, `dismm` (VB reorder point / PD programme), `minbe`, `eisbe`, `plifz`, `bstrf` (rounding: 50 kg bag, 20 L can, 8,000 L tanker), `verpr` (moving average price), `max_stock` (tank or shed capacity), `primary_lifnr` |
| `ec_mm_vendors.csv` | LFA1 subset | Supplier | `lifnr`, `name`, `city`, `route` (sea / road), `quoted_days` |
| `ec_mm_purchase_orders.csv` | EKKO, EKPO, EKET | PO line | `ebeln`, `ebelp`, `bedat` (PO date), `bsart` (NB normal / rush), `lifnr`, `matnr`, `menge`, `netpr`, `eindt` (requested delivery), `status`, **`delay_cause`** (answer key) |
| `ec_mm_movements.csv` | MATDOC | Material document line | `mblnr`, `zeile`, `budat`, `bwart`, `matnr`, `lgort`, `menge`, `shkzg` (S in / H out), `kostl` (division), `aufnr` (work or PM order), `block_code`, `ebeln`, `ebelp` |
| `ec_mm_reservations.csv` | RESB | Open planned issue | `rsnum`, `matnr`, `bdter` (requirement date), `bdmng`, `kostl`, `block_code`, `window` |
| `ec_mm_stock.csv` | MARD | Material, at 2025-05-23 | `matnr`, `labst` (unrestricted), `last_count` |

Movement types used: **101** goods receipt against a PO, **201** issue to a
division cost centre, **261** issue to a PM order, **551** scrapped (storage
loss found at a count), **701/702** count differences.

### The 16 materials

| Group | Materials | Demand type | Driven by (reconciles to) |
|---|---|---|---|
| Fertiliser | NPK 12-12-17-2, Urea, Kieserite, Borate | Programme | [ec_fertiliser.csv](gis/data/synthetic/ec_fertiliser.csv) issues, exactly, for the 2024-10 and 2025-03 rounds |
| Agrochemicals | Glyphosate 480 SL, Metsulfuron-methyl 20 WG, Hexaconazole 5 SC, rodenticide bait | Activity | Spraying hectares in `ec_upkeep_orders.csv` (about 1,500 ha a month); treatment and follow-up palms in `ec_pest_orders.csv`, split by pest through `ec_pest_treatment.csv` |
| Fuel | Diesel | Activity | Trip diesel in `ec_weighbridge.csv` (about 26,000 L a month), plus non-haul use |
| Spare parts | One part per fault type: alternator, injector pump, radiator, wheel bearing, hydraulic hose, clutch plate, brake shoe, tyre | Intermittent | `ec_pm_orders.csv`, one 261 issue per order (about 35 a month across the eight) |

Doses (litres per hectare, kilograms per palm) are literature placeholders in
the register, still to agree with the estate agronomist. The generator uses
the register defaults, as `build_operations.RATES` does.

### Seven suppliers

Pupuk Kaltim and Petrokimia Gresik (sea, fertiliser; the names already in the
stock feed), Meroke Tetap Jaya (sea, fertiliser), an agrochemical distributor
in Surabaya (sea), the fuel agent at the Merauke depot (road), and two parts
distributors: Jayapura (sea, faster, dearer) and Surabaya (sea, slower,
cheaper). Every name after the first three is invented and the header note
says so.

### History: 24 months, in two halves

2023-06-01 to 2025-05-23. Lead times need hundreds of orders, and fertiliser
needs more than two rounds, so five months is not enough.

- **2025-01-01 onward** the issues are read off the operations ledger and
  reconcile to it line by line, the way harvest orders reconcile to EPMS.
- **Before 2025** the ledger does not exist. Issues come from the same drivers
  at monthly grain: the 2023-10 and 2024-03 fertiliser rounds under
  `gen_fertiliser`'s rules at division grain, diesel from
  `ec_harvest_history.csv` tonnes at the 2025 litres per tonne, herbicide from
  the spraying rotation, parts from `failures_per_1000_trips` in
  `ec_vehicles.csv`. These rows carry no `aufnr`, which is how to tell them
  apart.

About 400 PO lines over the 24 months.

### How the store is simulated

Day by day, the way `build_operations` simulates crews:

1. Receipts due today post as 101, split in two for the supplier that splits.
2. Issues post as 201 or 261.
3. **If stock cannot meet an issue, a rush order is raised**: a local buy at a
   premium, arriving in one to three days, posted as a rush PO and its 101
   before the issue. Estates do this, and it keeps the MM ledger consistent
   with an operations ledger that was generated without regard to stock.
4. A reorder-point material whose stock position (on hand + open POs) falls to
   MINBE gets a normal PO for its rounding quantity. Fertiliser POs are placed
   PLIFZ days before each round's target date. **The history is generated
   under SAP's own settings**, which is what makes it the baseline.
5. Quarterly counts post storage loss as 551 and small differences as 701/702.

### What the generator plants

| Effect | Planted | Must come back |
|---|---|---|
| Quoted against real lead time | Each supplier's true median is PLIFZ × 1.15–1.6, sea routes worst | Learned median within ±10% of planted |
| Wet season | POs due December–March take ×1.25 on sea routes, ×1.4 on the depot road | Season factor within ±0.08 |
| Missed vessel | One sea supplier: 12% of POs arrive 21–35 days late; `delay_cause = vessel_missed` | Tail chance within ±5 points; that supplier has the widest P90 |
| Split deliveries | One supplier delivers 30% of POs in two receipts | Lead time measured to the receipt completing 95% of the quantity |
| Storage loss | Urea 0.6% a month, Kieserite 0.2%, posted at counts | Within ±0.15 points a month |
| Handling loss | Herbicide issued = sprayed ha × dose × 1.08 | Usage per hectare within ±4% |
| **Not planted** | Order size on lead time; weekday of the PO | Must not come back |

`delay_cause` is the answer key. It joins `skill` in `learn.ANSWER_KEYS`; the
loader strips it, and the recovery test is the only reader.

### Keeping every existing feed as it is

`build_synthetic.py` draws every generator from one random sequence, so a new
generator placed in the middle would shift every draw after it and move every
planted-truth check in buildplan_ml.md. `build_materials.py` follows
`build_operations.py`: its own seed (`f"{SEED}:materials"`), run after
`build_operations`, reading the ledgers off disk. The generator's check hashes
every existing CSV before and after, and fails if any changed except the one
it is meant to rewrite:

- `ec_fertiliser_stock.csv` is rewritten from `ec_mm_stock.csv` and the
  quoted lead times, so the Nutrition panel and the stores window cannot
  disagree. This is how `build_operations` already rewrites `ec_upkeep.csv`.

### A correction this plan makes

Twenty rows of `ec_fertiliser.csv`, 84 t (2% of the 2025-03 round), carry
issue dates between 2025-05-24 and 2025-06-27, after the export ends. The
Nutrition panel counts them as issued. In MM they become open reservations,
which is what they are on 2025-05-23, and `nutrition_position` counts them as
reserved rather than issued.

---

## 3. Lead time

**Question.** When Pupuk Kaltim is sent a PO today, when does the fertiliser
arrive, and how late can it be?

**Measure.** PO date (`bedat`) to the receipt that completes 95% of the
quantity. Rush orders are excluded: they measure the emergency, not the
supplier.

**Model.** Per supplier:

```
log days = route mean + supplier effect (shrunk) + wet season (pooled by route)
```

- The supplier effect is empirical Bayes: `(k × route mean + n × observed) / (k + n)`,
  with `k` in POs from the register (`leadtime_prior_orders`), chosen by backtest.
- The spread is Kaplan–Meier on days to receipt. A PO still open at the cutoff
  counts as "at least this long", not dropped (rule 3).
- The tail is one number per supplier: the chance of arriving more than 14
  days after the requested date, shrunk toward the route's rate.

**Output.** Median, P90, and the chance of a very late delivery, each against
the quoted PLIFZ. "Quotes 30 days; takes 41, and 1 order in 10 takes 63 or
more."

**Bar.** Held-out absolute error of the median below PLIFZ's, on POs placed in
each month after the cutoff. P90 covers 85–95% of held-out POs.

**Backtest.** Monthly origins, 2024-06 to 2025-05 (12 folds). Each fold fits on
POs placed before the origin, censoring what was still open, and scores POs
placed in the next month.

---

## 4. Consumption

**Question.** How much of each material goes out between now and the day a
PO placed today arrives?

**Model.** One formula per demand type, all a supervisor can check:

| Type | Expected use | Learned |
|---|---|---|
| Programme (fertiliser) | Open reservations × fulfilment, placed in time by the issue-lag distribution of past rounds | Fulfilment share and lag per division, pooled; from four rounds |
| Activity (chemicals, diesel) | Expected activity × usage per unit | Usage per hectare, palm or tonne, by ridge ratio; the planted 8% comes back here |
| Intermittent (parts) | Poisson: failures per 1,000 trips × expected trips, pooled by vehicle class | Rate per class; Croston-SBA on weekly counts is fitted as a challenger and reported |

Expected activity, known at the time: in the backtest, the trailing eight
weeks scaled by last year's seasonal ratio. Served for 2025-05-24 onward,
spraying uses the hectares `ops.demand('spray')` has due and diesel uses the
forward block forecast in `ec_forecast_forward.csv`. The served model
therefore has better inputs than the backtest gave it, and the section says
so.

**Spread.** The model's own past errors over the same horizon, bootstrapped,
as the work-done model does. No distribution is assumed.

**Bar.** Held-out absolute error on use over each material's lead time below
the trailing 13-week average, the figure SAP's reorder point is set from.
Scaled per material and pooled into one improvement.

---

## 5. Safety stock and the reorder point

**Question.** How much stock should trigger a reorder, how much of that is
buffer, and what does the buffer cost?

**Demand during lead time.** 2,000 paths per material, vectorised: draw a lead
time from §3 (tail included), then draw use over it from §4. The reorder point
is the service-level quantile of that total. Safety stock is the reorder point
minus the expected total.

**Why the buffer is that size.** Rerun with the lead time held at its median.
What remains is the demand share; the rest is the supplier's.

> Illustrative: "Of the 38 t buffer on NPK, 29 t is there because deliveries
> from Pupuk Kaltim vary, and 9 t because use varies."

**Service level.** From the register per group. The cost view shows the choice
against the alternatives at 80, 85, 90, 95, 97 and 99%: expected holding cost
(average stock × price × `holding_cost_pct_yr`, plus learned storage loss)
against expected rush cost (units short × learned rush premium). It marks the
cheapest point and never overrides the register.

**Order quantity.** Cover for `order_cover_weeks`, rounded to the material's
BSTRF and capped at `max_stock`. No EOQ: the lot is set by bags, cans and
tankers, and the estate already knows it.

**Projection.** Day by day from 2025-05-24 for 90 days: stock on hand from
MARD, open POs arriving on their §3 distribution, use drawn from §4. It gives
the P10–P90 band of stock, the chance of running out before the next receipt,
and the **order-by date**: the last day the stock position stays above the
reorder point.

### The test that decides whether it is used: the policy replay

Twelve months, 2024-06-01 to 2025-05-23, replayed twice from the recorded
stock on 2024-06-01:

- **Old way.** SAP's MINBE, EISBE and PLIFZ as generated, and fertiliser
  ordered PLIFZ days before each round.
- **New way.** Reorder points refitted at each month start on data before it.

Both replays meet the **recorded** issues. A replayed PO takes the lead time of
the recorded PO from the same supplier placed nearest in date. That is what
that lane actually did then, not a draw from the model being judged. When
stock cannot meet an issue, the replay raises a rush order at the recorded
premium.

| Reported per material and in total | |
|---|---|
| Rush orders and units short | Service |
| Average stock value held, storage loss | Holding |
| Total cost = holding + storage loss + rush premium | The one number graded |
| Cycle service level achieved against the target | The constraint |

**Bar.** Total cost below the old way's, and service achieved no more than 3
points under target. The grade is `learn.grade()` on the cost improvement.
`use_stock_model` is `auto`: on only when this passes.

**A check on the replay itself.** The history was generated under the old
policy, so replaying the old policy must reproduce at least 90% of the recorded
rush orders. If it does not, the replay machinery is wrong and neither result
is trusted.

---

## 6. Wiring

### The chain

```
PO history ──► lead time (3) ──┐
                               ├──► demand during lead time ──► reorder point, safety stock (5) ──► order-by date
MM issues, reservations, ─► consumption (4) ─┘                          ▲
ops ledger                                        service level, holding cost, rush premium (register)
```

### Backend

| File | Change |
|---|---|
| `gis/build_materials.py` | New. The generator and its checks; called at the end of `build_synthetic.build()`, results under `materials_checks` in the manifest |
| `gis/models/leadtime.py` | New. §3: `fit`, `distribution`, `backtest`, `recovery`, `reload` |
| `gis/models/consumption.py` | New. §4, same shape |
| `gis/models/safety_stock.py` | New. §5: `policy`, `projection`, `cost_curve`, `replay`, `backtest`, `reload` |
| `gis/stores.py` | New. Packaging in the voice of `gis/forecasts.py`: a sentence first, a range not a point, what it means, the trust grade, `EXPLAIN` per model, `warm()` in its own thread so the rail never waits |
| `gis/models/learn.py` | `delay_cause` added to `ANSWER_KEYS` |
| `gis/layers.py` | `nutrition_position` reads MARD stock and the learned reorder point, and counts post-export issues as reserved |
| `gis/assumptions.py` | A `stores` group (below) |
| `gis/features.py` | Five `sap_mm` requirements; three features on one `stores` panel in the Commercial domain ("buying what the estate cannot grow"); `stock_levels` now names MARD |
| `gis/readiness.py` | A `stores` row: needs, evidence, what it degrades to (today's cover-against-lead-time flag), the ask |
| `gis/decisions.py` | Two artifact kinds, below |
| `gis/copilot.py` | Three tools, below |
| `gis_router.py` | Endpoints, below |

### Endpoints

| Endpoint | Returns |
|---|---|
| `GET /gis/stores?group=` | Every material: on hand, days of cover, reorder point and safety stock (learned and SAP's), order-by date, status (order now / this week / covered / overstocked), plain sentence |
| `GET /gis/stores/material?matnr=` | Projection band, lead-time distribution, consumption drivers, safety stock split, cost curve, open POs, recent movements |
| `GET /gis/stores/lead-times` | Per supplier: quoted, median, P90, very-late chance, season factor, POs counted and censored |
| `GET /gis/stores/ledger?matnr=&from=&to=&limit=` | MM movements, MB51-style, paginated |
| `GET /gis/stores/accuracy` | The three backtests, the policy replay, recovery checks, the rules |

`/gis/forecast/accuracy` and the outlook's `trust()` stay at four models;
`test/models.mjs` asserts four and there is no reason to change it.

### Assumption register: a `stores` group

| Key | Default | Kind |
|---|---|---|
| `use_stock_model` | auto | on only when the policy replay passes |
| `service_level_fertiliser` | 97% | assumed |
| `service_level_agrochemical` | 95% | assumed |
| `service_level_fuel` | 98% | assumed |
| `service_level_parts` | 90% | assumed |
| `holding_cost_pct_yr` | 12% | assumed, agree with finance |
| `rush_premium_pct` | learned from rush POs | derived, editable |
| `leadtime_prior_orders` | 8, then backtest-chosen | derived |
| `order_cover_weeks` | 6 | assumed |
| `glyphosate_l_per_ha`, `metsulfuron_g_per_ha`, `hexaconazole_l_per_palm`, `bait_kg_per_palm` | literature | literature, agree with the agronomist |

A register edit to anything but a switch drops the fitted policy, as
`forecasts.assumption_changed` does.

### Decisions: two drafted documents

| Kind | When | Lines | Lands in |
|---|---|---|---|
| `material_requisition` | A material is past its order-by date | Material, quantity (rounded to BSTRF), supplier, needed-by date, the reorder point and why | SAP MM purchase requisition (ME51N); approver Estate Manager |
| `mrp_settings_change` | Learned settings differ from MARC by more than 20% | Material, field (MINBE, EISBE, PLIFZ), current value, proposed value, the evidence | Material master change (MM02); approver Head Office Procurement |

The existing `purchase_requisition` stays as the FFB purchase it already is.

### Copilot

Three tools, and the rule stays: the model never computes.

- `stock_position`: one material or group: on hand, cover, reorder point, order-by, chance of running out.
- `supplier_lead_time`: one supplier or material: quoted against median and P90, very-late chance.
- `reorder_advice`: quantity and date, and what a different service level would cost.

### The window

A `stores` entry in `POPUPS` ([registry.js:57](command_static/src/panels/registry.js#L57)),
built by a new `command_static/src/panels/stores.js` on the same contract as
`forecastsPopup`. Rail hint: "3 to order".

| Group | Sections |
|---|---|
| **Now** | At a glance (to order this week, running-out chances, stock value held against recommended, rush orders in the last 90 days) · Order list, with Raise requisition |
| **Materials** | Fertiliser · Agrochemicals · Fuel · Spare parts. Each row opens the material: the projection band chart, why the buffer is that size, the cost curve, open POs |
| **Suppliers** | Lead times against quotes · Wet season |
| **Ledger** | Movements · Purchase orders · Rush orders |
| **Trust** | Policy replay (old against new) · Lead-time and consumption backtests · Planted against recovered · What makes it real |

The projection chart is one inline SVG (band, median line, reorder point,
receipt markers). No chart library; nothing on the page uses one today.

The Nutrition panel's stock table reads `/gis/stores?group=fertiliser` and
links to the window.

### Tests

`command_static/test/stores.mjs`, Playwright, same harness as `models.mjs`:

- every `/gis/stores/*` endpoint returns 200
- the ledger reconciles: MARD = sum of movements per material; fertiliser 201s
  = `ec_fertiliser.csv` inside the window; one 261 per PM order
- `delay_cause` appears in no payload
- recovery: every planted effect back within tolerance, the unplanted ones not
- the replay reports both policies, and the old-way replay reproduces ≥90% of
  recorded rush orders
- `use_stock_model` off → every material says it runs on SAP's settings
- raising a service level never lowers a reorder point
- the window renders every section with no console errors; Raise requisition
  writes a `material_requisition`
- register and decisions reset afterwards

`node test/smoke.mjs` picks the new sections up without changes. Existing
`models.mjs` and `ops.mjs` must pass unchanged, which is the proof §2's seed
isolation worked.

---

## 7. What it cannot know yet

- **Four fertiliser rounds.** Round timing and fulfilment are learned from
  four occurrences and marked that way.
- **A real stockout hides demand.** The generated estate always rush-buys, so
  every need is recorded. In a real SAP extract, a stockout shows up as an
  issue that never posted or posted late, and demand after it is
  understated. The ask includes reservations (RESB) for that reason.
- **Receipt posting lag.** The store posts a receipt when it gets to it, not
  when the truck arrives. The generated lag is zero; a real extract needs the
  delivery note date beside the posting date.
- **A rush premium is not the full cost of running out.** If fertiliser cannot
  be rush-bought in Papua in the wet season, the real cost is a late round,
  which the upkeep deferral rate already prices. Until the estate says which,
  the rush premium is the only cost the ledger can show.
- **One store.** Division sub-stores and transfers between them wait for MARD
  by storage location.

**Real data that makes it yours.** Five standard SAP reports for plant EC over
24 months, no custom development:

| Report | Gives |
|---|---|
| MB51 | Movements 101, 201, 261, 551, 701/702 |
| ME80FN, with EKBE history | PO lines, schedule lines and every goods receipt |
| MM60, plus MARC fields | Reorder point, safety stock, planned delivery time, rounding |
| MD04, or RESB | Open reservations |
| MB52 | Stock on the extract date |

---

## 8. Order of work

| Phase | Work | Why here |
|---|---|---|
| 1 | `build_materials.py`: files, store simulation, planted effects, reconciliation and hash checks; `ec_fertiliser_stock.csv` rewrite; the post-export fertiliser correction; features and readiness rows | Every model is judged on it, and the seed isolation has to be proven before anything reads it |
| 2 | Lead time, backtest and recovery; `/gis/stores/lead-times` | Depends only on POs, and is the largest share of most buffers |
| 3 | Consumption, backtest and recovery | Depends only on the ledger |
| 4 | Safety stock, projection, cost curve, the policy replay; register group; `gis/stores.py`; remaining endpoints; warm-up | Needs 2 and 3 |
| 5 | The window, Nutrition panel swap, rail hint, the two artifact kinds | Needs 4 |
| 6 | Copilot tools, `test/stores.mjs`, build status in this file | Needs everything |

**Cut line.** If time runs short, spare parts go first: the eight parts, the
Poisson and Croston models and the parts section. Nothing else depends on them.

---

## 9. Three things to get right

1. **The baseline is SAP's settings, and the test is a replay of recorded
   issues.** Not a comparison with a textbook formula, and not the model's
   own simulation.

2. **Open POs are censored, not missing.** Drop them and the supplier who
   misses vessels looks like the fastest one on the list.

3. **Existing feeds do not move.** One shared random sequence runs through
   `build_synthetic.py`. A new generator is isolated on its own seed and
   proven by hash, or every planted-truth result in buildplan_ml.md silently
   changes.
