# Estate Command: the four operational domains

Plan for restructuring the estate command around Harvesting, Upkeep &
Maintenance, Pest & Disease, and Transport, and for building out the feature
set on synthetic feeds.

Written against the build as it stands on 2026-09-11: 291 real EC block
polygons, real harvest 2025-01-01 to 2025-05-23, real Sentinel-2 NDRE on all
291 blocks, and eight synthetic CSV feeds under `gis/data/synthetic/`.

---

## Build status

All ten phases built. **28 of 36 features live**, across six domains and 34
panels. Every endpoint returns 200 and every panel resolves its requirements
strip.

| Phase | State | Landed as |
|---|---|---|
| 1 Feature manifest | done | `gis/features.py`, `/gis/features`, `/gis/data-request`, requirements strip on every panel |
| 2 Domain regrouping | done | Rail groups by the six domains, not by use-case number |
| 3 Real public feeds | done | `gis/build_terrain.py`, `gis/build_rainfall.py`, `gis/environment.py` |
| 4 Transport feeds | done | Trip ledger, fleet master, PM orders; `/gis/transport` |
| 5 Shrinkage detector | done | `gis/models/shrinkage.py`, `/gis/shrinkage` |
| 6 Pest & disease | done | `gen_pest`, `/gis/pest`, four panels |
| 7 Fertiliser, roads | done | `gen_fertiliser`, `gen_roads`, `/gis/nutrition`, `/gis/roads` |
| 8 Clustering | done | `gis/models/clusters.py`, `/gis/clusters` |
| 9 Crew productivity | done | `gen_harvester_days`, `gis/models/productivity.py` |
| 10 Lagged forecast | done | `gen_harvest_history`, `gis/models/lagged_forecast.py` |

Live coverage by domain:

| Domain | Live | Panels |
|---|---|---|
| Harvesting | 5 of 9 | 9 |
| Upkeep & Maintenance | 7 of 8 | 6 |
| Pest & Disease | 4 of 5 | 5 |
| Transport | 4 of 6 | 6 |
| Commercial | 3 of 3 | 3 |
| Governance | 5 of 5 | 5 |

Still specified but not built, as of 2026-09-11: realised cutting interval,
grading by harvester, loose fruit recovery, bunch-weight trend, herbicide
adherence, canopy early warning, free fatty acid decay, collection point
coverage. Each already carries its requirements strip, which is the part the
client is being shown.

**Update 2026-09-18.** Seven of the eight are built; see
[buildplan.md](buildplan.md) "Closed on 2026-09-18". Grading by harvester stays
planned because the export's grading has no variance to attribute (99.82% ripe),
and the manifest note now says so with the figures. Loose fruit recovery runs on
the client's own `loose_fruits` counts, which this plan and the readiness
register had wrongly recorded as absent.

### The leave-behind, generated

`GET /gis/data-request` now writes itself:

| Source system | Extracts | Features unblocked |
|---|---|---|
| EPMS field mobile | 6 | 11 |
| EPMS core | 7 | 9 |
| Mill weighbridge & laboratory | 4 | 8 |
| SAP Plant Maintenance | 4 | 7 |
| SAP Materials Management | 4 | 6 |
| SAP Sales & Distribution | 2 | 2 |

Highest-value single ask: vehicle trip logs with timestamps, from SAP PM,
which unblocks six features on its own.

### Corrections to this plan, found by building it

- **scikit-learn is 1.6.1, not 1.8.** The earlier check ran against the wrong
  interpreter. The app runs on the Anaconda Python, where HDBSCAN and
  IsolationForest are both present, so nothing in the plan changes.
- **The Copernicus DEM is a surface model.** Taking the raw gradient gave a
  mean slope of 4.9 degrees on a coastal plain with 29 m of total relief: that
  is palm crowns, not ground. `build_terrain.py` now smooths to a 150 m
  landform scale before the gradient, and reports the discarded residual as
  canopy roughness. See the module docstring.
- **More features made the anomaly detector strictly worse.** Six columns
  scored 0.0 precision against the planted cohort; one column scored 0.85.
  Recorded as an ablation in `gis/models/shrinkage.py` and shown in the panel,
  because it is the strongest argument in the build for planting answers.

