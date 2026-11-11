# Estate Command: where the estate loses money

Plan for a cost-efficiency capability on synthetic SAP FI/CO records: break the
estate into cost nodes, give every node an efficiency score and a rupiah
leakage, and open on the ten places money is being lost and what to do about
each.

Written against the build as it stands on 2026-10-13: 41 panels in six rail
domains, the ops ledgers, the stores models, the assumption register, the
decisions log and a 29-tool copilot. Synthetic world anchored at 2025-05-23
(re-anchoring is still deferred).

Why this, now. The client review of Estate Command and the FFB forecast
returned two points: planning and forecasting already exist in-house and run
on years of field experience; and the app's purpose is lost under too many
menus. What they asked for instead: **a system that tells them where they are
losing money and how to stop it.**

Scope agreed on 2026-10-13: **one estate (EC), synthetic SAP cost records,
inside Estate Command** as a new rail domain, not a separate page.

---

## Build status

All phases built on 2026-10-13. **Cost efficiency** is first on the rail with
four entries (Where money leaks, Efficiency map, Budget vs actual, Block
margin), 11 endpoints under `/gis/finance/*`, a 48-tool copilot, and
`test/finance.mjs` green beside every existing suite (smoke 46 panels in 7
domains, ops, models, stores, the seven panel tests, blocks, selection,
compare, forecast lab, 51 Python tests).

| Phase | State | Landed as |
|---|---|---|
| 1 Ledger | done | `gis/build_finance.py`: 36,436 postings and 12 new files; reconciles to payroll man-days, MM issues at standard and the fuel issued per division, exactly; Rp 29.0 M/ha/yr; every other feed byte-identical |
| 2 Engine | done | `gis/finance.py`, ten functions, 4,692 unit-months; recovery 18 of 18 |
| 3 Window | done | `command_static/src/panels/leakage.js`; map metrics `efficiency_pct`, `leakage_idr_per_ha` |
| 4 Nova layer | done | `gis/diagnosis.py` (66 checks), `gis/finance_playbook.py`, `prompts/estate_leak_diagnosis_system.txt` |
| 5 Other functions | done | all ten functions live, each with checks and actions |
| 6 Loop | done | `cost_action` with a close step; past findings in the grounding; a Cost actions section in Did it work; four copilot tools |
| 7 Trust | done | budget comparison, `test/finance.mjs`, `evals/run_leak_diagnosis.py` (15 cases, three settings) |

### The tests that decide it, measured

| | Budget variance | Leak engine |
|---|---|---|
| Planted leaks found (9 planted + 3 counterfactual) | 3 | **12** |
| Decoys raised as problems (4) | 1 | **0** |
| Planted rupiah located to the unit | Rp 170 m | **Rp 794 m of Rp 794 m** |

Median unit efficiency 87.8%, inside the 80-92% band. April 2025 reads
"Rp 2.04 bn leaked; estate efficiency 90%". Budget variance finds the
fleet, culvert and grading leaks, whose cost lands on one object; it misses
every leak spread across objects (overtime, padding, fertiliser labour) or
hidden in a generous line (fuel, urea), and every crop loss, which is never
posted.

### The diagnosis, measured

`python evals/run_leak_diagnosis.py --runs 3`: 15 cases (9 planted, 3
counterfactual, 3 decoy), medians of three runs on Nova Pro, 99 calls.

| | Planted causes right (12) | of which counterfactuals (3) | Decoys read as normal (3) | True cause ruled out | Unverified figures | Invalid after retry |
|---|---|---|---|---|---|---|
| With the checks (the product) | **10** | **1** | 3 (the gate) | 2 | 0 | 0 |
| Checks withheld (ablation) | 7 | 0 | 3 | 3 | 0 | 0 |
| Decoys with the gate lifted | | | **0** | | 0 | 0 |

What it says:

- **The checks do work, but less than the plan needs.** On the nine leaks with
  the textbook cause for their driver, Nova is right 9 of 9 with the checks and
  7 of 9 without: the cause list alone points at them. The counterfactuals
  separate the settings: same drivers, the less obvious cause. With the checks
  it gets G5-01's "rounds too big" right; without them, none.
- **Nova Pro still answers from its prior on two of three counterfactuals.**
  TR-06's tank leak reads as siphoning, and U6-03's late muster as ghost
  names, on every run, while the checks say the opposite: 15 drivers' small
  fills over the burn on 27 of 27 days, and a gap that runs 0 to 5 with
  absence. Rewording those two findings to state the comparison (done once,
  in-sample) changed nothing. The §7-style bar "clearly worse without the
  checks" is met (10 against 7), the §5 bar of 10 of 12 is met, and the true
  cause is still ruled out twice.
- **On noise, Nova invents confident causes.** Every decoy, with the gate lifted,
  came back "likely" with a named cause. So leaks under the materiality line
  are no longer sent to the model: the unit reads "within normal variation for
  its peers" (`diagnosis.diagnose`, `force` lifts the gate for the eval only).
- Nothing it wrote failed the figure audit, and no id was invalid, in any of the
  99 calls.

**Next.** Try the main tier on a stronger model for the diagnosis only
(`BEDROCK_MODEL_ID`; the eval compares in one command), or move the decisive
signatures into deterministic cause tests that Nova must not contradict. Until
one of them clears the counterfactuals, the window should present a likely
cause as what it is: a hypothesis with a field check to confirm it.

Live cost: about 1.6-2.2 k tokens in and 0.35-0.55 k out per diagnosis, 3-10
s on Nova Pro: under half a US cent each, half the estimate in §5.

### Corrections to this plan, found by building it

- **Leakage counts drivers that ran over; savings never net against waste.**
  With S + L − N netted, gang G3-02's planted overtime vanished against its
  below-average idle time. Leakage is now the sum of positive drivers, and
  necessary cost counts each driver at the lower of actual and benchmark, so
  leakage = S + L − N still holds. Rule 4 in its strongest form.
- **The upkeep rate card is not a benchmark here.** Every block ran ~35% over
  the register's rates (the generated crews work at ~80% of them), so seven
  division groups flooded the list. Upkeep labour is now judged against the
  best quarter of blocks per activity, like everything else; the card stays
  as a fact.
