Remaining	Blocker
3.2 forecast half of the yield choropleth	Partial. Trailing-mean forecast is a metric option, but the scrubber has no forward months because the export ends 2025-05-23.
3.3 fire and force majeure	None. Best head start of anything left.
3.4 harvest rotation	Ripeness pressure is computable from harvest dates. Gang assignment is not, no gang data in the export.
3.5 cost and margin	No cost data in the EC export.
3.6 contract position and vendors	Vendor coordinates do not exist.
3.7 vegetation anomaly	None. The only fully green row in the readiness panel.
3.8 replant age profile	No content. Every block is 7 to 11 years old.
3.9 labour, upkeep, pest, drainage	Mostly blocked, pest proxy has no variance.
4.2 action artifacts	None. Buttons currently write a DOM note and nothing else.
4.3 decision logging and audit view	None. Nothing is persisted at all today.


## Closed on 2026-09-10

| Line | Now | How |
|---|---|---|
| 3.7 vegetation anomaly | Done, and real | `gis/build_ndre.py` pulls the least-cloudy Sentinel-2 L2A scene over EC from the free Copernicus archive (S2B_54MVT_20250215, 7.6% cloud), crops the red-edge and NIR bands through a public raster service, masks cloud with the scene's own SCL band and averages NDRE inside each polygon. 291 of 291 blocks measured at 20 m. The metric badge flips from synthetic to real on its own when the file exists. |
| 4.2 action artifacts | Extended | The artifact was already drafted deterministically; it now also carries the instruction text a supervisor reads, written by Nova Lite from the document and its per-kind provenance note. A model failure leaves the artifact intact and unnarrated. |
| 4.3 decision logging and audit view | Extended | The log was already persisted; the panel now also writes the shift handover from it — what was decided, what is still open, what deteriorates untouched — on Nova Pro, with every figure audited back to the payload. |

Also added, outside the original plan:

- **Ask the map** (`POST /gis/ask`). A tool-calling agent over fourteen tools spanning every layer on the page. It answers in the estate's own figures and moves the map to match, and returns the full tool trace so any claim can be checked against the call that produced it.
- **The figure audit** (`gis/reasoning.audit_figures`). Every number and block label in generated text is looked for in the payload behind it; what is missing is reported on the response and shown in the UI. This is what stops a model from multiplying tonnes by a per-kilo price and presenting the product as a fact. The copilot gets one self-correction pass when its own audit fails.
- **Metric agreement** (`GET /gis/metrics/compare`). Rank correlation between any two metrics plus the blocks in the bottom fifth of both. Satellite vigour and recorded yield rank this estate almost independently (rho 0.10), so the thirteen blocks weak on both are the corroborated ones.
- **The readiness interview** (`/gis/readiness/interview`). A red row becomes the questions worth asking in the room; the client's answer is scored against the measurement and recorded. A proposal never overwrites a measured status, a low-confidence verdict is withheld, and a downgrade needs high confidence.
- **The duty officer brief** (`GET /gis/fire/brief`). Posture, priority order and orders over the live fire assessment, with the real-versus-invented split carried in every line.

### Still open from the table above

3.2, 3.4, 3.5, 3.6, 3.8 and 3.9 are unchanged: each is blocked on a feed the EC
export does not carry, and the readiness register states which one and what the
layer degrades to without it. The interview endpoints exist to turn those rows
into answers from the client rather than assumptions about them.


## Closed on 2026-09-18

An audit of the plans against the running app found the backend whole (every
endpoint 200, every test green) and three things the plans did not say:

1. **The app threw away real client data.** `EC_oph.csv` carries a
   `loose_fruits` column on every harvest record, 11,571,395 fruits over
   183,850 rows and all 291 blocks. The OPH loader never read it; the readiness
   register said "the export carries no counts"; the leave-behind asked the
   client for a feed they had already sent.
2. **Five live features were dead buttons.** `peer_yield`, `upkeep` and
   `margin` are map metrics, `readiness` and `ask` are drawers; none had a
   panel renderer, so the rail fell through to "not yet built" for all five.
   The smoke test walked them and passed, because it only checked the body was
   non-empty.
3. **Seven of the eight planned features had their data on disk already.**
   The eighth cannot be built for a reason worth stating.

| Line | Now | How |
|---|---|---|
| Loose fruit recovery | Done, **real** | `gis/loose_fruit.py`. Loose fruit per bunch by block and month from the client's own counts: 1.41 estate-wide, 86% of the 1.64 the best quarter of blocks reach; Division 3 at 1.08. A real map metric `loose_per_bunch`. The readiness row and manifest corrected; the ask is now the weight at the platform, which the export does not carry. |
| Realised cutting interval | Done, intervals real | `gis/cutting_interval.py`. The round measured visit to visit, because a block is worked two consecutive days per visit and raw day gaps are bimodal: about 11 days in January–February, 18 in April–May, 43 blocks past 14 days at the anchor. The buildplan's "5.2 to 7.7" was calendar days per cutting day, a work-rate figure, not the round. Found on the way: `ec_rotation.csv`'s `last_harvest_date` disagrees with the real last cutting day on 268 of 291 blocks, so the rotation panel's "days since" is synthetic and this panel shows the real gap beside it. |
| Herbicide adherence | Done | `gis/herbicide.py`. MM 201 issues joined to spray orders on AUFNR. 131 of 291 blocks past their 100-day round; 52% of planned hectares sprayed, 41% rained off on real ≥15 mm days; 1.08 L/ha glyphosate against a 1.0 dose, which is the planted handling loss read back exactly. |
| FFA against dispatch delay | Done | `gis/ffa.py`. 0.12 FFA points per hour on the ticket, recovered against the planted 0.12 and said so; 48.7% of the ticket is the mill queue; nothing crosses the 3% line, which is a placeholder to agree with the mill. |
| Bunch weight trend | Done | `gis/abw_trend.py`. Not moving: 8.01 kg every month, every block slope inside trip noise, and the panel says so rather than ranking noise. The calibration caveat from `ec_abw.csv` sits above the fold: the level is an artefact of landing the estate at 23 t/ha, and the real counts imply a yield that is impossible at a normal ABW. |
| Canopy early warning | Done, half real | `gis/pest_warning.py`. 65 blocks the satellite flags that the invented census does not, 14 of them under 70% inspected. Rank agreement between real vigour and generated incidence is +0.34, the wrong sign for a disease, because `gen_pest` never read the NDRE; stated on screen as what a real census would be tested against. |
| Collection coverage | Done, honestly | `gis/collection.py`. Same-day evacuation is 100% by construction: the generator seeds identical dates across the three feeds, so the panel says so first and shows what the ledger can carry: 37% of the crop leaves after noon into a 0.4 h longer queue, route R-01 runs 73% full. The ask names the two timestamps the client records and did not export. |
| Grading by harvester | **Stays planned, and says why** | The real grading is 99.82% ripe across 183,850 rows; wet and pest-damaged-old are all zero, dirty has two non-zero rows. Nothing to attribute to a harvester until grading carries variance. The manifest note now carries the measured distribution. |

Also on this date: the five dead rail buttons route to the metric or the drawer
they stand for, with a ranked block list docked for the metric ones; the smoke
test now fails on a live feature that renders "not yet built"; seven copilot
tools over the new modules (44 in all); `npm test` runs `models.mjs`,
`stores.mjs` and the seven feature tests; the entry-module import cycle that
made Vite's HMR boot the page twice is broken.

The manifest reads **47 features, 46 live, 8 on the client's own data** (was 39
live, 4 on real data).