### Findings the build produced

- **Elevation and canopy vigour correlate at rho 0.415** across all 291
  blocks. Two independent real measurements, neither from the client, both
  free. On a coastal plain that points at drainage, which is an Upkeep
  finding obtained with zero client data.
- **Terrain slope explains nothing here** (rho 0.05 against yield). The estate
  is flat, so harvester quotas need no slope adjustment. Worth saying out
  loud: it stops the productivity model being asked to carry a covariate with
  no range, and on a hill estate the same layer would carry real signal.
- **Half the haulage cycle is queueing**, not driving: 48.6% of mean
  turnaround is time at the mill gate. That is the part the estate can change
  without buying a vehicle.
- **A flat harvester quota is wrong at both ends.** The productivity model puts
  a fair day between 97 and 187 bunches depending on the block. A single
  estate-wide quota of 138 is 41 too high on the hardest blocks and 49 too low
  on the easiest, which is precisely how difficult blocks end up half-collected.
- **The lagged forecast found season, not biology.** Given every lag from 1 to
  24 months and no hint which matter, rainfall importance landed on lags 1 to 3.
  Only 10.2% fell in the 20-24 month sex-determination window. Rainfall three
  months ago is an excellent proxy for what season it is, and season explains
  much of a 36-month series. Telling a real 24-month biological signal apart
  from a seasonal proxy needs several years of real target, which is exactly
  the extract this feature asks for. Reported as-is rather than tuned until it
  flattered the method.

### Calibrations the build forced

Three generated layers were wrong on the first pass and are worth recording,
because each was caught by asking whether the output was physically possible.

- **Roads.** The first scoring put 115 blocks at impassable-when-wet and none
  in good condition. An estate with 40% of its roads impassable would not be
  harvesting. Recentred to 36 good, 167 fair, 87 poor, 1 impassable.
- **Terrain.** Covered above: the raw DEM gradient measured canopy.
- **Worker output.** The first version wrote slope onto each worker-day row
  without letting it affect output, and the regression still found a slope
  coefficient, an artifact of crew-size rounding. A synthetic feed that does
  not contain the effect its model claims to find is worse than no feed, so
  difficulty now drives crew size directly.

The pest layer was checked rather than assumed: Moran's I of 0.914 on eight
nearest neighbours confirms the Ganoderma foci are genuinely clustered, which
is what makes the map credible to anyone who has walked an estate.

---

## 0. What this app is

Not an operations tool. A **feature catalogue that runs**.

The purpose is to show the client a working version of every feature worth
having, and to attach to each one the precise list of data it needs from them.
Synthetic data is the specification device: a feed exists in
`gis/data/synthetic/` exactly because the client has not supplied the real one,
and its presence in the demo is what lets the pitch be specific.

The sentence the whole build is engineered to support:

> "Here is field-to-mill shrinkage detection running on your estate. To run it
> on your real numbers we need four things: TPH bunch counts per trip, mill
> weighbridge tickets, the vehicle and driver on each trip, and mill grading
> dockage. You have all four in EPMS and SAP today."

Three consequences follow, and they drive every decision below.

1. **Build every feature.** A feature that needs data the client does not have
   is not a feature to cut. It is the one with the longest ask, and the ask is
   the product. Do not rank by how much real data backs a feature.
2. **Coverage beats depth.** Twenty-eight features each running credibly on
   synthetic feeds is worth more than four features running on real data,
   because the catalogue is the thing being sold.
3. **Every feature must state its own data requirements, in the UI.** This is
   the one genuinely new piece of architecture in this plan. See section 2.

The provenance discipline already running through the build, the `real` /
`derived` / `synthetic` badge on every metric, is not a caution. It is the
pitch mechanism. Every synthetic badge on screen is a line item on the data
request, and the register at `/gis/readiness` is already half of the leave-behind
document.

---

## 1. The four domains