- **Monthly best quartile was too strict** (median 76%). The plan's own fix,
  a three-month rolling unit cost for the benchmark, brought it to 87.7%.
- **Division groups mean "this division, not the others".** A group needs one
  driver on most of a division's units *and* the division's rate on it at
  1.5× the median division's. Otherwise systemic noise formed a group in
  every division.
- **Overtime, unassigned labour and pay above attendance belong to the gang**,
  not the block (Workforce function). They post to block orders in CO but the
  gang controls them.
- **Paid idle needs the crew's own roles.** Muster runs 15-50% above work-order
  man-days because cutters' orders carry no mandor, kerani or collectors.
  Necessary idle is the larger of the crew's support-role share and its type's
  best quartile; Sundays and rain-outs (15 mm for spray teams) are necessary.
- **Leaks were planted without touching MM.** The plan's "matching MM rows" would
  have broken the stores reconciliation. Instead: fuel card fills (TR-04),
  supplier invoices against receipts (urea, 5% not 18%: at 18% one leak was
  ~Rp 1 bn and swamped the list), fertiliser application contract sheets
  (Division 5's labour, not over-issue, which the generated programme already
  under-issues at ~86%), workshop job cards (DU-01) and road orders. The spray
  over-dose plant was dropped: herbicide excess already exists in MM.
- **Roads are judged on surveyed length and the first repair in 12 months**,
  not a peer rate: grading frequency is policy. Grading is priced with a gravel
  top-up (Rp 12 M/km) so 0.17 km segments carry material money.
- **Fleet, roads and workforce recovery is judged over the window**: repairs
  and road works are lumpy, and 67 crews' monthly ranks are noisy.
- **Fuel issues carry no vehicle**, as the plan found; the fill log splits each
  division's daily issue across vehicles by trip litres, with ±3% dispensing
  noise.
- **The FFA and haulage rates moved into the register** (`ffa_idr_per_kg_point`,
  `transport_idr_per_kg_km`); the FFA panel reprices per call.
- **Loose fruit uses the module's method on this window.** The panel reads
  EC's real 19 months; the cost window prices January-May 2025 only, so the
  two figures differ by design.
- **Terrain barely moves cost on this estate** (Rp 126.8k/t raw on the hardest
  tenth against Rp 126.5k), so the terrain decoy tests that hard blocks are not
  singled out and their wage efficiency stays within 5 points. Late rounds on
  far blocks are real crop loss and stay counted.
- **Holding cost and rework are not built.** Stores holding cost needs a
  per-material recommended stock the stores module does not expose; upkeep
  rework needs completion semantics the generated orders do not carry.
- **The overtime cause descriptions were reworded once** after the first eval
  run read G3-02 as "rounds too big": the eval is in-sample for that pair.
- **Counterfactual leaks were added after the first eval**, because the planted
  nine all carried the textbook cause and the ablation could not tell whether
  the checks mattered (9 against 8). TR-06 (tank leak), G5-01 (rounds too big)
  and U6-03 (muster written late) share drivers with TR-04, G3-02 and U4-02.
  The engine must find them with the right driver in the top ten of their
  function over the window (the workforce top five is crowded by natural
  paid-idle leaks).
- **A materiality gate on the diagnosis**, from the decoy result above.
- **A diagnosed row's Do follows the likely cause**, not the playbook's first
  action, so a rush-buy reading is not paired with "recover the overcharge".
- **The full regenerate does not reproduce the committed world.** Running
  `build_synthetic.py` today changes 25 existing feeds (bunch weights, trips,
  orders): the real-data inputs underneath have moved since they were
  committed. That predates this build. The finance generator now runs last in
  that chain, but this build was made and measured on the committed feeds with
  `python gis/build_finance.py` alone. Do not run the full chain without
  re-running every planted-truth check in buildplan_ml.md and
  buildplan_stores.md.

---

## 0. What changes