The rail currently groups work by use-case number: UC-08 contract position,
UC-09 vendor sourcing, UC-01 rotation, UC-11 replant, UC-12 labour, UC-14 audit.
That is consultant shorthand. No estate manager thinks in UC-08. Regrouping
costs almost nothing, since `PANELS` is a dict of six entries in
`command_static/command.html`.

**One addition to the four-way split.** Fire and force majeure, contract
position, vendor sourcing, the decision log and the readiness register belong to
none of the four, and forcing them in makes the taxonomy worse. Use the four
operational groups plus **Commercial** and **Governance**, with fire keeping its
own group as it has now.

What each domain has today:

| Domain | Features today | Backed by |
|---|---|---|
| **Harvesting** | rotation, ripeness pressure, bunches/ha, peer index, grading deduction, tonnage, labour deficit | bunch counts and planting years real; rotation, gangs, ABW, labour synthetic |
| **Upkeep & Maintenance** | upkeep days overdue on four activities, cost ledger, replant horizon | planting years real; the rest synthetic |
| **Pest & Disease** | nothing | — |
| **Transport** | `km_to_mill`, one `transport_idr` cost line | synthetic |

Two domains need relabelling, two need building from nothing.

Pest and disease is worth one blunt note for the room. The register already
measured the only available proxy as dead: EC grading is degenerate across all
291 blocks, mean deduction rate 0.25%, maximum 2.1%, minimum ripe rate 98.8%,
and there is no table in the 138-table schema that can hold an infected palm.
That is not a reason to skip the domain. It is the strongest data ask in the
whole deck, because the client cannot see pest pressure at all today.

---

## 2. The feature manifest: the centrepiece

A machine-readable map from feature to the data it needs. Everything else in
this plan hangs off it.

New module, `gis/features.py`:

```python
FEATURES = [
  {
    "id": "shrinkage_detection",
    "domain": "transport",
    "label": "Field-to-mill shrinkage",
    "question": "Which trips deliver less than the bunch count says they should?",
    "runs_on": "synthetic",
    "requires": [
      {"entity": "TPH bunch count per trip",  "system": "EPMS field mobile",
       "table": "t_tph / t_oph",      "status": "synthetic", "readiness": "weighbridge"},
      {"entity": "Weighbridge ticket, gross/tare/net", "system": "SAP / mill",
       "table": "ZEPMS weighbridge",  "status": "synthetic", "readiness": "weighbridge"},
      {"entity": "Vehicle and driver on trip", "system": "SAP PM",
       "table": "vehicle trip log",   "status": "synthetic", "readiness": "fleet"},
      {"entity": "Block polygons",    "system": "client ArcGIS",
       "table": "EC_overlay",         "status": "real",      "readiness": "block_map"},
    ],
  },
  ...
]
```

Four things fall out of it, all for free:

- **A requirements strip on every panel.** Each panel header renders its
  `requires` list, green where satisfied, amber where stubbed. The client sees
  the ask attached to the feature, at the moment they are looking at the
  feature. This is the single highest-value UI change in the plan.
- **`GET /gis/features`**, the catalogue: every feature, its domain, its
  question, what it runs on, what it needs.
- **`GET /gis/data-request`**, the leave-behind. Union of every requirement
  whose status is not `real`, grouped by source system, deduplicated, each with
  the features it unblocks. That document currently does not exist and would
  otherwise be written by hand after the meeting.
- **A coverage headline.** "28 features, 6 running on your data, 22 waiting on
  11 extracts." That number is the pitch in one line.

Each requirement's `readiness` key points at a row in the existing
`gis/readiness.py` register, so the fine-grained per-feature list and the
coarse capability register stay consistent and the interview endpoint keeps
working unchanged.

---

## 3. The feature set to build

Target is roughly 28 features across the four domains. Everything not marked
*exists* is new.

### Harvesting

| Feature | Needs |
|---|---|
| Harvest rotation and ripeness pressure *(exists)* | last-harvest date per block, gang roster |
| Yield vs age-matched peers *(exists)* | harvest history, planting year |
| Realised cutting interval vs target | harvest dates per block, multi-year |
| Crew productivity and adjusted targets | worker-day output, terrain slope, palm height |
| Grading quality by harvester | harvester identity on the grading log |
| Loose fruit recovery rate | TPH loose-fruit counts |
| Average bunch weight trend per block | weighbridge tickets joined to bunch counts |
| Labour supply vs demand *(exists)* | attendance, gang establishment |
| Lagged yield forecast, 1-6 months out | 36 months harvest history, rainfall, fertiliser actuals |

### Upkeep & Maintenance

| Feature | Needs |
|---|---|
| Upkeep rotation adherence *(exists)* | activity completion dates per block |
| Nutrient applied vs agronomy target, kg/palm | MM goods issues for NPK, urea, borate, kieserite |
| Application lag vs target window | goods-issue date against agronomy programme |
| Weeding and herbicide programme adherence | herbicide batches issued per block |
| Agronomic underperformance clustering | vigour, yield, age, inputs, upkeep state |
| Road condition and grading backlog | road grading and culvert work orders |
| Fertiliser stock cover and lead time | warehouse stock levels, lead times |
| Replant horizon *(exists)* | planting years |

### Pest & Disease

| Feature | Needs |
|---|---|
| Ganoderma census and incidence map | palm census with per-palm status |
| Spread risk from infected-neighbour proximity | census plus block geometry |
| Rat and rhinoceros beetle damage watchlist | scouting rounds with damage counts |
| Treatment coverage and follow-up due | treatment records with dates |
| Early warning: vigour anomaly against census | NDRE *(already real)* plus census |

### Transport

| Feature | Needs |
|---|---|
| Field-to-mill shrinkage detection | bunch counts, weighbridge tickets, vehicle, driver |
| Turnaround time, block to mill | trip depart and arrive timestamps |
| Diesel litres per tonne evacuated | fuel issues, odometer, net weight |
| Fleet MTBF and breakdown downtime | PM breakdown work orders |
| FFA progression vs dispatch delay | laboratory FFA, dispatch and receipt times |
| TPH collection coverage and scheduling | trip logs against TPH locations |

Cross-cutting, already present: contract position and vendor sourcing
(Commercial), fire and force majeure, decision log, readiness register, ask the
map, and the generated briefs.

---

## 4. The four ML use cases

All four get built. Each is a feature in the manifest with its requirement list
attached, and the list is the point.

### Agronomic underperformance clustering

Cheapest to build and the most real-data content, so it is a good first one.
NDRE is real on 291 of 291 blocks from scene S2B_54MVT_20250215 at 20 m, yield
is real, palm age is real. Only fertiliser in kg is stubbed.
`layers.compare_metrics` already does the two-metric version, rank correlation
plus the blocks in the bottom fifth of both. Clustering generalises it.
scikit-learn 1.8 is installed and ships HDBSCAN in `sklearn.cluster`, so no new
dependency.

The existing finding motivates it: satellite vigour and recorded yield rank this
estate almost independently, Spearman rho 0.10, with thirteen blocks weak on
both. Label clusters by which dimensions are extreme rather than by root cause,
since "fertiliser deficit" is a claim `gis/reasoning.audit_figures` will flag
and "low vigour, on-programme upkeep, average age" is one it will not.

### ERP discrepancy and shrinkage

The motivating example is real and already in the repo. EC records 2,870 bunches
per hectare per year, which at the 12-16 kg bunch weight normal for 7-11 year
palm implies 34-46 t/ha/yr against a ~28 t/ha/yr ceiling for a very good estate.
`build_synthetic.py` back-solves ABW to ~8.01 kg so the estate lands at a
printable 23 t/ha/yr, and says so in the file header. That is a live
count-versus-weight discrepancy in the client's own export, currently disclosed
in a CSV comment. Promote it to the panel: it is the best argument in the deck
for why they want this feature.

Build it on a trip-level feed with a known anomaly cohort injected, one driver,
one route, one fortnight. Robust z-score on the expected-versus-actual weight
ratio as the primary detector, IsolationForest as a second opinion.

Present it as **injected versus recovered**: here are the anomalies we planted,
here are the ones the detector found, here is precision and recall. Not as a
disclaimer, but as proof the method works, which is what earns the real
weighbridge extract.

### Worker and crew productivity