| Today | Where it fails | After |
|---|---|---|
| `ec_block_costs.csv` is allocated top-down: rate × hectares × noise ([build_synthetic.py:268](gis/build_synthetic.py#L268)) | Cost has no link to work done. Ranking blocks on it ranks the random draw. | A CO line-item ledger built bottom-up from the work orders, trips, material issues and PM orders, every line pointing at its source document |
| Rupiah figures scattered across Loose fruit, FFA, Herbicide, Block margin and the ops deferral, each with its own price and method | No single "how much are we losing" number; the same loss can be counted twice | One leakage engine, one price register, each loss owned by exactly one node |
| The money questions sit in Commercial and inside individual panels | The client could not find the value | A **Cost efficiency** domain at the top of the rail; its window opens on the top ten leaks |
| Block margin answers "which blocks earn their keep" | Margin mixes yield luck with cost control and says nothing about the cause | Per node: spend, necessary cost, leakage, efficiency, the cause, the action |
| A driver name ("fuel gap") would be the only "why" | A driver is a symptom; the fix depends on the cause behind it | Nova weighs server-run checks to name the most likely cause, rule others out, and draft the action from a vetted playbook (§5) |

Nothing here writes to SAP. An action becomes a drafted `cost_action` in the
decisions log, and the Did-it-work panel reads the node's leakage afterwards.

### The first screen

What the window says on 2025-04 (last closed month), when built. The figures
are illustrative; the rows are the planted leaks of §3.

> **Rp 612 m leaked in April · estate efficiency 86% · Rp 214 m deferred**
>
> | # | Where | Leak | Why | Do |
> |---|---|---|---|---|
> | 1 | Truck TR-04 | Rp 96 m | 31% more diesel issued than its trips burn | Reconcile TR-04's fuel log and tank |
> | 2 | Gang G3-02 | Rp 81 m | Overtime on 11 of 22 days, output flat | Stop standing overtime; plan the round inside the day |
> | 3 | Crew U4-02 | Rp 74 m | Paid 3 man-days a day above attendance | Reconcile payroll with the muster roll |
> | 4 | Division 5 fertiliser | Rp 58 m | Issued 14% above prescription | Issue against the block prescription only |
> | 5 | Urea, vendor V102 | Rp 47 m | Invoiced 18% above standard price | Hold V102 to contract; re-quote |
> | ... | | | | |

Opening row 1 shows Nova's diagnosis beneath the figures:

> **Most likely siphoning on driver D-11's shifts.** The gap appears on 9 of
> D-11's 10 shifts and on none of the other two drivers' shifts on TR-04, and
> it does not change with trip count. *Ruled out:* a tank leak, which would
> lose fuel on days without trips too. *To confirm:* dip the tank at the start
> and end of D-11's next three shifts.
> **Do:** rotate D-11 off TR-04 for two weeks (Transport Assistant, saves up
> to Rp 96 m a month) · cap each issue at planned-trip litres (Store Keeper).

---

## 1. The rules this build follows

1. **Rupiah ranks, efficiency reads.** A 55% node costing Rp 4 m matters less
   than a 92% node costing Rp 2 bn. Lists sort by leakage in IDR, never by %.
2. **One definition for every node.**
   - *Spend* S: CO postings to the node in the period.
   - *Crop lost* L: tonnes lost × FFB price, plus quality penalties, for losses
     the node owns (§2, ownership table).
   - *Necessary cost* N: the node's volume × the benchmark unit rate.
   - **Leakage = max(0, S + L − N)**
   - **Efficiency = min(100%, N ÷ (S + L))**, blank when S + L = 0.

   A parent's efficiency is ΣN ÷ Σ(S + L) over its children, never an average
   of percentages, so the estate figure and the node figures always agree.
3. **The benchmark is the estate's own best quartile among true peers.** Not a
   textbook rate. "Your best quarter of comparable blocks harvest at Rp X per
   tonne" is hard to argue with. Where a hard specification exists (vehicle
   litres per 100 km, payload, prescribed dose), the specification is the
   benchmark instead.
4. **Spending less is not efficiency.** A block that skips its weeding round
   is cheap this month and costly next year. Underspend with an overdue
   backlog is reported as **Deferred** (Rp), valued at the register's
   loss-per-day-overdue rates the ops scheduler already uses. It never raises
   efficiency, which is capped at 100%.
5. **Compare like with like.** Peers are formed on what the manager cannot
   change (bunch density, slope, distance to mill, vehicle class) and within
   the same month (Lebaran, wet season). Fertiliser is judged against the
   block's prescription, never kg/ha across blocks: weak blocks are prescribed
   more ([ec_fertiliser.csv](gis/data/synthetic/ec_fertiliser.csv) header).
6. **Every rupiah counted once, every rupiah traceable.** The ownership table
   in §2 assigns each loss to exactly one node. Every leakage figure drills to
   the postings, and every posting to its work order, trip, material document
   or PM order.
7. **Score only what someone controls.** Overhead is shown per hectare and
   left out of the efficiency score and the top ten.
8. **The old way is budget variance.** The baseline is the CO budget-versus-
   actual report the client already runs. The headline test (§7) is how many
   planted leaks each method finds, and how many false alarms it raises.
9. **Recovery is not discovery.** Planted leaks have an answer key; the engine
   never reads it ([learn.py](gis/models/learn.py) `ANSWER_KEYS`).
10. **Nova explains; it never measures.** Every figure on screen is the
    server's. Nova chooses among listed causes and actions, cites the checks,
    and is audited. With the model off, the window still works (§5).

---

## 2. The nodes

The tree is **Estate → Function → Unit**, monthly. Ten functions; nine scored.

| Function | Unit | Volume | Benchmark (necessary unit rate) | Peers |
|---|---|---|---|---|
| **Harvesting** | Block (crew as a second cut) | Tonnes weighed | P25 Rp/t among peers, same month | Bunch-density tercile × slope tercile × distance tercile |
| **FFB transport** | Vehicle (route, driver as cuts) | Tonne-km | Spec: litres/100 km × diesel price, payload at full load; P25 driver-hours per trip among the class | Vehicle class |
| **Fleet maintenance** | Vehicle | Trips | P25 Rp per 1,000 trips within class, age-adjusted | Vehicle class × age band |
| **Fertiliser** | Block × round | Prescribed kg | Prescribed kg × standard price + application man-days at register rate × man-day cost | None needed: the prescription is the standard |
| **Field upkeep** | Block × activity (prune, circle weed, path, spray) | Ha or palms done | Register rate (`prune_palms_per_man_day` etc.) × man-day cost + chemicals at dose × standard price | None needed |
| **Pest & disease** | Block | Palms treated where the census calls for it | Register rate × man-day cost + material at dose | None needed |
| **Workforce** | Crew | Crew-days present | Zero idle paid man-days beyond rain-out days | Crew type |
| **Stores** | Material | Value issued | Standard price (MBEW `verpr`), recommended stock from `gis/stores.py` | None needed |
| **Roads** | Road segment | Km maintained | P25 Rp/km/yr among segments of the same condition class | Condition class |
| **Overhead** | Division | Planted ha | Shown, not scored | |

The estate is young and uniform: palm age is 7-11 years with 149 of 291
blocks at 10, and slope runs 1.1-5.5°. Fixed age and slope bands would put
everything in one bucket, so peers use terciles of what actually varies. A
peer group under 8 units drops its weakest dimension (slope, then distance);
the payload records which was dropped.

### What each node leaks, and its cause

Each leakage figure splits into named drivers. The largest driver is the
"Why" on screen; the driver's template is the "Do".

| Function | Driver | Measured as | Source |
|---|---|---|---|
| Harvesting | Rate gap | (man-days − tonnes ÷ peer P75 t/man-day) × man-day cost | harvest orders, harvester-day |
| | Overtime | overtime premium paid on days output was at or below the crew's median | CO wage postings |
| | Loose fruit left | `loose_fruit.py` gap, repriced from the register | existing module |
| | Late rounds | days past round × `harvest_loss_pct_per_day_overdue` × crop value | `ec_rotation`, ops |
| FFB transport | Fuel gap | diesel issued to the vehicle − trip km × spec | MM 201 fuel issues, weighbridge |
| | Part loads | trips below 80% payload × cost per trip | weighbridge |
| | Shrinkage | EPMS bunches × ABW − net kg, priced (the shrinkage model's flagged trips) | `models/shrinkage.py` |
| | Queue and FFA | `ffa.py` queue and afternoon cost | existing module |
| Fleet | Breakdowns | PM cost above class P25 + downtime hours × `vehicle_hire_idr_per_hour` | `ec_pm_orders` |
| Fertiliser | Over-issue | (issued − prescribed kg) × standard price | `ec_fertiliser` |
| | Application labour | man-days above standard | CO wage postings |
| | Late application | lag days, **flagged, not priced** (the yield response is too uncertain to put a number on) | `ec_fertiliser` |
| Upkeep | Labour over standard | man-days − done ÷ rate | upkeep orders |
| | Chemical over dose | `herbicide.py` excess | existing module |
| | Rework | the same activity repeated inside half its interval | upkeep orders |
| Pest | Untriggered work | treatments on blocks with no census trigger | pest orders, census |
| Workforce | Paid idle | wages for present crew-days with no work order, beyond rain-out days | attendance, CO |
| | Pay over attendance | man-days paid − man-days present | CO, attendance |
| Stores | Price variance | (invoice price − standard) × quantity, rush buys included | MM POs |
| | Write-offs | movements 551 and negative 701/702 at standard | MATDOC |
| | Holding | value above recommended stock × `holding_cost_pct_yr` | `gis/stores.py` |
| Roads | Repeat repairs | culvert repairs beyond the first in 12 months | road PM orders |
| | Billing over length | contractor km billed − segment length | road PM orders |

### Who owns which loss

| Loss | Owned by | Never counted at |
|---|---|---|
| Loose fruit not collected, late-round crop | Harvesting block | Workforce, even when absence caused it (the cause line may say so) |
| Shrinkage, queue FFA, part loads | FFB transport | Harvesting |
| Diesel issued but not burned | FFB transport (the vehicle) | Fleet |
| Breakdown cost and downtime | Fleet | Transport |
| Material price above standard | Stores | Consuming blocks, which are charged at standard |
| Material used above dose or prescription | Consuming block | Stores |
| Paid idle man-days | Workforce | Activity nodes, which own only man-days on a work order |

---

## 3. The SAP records, and what fills the gaps

The EC export carries no FI/CO data. Everything below is generated, shaped as
the SAP extract that replaces it.

### The files

| File | SAP shape | Holds |
|---|---|---|
| `ec_fi_gl_accounts.csv` | SKA1 / cost elements | ~20 accounts: harvest basic wage, harvest premium, overtime, upkeep wages, fertiliser, agrochemicals, diesel, spare parts, external repair, road works, overhead, FFB revenue |
| `ec_co_cost_centres.csv` | CSKS | Seven divisions, workshop, transport pool, store, generators, office |
| `ec_co_orders.csv` | AUFK | One internal order per block (how most SAP plantation setups carry a block), one per vehicle, one per road segment |
| `ec_co_postings.csv` | ACDOCA / KSB1-KOB1 line items | `belnr, budat, racct, rcntr, aufnr, block_code, vehicle_id, segment_id, wsl, quantity, unit, src_type, src_id` |
| `ec_co_budget.csv` | CO plan version 0 (COSP) | Monthly budget per cost centre, order and account: the client's own baseline |
| `ec_hr_wage_types.csv` | Payroll results by wage type | Per crew-month: basic, premium, overtime, man-days paid |
| `ec_road_orders.csv` | PM orders on roads | Grading and culvert work with km billed and cost |
| `ec_fi_leak_key.csv` | none (**answer key**) | One row per planted leak and decoy: node, driver, months, planted IDR, true cause (a §5 candidate id), the mechanism planted so the checks can tell it apart |

`ec_block_costs.csv` is rewritten from the ledger's block roll-up for
2025-01..2025-05; its forward months stay as they are. Block margin and the
map's cost metrics then agree with the new window by construction, the way
`build_operations` rewrites `ec_upkeep.csv`.

### How the ledger is posted

Bottom-up. Each source row posts once:

| Source | Posts |
|---|---|
| Harvest, upkeep, pest orders (`man_days_actual`) | Basic wage to the block order at `man_day_cost_idr` |
| Harvester-day rows | Premium on bunches above `harvest_bunches_per_man_day` at `premi_idr_per_bunch` |
| Attendance present with no order that day | Basic wage to the division cost centre, account "standby" |
| MM 201 issues | Material at `verpr` to cost centre and block order |
| MM 261 issues and PM orders | Parts and external repair to the vehicle order |
| Fuel issues | Diesel to the vehicle order. **Correction:** fuel issues carry a division cost centre but no vehicle (1,171 issues since 2025-01-01, `aufnr` empty). Division totals already match weighbridge diesel (about 27 k L a month each), so the generator splits each division's issues across its vehicles' trip litres and stamps the vehicle, as a real fuel log would |
| Road orders | Road works to the segment order |
| Overhead | Fixed monthly postings to the office and division cost centres |
| Weighbridge net kg | FFB revenue at `ffb_price_idr_kg`, for cost-per-kg against price |

Target band: the unplanted ledger lands at 28-32 M IDR/ha/yr, the band
`COST_IDR_HA_YR` already uses.

### What the generator plants

Leaks the engine must find, and decoys it must not report as leaks.

| Planted | Size | Must come back |
|---|---|---|
| Fuel gap on one tractor-trailer | 31% more diesel issued than burned, 3 months | Top 3 in those months; driver = fuel gap |
| Standing overtime in one harvest gang | Overtime on ~50% of days, output flat | Gang in the top 5; driver = overtime |
| Pay above attendance in one upkeep crew | 3 man-days a day paid above present | Workforce top 3; driver = pay over attendance |
| Division over-issuing fertiliser | Issued 14% above prescription, both rounds | Every block in that division flagged; others not |
| Vendor price above standard | Urea invoiced 18% above `verpr`, 3 months | Stores top 3; driver = price variance |
| Spray team over dose | 1.3× dose for 2 months | Upkeep flag on its blocks; driver = chemical over dose |
| Breakdown-prone truck | The oldest dump truck at 3× class repair cost | Fleet top 1 |
| Repeat culvert | One segment, 4 repairs in 12 months | Roads top 1 |
| Grading billed over length | One contractor bills 1.4× segment length | Roads top 3 |
| Already in the world | D-07 shrinkage (2025-03-10..23), loose fruit gap, queue FFA | Priced and listed, matching the existing panels to the rupiah |
| **Decoy:** high-cost terrain | Steep, dense, far blocks cost more per tonne honestly | Not in the top 20; efficiency within ±5 points of the estate median |
| **Decoy:** weak blocks prescribed more | Existing confounding in `ec_fertiliser.csv` | No fertiliser leak where issue = prescription |
| **Decoy:** Lebaran dip | Mar-Apr unit costs rise estate-wide | No month-wide spike in leakage |
| **Decoy:** skipped upkeep | One crew skips circle weeding for 2 months | Shows as Deferred, efficiency not above its peers |

`ec_fi_leak_key.csv` and any `planted_*` column join `learn.ANSWER_KEYS`.

### Budget

The budget is generated the way estates set it: last year's actual per
hectare plus inflation, spread by month. Planted leaks therefore sit partly
inside budget. Two leaks (the vendor price, the fuel gap) are hidden by a
generous budget line, and one decoy (the high-cost terrain) shows as over
budget. That is the client's current view, faithfully reproduced, and the
comparison in §7 depends on it.

### Keeping every existing feed as it is

`gis/build_finance.py` follows `build_materials.py`: its own seed
(`f"{SEED}:finance"`), run after `build_materials`, reading every ledger off
disk. It hashes every CSV before and after and fails if anything changed
except `ec_block_costs.csv`. Planted leaks that touch existing behaviour (the
fertiliser over-issue, the spray dose, the urea price) are posted **only in
the new FI/CO files**, with the matching MM rows added as new documents, never
edits. If that cannot be kept for the fertiliser over-issue, it moves to a
generator flag in `build_synthetic.py` and every planted-truth check in
buildplan_ml.md and buildplan_stores.md is re-run.

---

## 4. The engine

`gis/finance.py`, in the voice of `gis/stores.py`: a sentence first, warmed on
its own thread, recomputed on a register edit (no model fitting, so a reprice
is fast).

1. **Load** the ledger and the volume feeds. Build the node table: one row per
   unit-month with S, volume, peer key.
2. **Benchmark.** Per function, per month: the P25 of unit cost across the
   peer group (or the spec). Store which peers and how many.
3. **Losses.** Crop lost per owner from `loose_fruit`, `ffa`, `models/shrinkage`
   and the rotation state, all repriced through the register. Their own
   placeholder constants (`FFA_IDR_PER_KG_POINT`, `TRANSPORT_IDR_KG_KM`) move
   into the register so a panel and this engine can never disagree.
4. **Leakage, efficiency, deferred** per §1, then the driver split per §2.
5. **Materiality.** Leaks under `leak_materiality_idr` (default Rp 5 m a
   month) stay in the node tables but never in the top ten.
6. **Roll-up** by ΣN ÷ Σ(S + L): unit → function → division → estate.
7. **Calibration check.** On the unplanted ledger, median node efficiency must
   land at 80-92%. Lower means the benchmark is too strict (P25 of a noisy
   month is optimistic); the fix is a three-month rolling unit cost, not a
   different quantile.

Not marked ML. It is a benchmark with peer groups, not a fitted model, and the
`ML` set in `features.py` stays as it is.

---

## 5. Cause and action: the Nova layer

The engine says *where* and *how much*, and names the driver ("fuel gap").
That is not yet a cause. A fuel gap can be a leaking tank, siphoning on one
driver's shifts, or a vehicle doing unrecorded non-FFB work, and each needs a
different fix. Telling them apart means weighing several pieces of evidence
against each other, which is what Nova is for. The contract is the one
[gis/briefings.py](gis/briefings.py) already follows:

> **The server computes and checks; Nova weighs the evidence and writes.**

Nova never measures anything. It chooses among causes the server lists, cites
checks the server ran, picks actions from a vetted playbook, quotes savings
the server computed, and every figure it writes is audited against what it
was given.

### What Nova does, and does not do

| Nova does | Nova does not |
|---|---|
| Ranks the candidate causes for a leak driver as likely, possible or ruled out, citing check ids | Invent a cause outside the candidate list (it may propose `other`, shown as unvetted) |
| Writes the cause in two sentences a manager would say | Compute a figure, a saving or a percentage |
| Names the one field check that would confirm the cause | Claim the cause is confirmed: it is "most likely" until someone checks |
| Picks and orders actions from the driver's playbook, tailored to this unit (the vehicle, the driver, the dates) | Write to SAP, or decide an action without a person approving it |
| Drafts the `cost_action` document text (via `briefings.artifact_prose`) | See the answer key |

### Evidence: cause checks per driver

For every driver the server runs a fixed set of deterministic checks and hands
Nova the results with their ids. The checks are designed so the candidate
causes leave different traces. Phase 2 covers the first three functions:

| Driver | Candidate causes | Checks the server runs |
|---|---|---|
| Fuel gap | tank or line leak · siphoning · unrecorded non-FFB work · engine worn past spec | gap on trip days vs no-trip days · gap by driver on this vehicle · issue size vs tank capacity · gap before and after the last PM order · km from trips vs km implied by litres · same-class vehicles on the same routes |
| Overtime | standing overtime habit · rounds too big for the crew · late start · rain-shortened mornings | overtime on dry vs wet days · overtime vs bunches cut · present vs establishment · start times from the orders · day-of-week pattern |
| Rate gap (harvest) | crew short-handed · long walk from home block · rain · paths overdue · bunch density lower than plan | headcount vs establishment · km from home block · rain on the day · `path_upkeep` days overdue · learned crew speed (`models/rates`, never `skill`) |
| Late rounds | crew capacity · rain-outs · transport not available | crew-days needed vs available · rain-out days · dispatch adherence on the block |
| Shrinkage | theft in transit · fruit left on platform or vehicle · weighing error · ABW wrong for the block | concentration by driver, vehicle, route · departure hour · same vehicle with other drivers · block's ABW against its peers |
| Part loads | dispatch splitting small lots · vehicle too big for the route · waiting on a single block | load % by route and hour · blocks per trip · vehicle class vs route |
| Breakdowns | end of life · repeat fault, poor repair · overloading · rough roads on its routes | same fault within 60 days · mean load vs payload · road condition score on its routes · age vs class |
| Pay over attendance | ghost names · muster not recorded · contract labour posted to the crew | gap constant day to day · gap on days the crew was absent · gap on Sundays and holidays |

Phase 4 adds the same table for fertiliser, upkeep, pest, stores and roads
(over-issue: bag rounding vs programme not updated vs diversion; price
variance: rush buy vs vendor above contract vs standard price outdated; and
so on).

### Actions: the playbook

`gis/finance_playbook.py`, pure data like `features.py`. Per driver: three to
six actions, each with an id, owner role, the SAP document it becomes (if
any), and the metric to watch afterwards. For example, for fuel gap:

| Id | Action | Owner | Watch |
|---|---|---|---|
| `fuel_reconcile` | Reconcile the vehicle's fuel log against its trips for the last 30 days | Workshop Head | fuel gap litres |
| `fuel_driver_rotate` | Rotate the driver off the vehicle for two weeks and compare | Transport Assistant | fuel gap by driver |
| `fuel_tank_inspect` | Pressure-test the tank and lines; raise a PM order | Workshop Head | fuel gap litres |
| `fuel_issue_cap` | Cap each issue at the litres its planned trips need | Store Keeper | issue vs plan |

The server attaches the expected saving to each action: the driver's monthly
leakage. Nova may quote it and may not change it.

### The call

`gis/diagnosis.py`, in the voice of `gis/briefings.py`:

- **Grounding**: the unit, its S, L, N, efficiency, peers, the driver split,
  the check results for the top two drivers, the candidate causes, the
  playbook, past `cost_action` decisions on this unit with the cause the
  manager recorded when closing them, and a provenance tag on every block.
  Capped at `reasoning.MAX_CONTEXT_CHARS`.
- **Prompt**: `prompts/estate_leak_diagnosis_system.txt`, which states the
  rules in the table above.
- **Model**: Nova Pro (main tier), `reasoning.json_chat`, temperature 0.2.
- **Output**:
  ```json
  {"headline": "...",
   "cause": {"id": "siphoning", "confidence": "likely", "evidence": ["c_by_driver", "c_trip_days"], "sentence": "..."},
   "ruled_out": [{"id": "tank_leak", "evidence": ["c_trip_days"], "sentence": "..."}],
   "confirm": "...",
   "actions": [{"id": "fuel_driver_rotate", "text": "...", "owner": "Transport Assistant", "when": "this week"}]}
  ```
- **Validation**: cause, evidence and action ids must exist in the grounding;
  `reasoning.audit_figures` finds no unverified figure. On a failure, one
  retry at temperature 0; after that the deterministic template stands.
- **Cache**: `reasoning.TTLCache`, 24 h, keyed on unit, month, the register's
  hash and the decisions count, so a register edit or a closed action
  refreshes it.
- **Warm**: the default month's top ten are diagnosed on a background thread
  after the engine warms, so the first click in a demo is instant.
- **Language**: `lang=en|id`. In Bahasa Indonesia the model must keep figures
  in the server's formatting, because `audit_figures` reads `1,240` not
  `1.240`. A test covers that.
- **Cost**: about 4 k tokens in and 0.7 k out per diagnosis, roughly half a
  US cent on Nova Pro at the prices in [evals/model_prices.yaml](evals/model_prices.yaml);
  warming a month's top ten costs about six cents.

### When the model is down

The **Why** and **Do** columns on the first screen are always the
deterministic templates (largest driver, first playbook action), so the
window never depends on Nova. The Diagnosis section in the unit detail
returns `reasoning.unavailable(...)` and says so in one line.

### On screen

The unit detail gains a **Diagnosis** section: the headline, the likely cause
with its evidence chips (each opens the check's figures), the ruled-out
causes, the confirm step, and the actions, each with Draft action. An audit
pill shows "figures checked" as the block brief does. A row in Top leaks
whose diagnosis is ready shows Nova's headline in place of the template.

### Learning from closed actions

When a manager closes a `cost_action`, they record the cause they actually
found (one of the candidates, or free text). That record goes into the next
diagnosis of the same unit and the same driver elsewhere on the estate as
"past findings". No fine-tuning: the estate's own findings, retrieved.

### How it is judged

The answer key gives each planted leak its true cause (a candidate id) and
the mechanism the generator planted to leave the right trace in the checks.
`evals/leak_diagnosis_cases.yaml`, run by the eval harness, three runs per
setting, medians compared (the run-to-run variance in
[evals/BASELINE.md](evals/BASELINE.md) applies to Nova too):

| Measure | Bar |
|---|---|
| Likely cause = planted cause | ≥ 10 of 12 planted leaks |
| True cause ruled out | 0 |
| Unverified figures across the top ten | 0 |
| Invalid ids after the retry | 0 |
| Decoy units: diagnosis says "within peer norm" rather than naming a leak | 4 of 4 |
| Same case set with checks removed (cause list only) | clearly worse: proves the checks, not the model's priors, do the work |

The last line matters: if Nova scores as well without the evidence, it is
guessing from the driver name and the layer is decoration.

---

## 6. Wiring

### The rail

A new domain, first in `DOMAINS` ([features.py:44](gis/features.py#L44)):

```python
("cost", {"label": "Cost efficiency"}),
```

with a rupiah glyph in `DOMAIN_ICON` ([rail.js:16](command_static/src/shell/rail.js#L16)).
Its features, short labels per the UI copy rule:

| Feature | Label | Question | Opens |
|---|---|---|---|
| `money_leaks` | Where money leaks | Where are we losing money, and what stops it? | Window `leakage` |
| `node_efficiency` | Efficiency map | Which blocks run closest to their best peers? | Map metric `efficiency_pct`, docked ranked list |
| `budget_vs_actual` | Budget vs actual | Where is spend off budget? | Side panel |
| `cost_margin` | Block margin | Which blocks earn their keep? | Moved from Commercial; reads the new ledger |

Rail hint on `money_leaks`: the month's leakage, e.g. "Rp 612 m".

### The window

A `leakage` entry in `POPUPS` ([registry.js:118](command_static/src/panels/registry.js#L118)),
built by `command_static/src/panels/leakage.js` on the `storesPopup` contract.

| Group | Sections |
|---|---|
| **Now** | Top leaks (the ten rows, each with Draft action and Show on map) · At a glance (leakage, efficiency, deferred, six-month trend) |
| **Nodes** | Harvesting · Transport · Fleet · Fertiliser · Upkeep · Pest · Workforce · Stores · Roads · Overhead. Each is a ranked table: unit, spend, necessary, leakage, efficiency, deferred, top driver. A row opens the unit: a waterfall from necessary cost to actual by driver, the Diagnosis (§5), its peers on one strip, the monthly trend, and its documents |
| **Ledger** | Postings (KSB1-style, filter by account, cost centre, order) · Budget vs actual |
| **Trust** | Planted against recovered · Leaks vs budget variance · Diagnosis accuracy · Benchmarks used · How it works |

Month selector defaults to the last closed month (2025-04), with May 2025
month-to-date. Charts are inline SVG like the stores window. Panels carry the
short label, the question and the data; method prose lives only in How it
works.

### Map

Two metrics in the colour menu: `efficiency_pct` and `leakage_idr_per_ha`
(per block, harvest + fertiliser + upkeep + pest functions). Show on map from
any node table hands the blocks over.

### Backend

| File | Change |
|---|---|
| `gis/build_finance.py` | New. §3: files, posting, planted leaks, answer key, reconciliation and hash checks; `ec_block_costs.csv` rewrite; results under `finance_checks` in the manifest |
| `gis/finance.py` | New. §4 engine and packaging, `warm()`, `reload()`, `assumption_changed()` |
| `gis/diagnosis.py` | New. §5: cause checks per driver, grounding, the Nova call through `reasoning.json_chat`, id validation, figure audit, retry, template fallback, cache, top-ten warm-up |
| `gis/finance_playbook.py` | New. Pure data: candidate causes, check ids and actions per driver, with owner role, SAP document and watch metric |
| `prompts/estate_leak_diagnosis_system.txt` | New. The rules of §5 |
| `evals/leak_diagnosis_cases.yaml` | New. One case per planted leak and decoy, the true cause from the answer key; run by the eval harness, never by the server |
| `gis/models/learn.py` | Answer-key columns added to `ANSWER_KEYS` |
| `gis/loose_fruit.py`, `gis/ffa.py`, `gis/layers.py` | Placeholder rates read from the register |
| `gis/assumptions.py` | A `finance` group (below) |
| `gis/features.py` | `cost` domain; `sap_co`, `sap_fi`, `sap_hr` systems; requirements below; `cost_ledger` now names ACDOCA |
| `gis/readiness.py` | A `finance` row: needs, evidence, what it degrades to (block margin on allocated cost), the ask |
| `gis/decisions.py` | `cost_action` kind, with a close step that records the cause found |
| `gis/copilot.py` | Four tools |
| `gis_router.py` | Endpoints, below |

### Endpoints

| Endpoint | Returns |
|---|---|
| `GET /gis/finance/summary?month=` | Headline sentence, leakage, efficiency, deferred, top N leaks with why and do (Nova's headline where a diagnosis is cached, the template otherwise), trend |
| `GET /gis/finance/nodes?function=&month=&division=` | Unit rows with S, L, N, leakage, efficiency, deferred, top driver, peer key |
| `GET /gis/finance/node?id=&month=` | Driver waterfall, peers, monthly trend, document list |
| `GET /gis/finance/diagnosis?id=&month=&lang=&refresh=` | Nova's cause, ruled-out causes, confirm step and actions, with the check results, the audit and the grounding; or `available: false` and the template |
| `GET /gis/finance/ledger?account=&cc=&order=&from=&to=&limit=` | Postings, paginated |
| `GET /gis/finance/budget?month=` | Budget vs actual by cost centre and account |
| `GET /gis/finance/accuracy` | Recovery checks, leaks-vs-budget comparison, benchmarks used, calibration |

### Assumption register: a `finance` group

| Key | Default | Kind |
|---|---|---|
| `ffb_price_idr_kg`, `man_day_cost_idr` | existing | existing |
| `premi_idr_per_bunch` | 250 | assumed, agree with HR |
| `overtime_multiplier` | 1.5 | assumed, agree with HR |
| `vehicle_hire_idr_per_hour` | 350,000 | assumed |
| `ffa_idr_per_kg_point` | 25 (moved from `ffa.py`) | placeholder, agree with the mill |
| `transport_idr_per_kg_km` | 4.6 (moved from `layers.py`) | placeholder |
| `benchmark_quantile` | 25 | assumed |
| `min_peer_group` | 8 | assumed |
| `leak_materiality_idr` | 5,000,000 a month | assumed |
| `part_load_threshold_pct` | 80 | assumed |
| `use_leak_diagnosis` | 1 | 1 on, 0 off: off shows templates only, for a demo without network |
| `diagnosis_lang` | en | en or id |

### Decisions: `cost_action`

| When | Lines | Owner |
|---|---|---|
| Draft action on a top-ten row, a node, or a diagnosis action | Node, driver, leakage per month, the likely cause and its confirm step, the action, expected saving, review date; prose by `briefings.artifact_prose` | By function: Estate Manager (harvesting, workforce), Assistant Manager of the division (upkeep, fertiliser, pest), Workshop Head (fleet, transport), Store Keeper (stores), Head Office Procurement (vendor price) |
| Close an action | The cause actually found (a candidate id or free text), the saving seen | The same owner; feeds "past findings" in later diagnoses |

The Did-it-work panel gets a cost reading: the node's leakage in the months
after the decision date against before, using replay inside the window as ops
outcomes already do.

### Copilot

The model never computes.

- `where_losing_money`: top leaks, filter by function, division, month.
- `node_efficiency`: one node's S, L, N, drivers, peers.
- `explain_leak`: the documents behind one leak driver.
- `diagnose_leak`: the §5 diagnosis for one unit, cached or fresh. The copilot
  relays it; it does not re-diagnose.

### Tests

`command_static/test/finance.mjs`, Playwright, same harness as `stores.mjs`:

- every `/gis/finance/*` endpoint returns 200
- the ledger reconciles: wage postings = man-days on orders + standby, to the
  man-day; material postings = MM issues × `verpr`, to the rupiah; fuel
  postings per division = MM fuel issues
- `ec_block_costs.csv` Jan-May = ledger roll-up per block-month
- no answer-key column appears in any payload
- recovery: every planted leak back per §3; every decoy off the list
- leakage of each existing module (loose fruit, FFA, shrinkage) equals that
  panel's own figure
- estate efficiency = ΣN ÷ Σ(S + L) over the units
- a register edit to `ffb_price_idr_kg` changes crop-loss leakage and nothing
  else
- the window renders every section with no console errors; Draft action
  writes a `cost_action`
- every diagnosis returned is either `available: false` with the template,
  or has only valid cause, evidence and action ids and no unverified figure
- no answer-key value appears in any diagnosis grounding
- `use_leak_diagnosis` off: every row shows its template, the window renders,
  no model call is made
- `lang=id`: figures stay in server formatting and the audit passes
- register and decisions reset afterwards

`smoke.mjs`, `ops.mjs`, `models.mjs` and `stores.mjs` must pass unchanged:
that proves the seed isolation.

Diagnosis accuracy (§5 table) is measured by the eval harness, not by
`finance.mjs`: it costs model calls and varies run to run.

---

## 7. The test that decides whether it is worth showing

**Leaks found against budget variance.** On the planted world, run the
client's current method (cost centres and orders over budget by more than 10%)
and the leak engine side by side:

| | Budget variance | Leak engine |
|---|---|---|
| Planted leaks found (of 9 + 3 existing) | counted | counted |
| Decoys flagged as problems (of 4) | counted | counted |
| Rupiah of planted leakage located to the unit | counted | counted |

The Trust section shows this table. If the engine does not find clearly more
leaks with fewer false alarms than budget variance, the benchmark or the
drivers are wrong and the window is not demo-ready. This is the answer to "we
already have cost reports".

---

## 8. What it cannot know yet

- **Their CO design.** Whether a block is an internal order, a WBS element or a
  cost centre, and whether assessment cycles move cost between them. The
  engine must read primary postings before settlement.
- **Best quartile of one estate is still one estate.** If all of EC is
  inefficient, its own top quarter is too. EA and EB are already on the map;
  benchmarking across the three is the natural next step once each has a
  ledger.
- **Payroll is simplified.** Flat day rate plus one premium tier. Real premium
  schemes are tiered by bunch count and terrain and differ by estate.
- **Late fertiliser and the yield gap are not priced.** Both are real money,
  but the yield response is too uncertain to put a defensible number on. They
  are flagged, not counted.
- **Five months.** Benchmarks within a month rest on one observation per unit;
  a real 24-month extract makes them steadier and makes a seasonal view
  possible.
- **A diagnosis is a hypothesis.** Nova can say which cause the records point
  to; only a field check proves it. That is why every diagnosis carries a
  confirm step and every action records the cause found when it is closed.
- **The playbook is ours, not theirs.** The actions are written from general
  estate practice. The client's SOPs, ingested into the existing Bedrock
  Knowledge Base (`rag_sources.py`), would let each action cite their own
  procedure. That needs the documents from them.
- **Planted causes are clean.** The generator plants one mechanism per leak.
  Real leaks mix causes, and the eval bar on synthetic data overstates how
  often Nova will be right on the client's.

**Real data that makes it yours.** Standard SAP reports for company code and
plant EC, 24 months:

| Report | Gives |
|---|---|
| KSB1 / KOB1, or ACDOCA | Actual line items by cost centre and order, with sender documents |
| KP06 / COSP, version 0 | Budget |
| KS03 / KO03 masters, cost element list | Cost centres, orders and their block or vehicle mapping |
| Payroll wage-type summary per gang and month | Basic, premium, overtime, man-days paid |
| Fuel log or fuel card extract | Litres per vehicle per day |

---

## 9. Order of work

| Phase | Work | Why here |
|---|---|---|
| 1 | `build_finance.py`: GL, cost centres, orders, wage types, road orders, postings, budget, fuel split, planted leaks and key; reconciliation and hash checks; `ec_block_costs.csv` rewrite; features and readiness rows | Every number is judged on it, and the seed isolation has to hold first |
| 2 | Engine core and the three biggest functions: harvesting, FFB transport, fleet. Register group; existing loss modules repriced off it; `summary`, `nodes`, `node` endpoints; recovery for their leaks | That is where most of the money is |
| 3 | The rail domain and window v1: Top leaks, At a glance, the three node sections, unit detail, Postings; the map metrics. Why and Do from templates | The window has to stand without the model first |
| 4 | The Nova layer for the three functions: checks, playbook, `diagnosis.py`, prompt, the endpoint, the Diagnosis section, top-ten warm-up; the eval cases and a first run | **Demo cut line.** Where, how much, why and what to do, for three functions |
| 5 | Fertiliser, upkeep, pest, workforce, stores, roads, overhead, each with its checks and playbook | Same engine, more drivers |
| 6 | `cost_action` with the close step, past findings in the grounding, the Did-it-work reading, copilot tools | Closes the loop from finding to fix |
| 7 | Budget vs actual, the §7 comparison, the Trust group, `test/finance.mjs`, build status in this file | Needs everything |

**Cut line.** If time runs short, roads and pest go first. Nothing depends on
them, and their leaks are the smallest.

---

## 10. Things to get right

1. **Rupiah ranks; the percentage only reads.** Efficiency is ΣN ÷ Σ(S + L) at
   every level, so the % and the rupiah always tell the same story.
2. **Peers and prescriptions, not averages.** The decoys must stay off the
   list. One wrong "cut fertiliser on your weakest blocks" in front of a
   planter with thirty years in the field ends the conversation.
3. **Every rupiah once, every rupiah to a document.** The ownership table
   prevents double counting; the drill-down to SAP documents is what makes an
   experienced manager believe the number.
4. **The checks do the diagnosing; Nova weighs them.** If Nova does as well
   with the checks removed, it is guessing from the driver's name. The eval
   runs that comparison every time.