Terrain slope is obtainable for real, free, today. `gis/build_ndre.py` already
reads Sentinel-2 through the public STAC catalogue at earth-search plus a public
TiTiler for the raster crop, and that same catalogue carries `cop-dem-glo-30`,
verified live on 2026-09-11. Per-block mean slope, relief and aspect is the same
script with a different collection and no band arithmetic.

Worker-day output stays synthetic. EPMS has `t_attendance` and
`m_gang_employee`, the EC export carries neither, and the current labour feed is
division-by-month rather than worker-by-day.

Output is expected bunches per man-day given slope, palm height from planting
year, and bunch density, with a residual per harvester. Ship it as an adjusted
daily target per block. Keep "phantom labour" off the panel headings: in front
of a client whose harvesters are named in the data, "unexplained variance worth
a supervisor visit" is the same finding and survives the room.

### Lagged yield forecast

Build it on synthetic multi-year history, and let the length of that history be
the ask.

The mechanism is real agronomy: inflorescence sex determination happens 20-24
months before bunch maturity, bunch abortion 8-10 months before. The export runs
2025-01-01 to 2025-05-23, under five months, so the lag structure cannot be
identified from it. Generate 36 months of block-level history consistent with
the five real months, fit LightGBM on it, and show the fitted lag importances.
LightGBM 4.6 is installed.

Then put the requirement on the panel in the plainest possible terms:

> This model reads 36 months of block-level harvest, 24 months of rainfall and
> your fertiliser actuals. You gave us five months of harvest. Here it is on
> generated history so you can see the output, and here is the extract that
> makes it yours.

Two of its three inputs can be made real cheaply. Rainfall is free: Open-Meteo
is already a dependency, `gis/fire.py` uses its forecast endpoint, and the
archive endpoint needs no key and returned real daily precipitation over EC's
coordinates back to 2023 when tested on 2026-09-11. Pull 36 months and the
rainfall half of this feature stops being synthetic. Only the harvest history
and the fertiliser actuals remain as asks, which sharpens the request rather
than diluting it.

---

## 5. Feeds to build

Follow the conventions in `gis/build_synthetic.py`: one seed, one CSV per
eventual real extract, a `# SYNTHETIC.` header naming the EPMS or SAP table it
stands in for, and every row joined to a real block. That last convention is
what makes the swap to real data a file replacement rather than a rewrite, and
it is worth protecting.

**The one design rule that matters.** Extend `_latent_field`, do not draw
independently. That smooth spatially-autocorrelated surface, anchored to each
block's real yield relative to its planting cohort, is why the existing
synthetic layers corroborate the real harvest instead of contradicting it. A
Ganoderma hotspot on a high-vigour high-yield block makes the map read as noise
and the client stops believing any of it.

Synthetic:

| File | Carries | Unlocks |
|---|---|---|
| `ec_weighbridge.csv` | trip, date, block, TPH, gang, driver, vehicle, EPMS bunch count, gross/tare/net kg, depart/arrive, mill dockage | shrinkage, turnaround, ABW trend, FFA decay |
| `ec_transport.csv` | vehicle class, odometer, diesel litres issued, breakdown orders, downtime hours | litres per tonne, fleet MTBF |
| `ec_pest.csv` | census round, palms inspected, Ganoderma-suspect, beetle damage, rat bait points, treatment date | the entire P&D domain |
| `ec_fertiliser.csv` | goods issues: block, material, kg, issue date, agronomy target window, stock on hand, lead time | nutrient gap, application lag, stock cover |
| `ec_harvester_day.csv` | worker, date, block, bunches, loose fruit kg, man-days, activity code | productivity, grading by harvester, loose fruit |
| `ec_roads.csv` | segment geometry on block edges, condition, last grading, culvert repairs | road backlog, and a transport cost that varies with condition |
| `ec_harvest_history.csv` | 36 months of block-level bunch counts, consistent with the five real months | the lagged forecast |

Real, and new:

| File | Source | Status |
|---|---|---|
| `gis/build_rainfall.py` | Open-Meteo archive API, 36 months daily precipitation | verified reachable, no key |
| `gis/build_terrain.py` | `cop-dem-glo-30` via the STAC and TiTiler path `build_ndre.py` already uses | verified present in the catalogue |

---

## 6. Where each piece lands

- **Manifest** — new `gis/features.py`. Pure data, no imports, so everything can read it.
- **Feeds** — new generators in `gis/build_synthetic.py`; two new real builders alongside `build_ndre.py`.
- **Joins** — `gis/layers.py`. New keys in `METRICS` with their provenance word, new estate-level panel functions beside `contract_position` and `rotation_plan`.
- **Models** — new `gis/models/` package: `shrinkage.py`, `clusters.py`, `productivity.py`, `lagged_forecast.py`. Keep them out of `layers.py`, which is a pure join that imports cheaply and serves fast. Fit offline, cache the artefact, never fit inside a request. There is an mlflow store in the repo already if run tracking is wanted.
- **Endpoints** — `gis_router.py`, keeping the existing split: deterministic endpoints never call a model and never fail; generated ones return `{"available": false, "reason"}`.
- **Agent** — one tool per feature group in `copilot._TOOLS`, plus a `data_requirements` tool so the copilot can answer "what would we need to run that here". Note the constraint the figure audit imposes: a tool must return every number its narrative will quote, pre-computed. A tool returning cluster assignments but not cluster sizes will have its narrative flagged unverified.
- **UI** — `command.html`. `PANELS` regroups into six domains; the requirements strip renders from the manifest; new choropleth metrics arrive for free through `/gis/metrics/catalogue`; each new panel is a function like `panelContract`.
- **Readiness** — `gis/readiness.py` gains rows for the new source entities, and each manifest requirement points at one.

New choropleth metrics, at no extra UI cost once the feeds exist:
`shrinkage_pct`, `turnaround_hours`, `diesel_l_per_tonne`, `nutrient_gap_kg_palm`,
`application_lag_days`, `road_condition`, `ganoderma_incidence_pct`,
`spread_risk`, `bunches_per_man_day`, `productivity_residual`,
`loose_fruit_pct`, `slope_deg`, `ffa_pct`.

---

## 7. Suggested order

Ordered by what unblocks the most, not by how real anything is.

| Phase | Work | Why here |
|---|---|---|
| 1 | `gis/features.py` manifest, `/gis/features`, `/gis/data-request`, the per-panel requirements strip | Turns the whole app into the pitch instrument. Everything after it inherits the behaviour. |
| 2 | Regroup `PANELS` into four domains plus Commercial and Governance | Hours, not days. Changes how the whole demo reads. |
| 3 | `build_rainfall.py` and `build_terrain.py` | Two feeds move from ask to satisfied, free, using machinery that already works. |
| 4 | Weighbridge and transport feeds, then the Transport domain panels | Biggest empty domain, and the feed unlocks six features at once. |
| 5 | Shrinkage detector with the injected-vs-recovered table | Best single demo moment, with a real motivating example already in the repo. |
| 6 | Pest feed and the five P&D panels | Second empty domain. Strongest data ask in the deck. |
| 7 | Fertiliser and roads feeds, then the Upkeep panels | Completes Upkeep and feeds the clustering model. |
| 8 | Clustering model | Needs the fertiliser feed from phase 7. |
| 9 | Harvester-day feed, productivity model, grading-by-harvester, loose fruit | Needs terrain from phase 3. |
| 10 | 36-month history feed and the lagged forecast | Heaviest model, and it reads best once the rest of the catalogue is populated. |

---

## 8. Three things to get right

1. **Every feature carries its requirement list, visibly.** A panel without one
   is a feature the client cannot buy, because they cannot see what it would
   cost them to have it. This is the difference between a demo and a
   specification.
2. **Never let a synthetic feature wear a real badge.** Not for caution, for
   sales. The badge is how the client tells the difference between what they
   already own and what they would be commissioning, and a single mislabelled
   panel makes them doubt the rest of the register.
3. **Injected versus recovered, wherever a model is unsupervised.** Showing what
   was planted and what was found is what makes a synthetic demonstration
   evidence rather than decoration.
