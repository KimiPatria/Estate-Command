# System requirements — AI/ML GIS decision platform

Two deployment options, for a client evaluating what it would take to run this
in production:

- **Option A — cloud-hosted** in the client's own AWS account (recommended).
- **Option B — cloud-hosted app with an on-premise LLM**, for the case where
  no data may reach a managed AI service.

All figures below are **measured on the current build** unless marked as an
estimate. Where a number depends on the client's scale, the assumption is
stated so it can be corrected.

---

## 0. What actually consumes resources

The platform is light. There is no deep learning in the serving path and no GPU
anywhere in Option A. Three workloads, in order of cost:

| Workload | Nature | Cost |
|---|---|---|
| Web application + API | Python (FastAPI), stateless, request/response | Small, constant |
| Model retraining | Scheduled batch, CPU only — regressions, gradient-boosted trees, empirical-Bayes shrinkage, a greedy scheduler | Small, bursty |
| LLM inference | Text-to-SQL, the tool-calling assistant, short written briefs | Per-token service (A) or a GPU server (B) |

Everything the models learn from is tabular and small: work orders, attendance,
stock movements, purchase orders, weather, monthly production series. The
heaviest single file in the current build is a 6 MB production series.

### Measured retraining cost (one estate, 291 blocks, 143 days of history)

Measured on a 16-core laptop CPU, no GPU:

| Job | Wall time | Peak memory |
|---|---|---|
| Rain, workforce turnout, work completion, crew speeds — fit + backtest + validation | **14.6 s** | 243 MB |
| Stores: supplier lead time, consumption, safety-stock replay | **5.6 s** | 264 MB |
| Monthly FFB production forecast (SARIMAX + gradient boosting ensemble, walk-forward folds, conformal intervals) | **13.9 s** | 372 MB |
| **Full retrain, all models, one estate** | **~35 s** | **< 400 MB** |

This is the answer to "what does retraining need": **one ordinary CPU core-minute
and under half a gigabyte of RAM per estate.** Retraining runs nightly, off-peak,
in a scheduled container that exits when done. It does not run inside the web
application and never blocks a user request.

**Scaling rule (estimate):** cost is roughly linear in estates and in history
length. At 3 years of history instead of 5 months, assume ×3–5 per estate. A
nightly window of 2 hours on 4 vCPU therefore covers on the order of 100–200
estates even on the pessimistic end. If the estate count is much larger, the
job is embarrassingly parallel — split it across more containers.

**Not required:** GPU, Spark/EMR, Databricks, a data lake, a dedicated ML
platform. Deliberately excluded from the serving path: the foundation
time-series model we benchmarked (it did not beat the current ensemble, and the
only checkpoint that did is licensed non-commercial).

---

## 1. Assumptions to confirm with the client

Sizing below is given at three tiers because these are not yet known:

| Unknown | Used for |
|---|---|
| Number of estates and total blocks in scope | Retrain window, database size, map tile volume |
| Number of named users, and peak concurrent users | Application container count, LLM throughput |
| Years of history available for extract | Retrain time, storage |
| AWS region required for data residency, and which AI models are available there | Latency, model choice |
| Whether the SAP extract is a nightly batch or near-real-time | Integration pattern, network volume |

Tiers used below: **Pilot** = 1–3 estates, ≤ 50 users. **Regional** = 10–25
estates, ≤ 300 users. **Group** = 50+ estates, 1,000+ users.

---

## Option A — cloud-hosted in the client's AWS account

The platform is deployed as containers in the client's own VPC, beside the
existing SAP S/4HANA environment. No data leaves the account except deliberate,
outbound-only calls to free public data sources (satellite, weather, fire).

### A1. Compute

| Component | Pilot | Regional | Group | Notes |
|---|---|---|---|---|
| Web/API containers | 2 × (2 vCPU, 4 GB) | 4 × (2 vCPU, 8 GB) | 6–10 × (4 vCPU, 8 GB) | Serverless containers (ECS Fargate) or EC2; two minimum for availability, auto-scaled on CPU |
| Nightly retrain job | 1 × (2 vCPU, 4 GB), ~5 min | 1 × (4 vCPU, 8 GB), ~20 min | 2–4 × (4 vCPU, 8 GB), parallel | Scheduled task; exits when finished, billed only while running |
| Data-extract job | 1 × (2 vCPU, 4 GB) | same | 2 × (2 vCPU, 4 GB) | Pulls SAP/EPMS deltas and public data |

Application memory is dominated by cached map layers and model outputs, not by
model fitting. 4 GB per container is comfortable at pilot scale; 8 GB gives
headroom for many estates cached at once.

### A2. Data stores

| Store | Purpose | Pilot | Regional | Group |
|---|---|---|---|---|
| Managed PostgreSQL (RDS), with PostGIS and pgvector | Analytical store, block geometry, decision log, assumptions, chat search index | 2 vCPU / 8 GB, 100 GB SSD | 4 vCPU / 16 GB, 500 GB | 8 vCPU / 32 GB, 1–2 TB, read replica |
| Object storage (S3) | Extracts, satellite layers, model artifacts, logs | 50 GB | 250 GB | 1–2 TB |
| Optional document store + vector index | SOP/contract Q&A | — | Managed vector collection | Managed vector collection |

Multi-AZ on the database from Regional tier upward. Daily automated backups,
7–35 day retention, encryption at rest throughout.

Note: the demo's local single-file databases (decision log, chat index,
experiment tracking) are replaced by the managed PostgreSQL instance. This is
the one real change between demo and production, and it is configuration, not
a rewrite.

### A3. LLM service

Amazon Bedrock, called privately from inside the VPC (PrivateLink), so prompts
never traverse the public internet. Two tiers are used: a reasoning model for
the assistant and text-to-SQL, a cheaper model for short written output.

Load is human-paced — an estate manager asking a handful of questions per day,
not a machine loop. Estimate 3–10 k tokens per question including schema
context.

| Tier | Questions/day (estimate) | Notes |
|---|---|---|
| Pilot | 200–500 | Well inside default service quotas |
| Regional | 2,000–5,000 | Request a quota increase ahead of go-live |
| Group | 20,000+ | Provisioned throughput becomes worth pricing |

No infrastructure to size — it is per-token consumption.

### A4. Network and integration

| Flow | Direction | Protocol | Volume (estimate) |
|---|---|---|---|
| SAP S/4HANA → platform | Read-only, pull | OData via the existing Fiori/Gateway, technical user | Nightly delta; 50–500 MB/day at Group scale. MM movements, purchase orders, reservations, stock; PM work orders; CO cost; SD commitments; weighbridge |
| EPMS → platform | Read-only, pull | REST API or a read replica | Daily close; tens of MB/day |
| Platform → users | HTTPS | Via existing VPN/SSO, entered from a Fiori Launchpad tile | Map tiles dominate; ~2–10 MB per session |
| Public data → platform | Outbound only | HTTPS via NAT | Satellite scene crops are the largest item: a few hundred MB per estate per refresh, monthly. Weather and fire data are KB-scale, daily |

If SAP and the platform sit in the same AWS account, SAP traffic stays inside
the VPC. If SAP is elsewhere, a private link or VPN tunnel is required — the
integration is pull-based and tolerant of a nightly window, so bandwidth is not
a constraint at these volumes.

**Read-only by design.** The platform holds no write credentials to SAP or
EPMS. Actions it proposes (a purchase requisition, a work order) are drafted as
documents for a person to approve in SAP.

### A5. Security, identity, operations

- Authentication through the client's existing identity provider (SAML/OIDC
  SSO); authorisation mapped to estate/role, mirroring SAP business roles.
- All traffic TLS; database and object storage encrypted at rest with
  customer-managed keys if required.
- Private subnets, no public ingress to the application; internal load balancer.
- Full audit log of every decision recorded, every assumption changed, and
  every AI call made (model, tokens, latency, purpose).
- Standard cloud monitoring and log retention; the application already emits
  structured logs and per-call AI usage records.

### A6. What the client needs to provide

1. An AWS account (or sub-account/OU) in the agreed region, with network
   connectivity to SAP.
2. AI model access enabled in that account/region.
3. A read-only SAP technical user, and the OData services exposed for the
   modules in scope (MM, PM, CO, SD, weighbridge).
4. Block boundary geometry (or agreement to digitise it — this gates the map).
5. SSO integration and role mapping.
6. EPMS API access, once EPMS is live.

---

## Option B — on-premise LLM

For the case where the client requires that no prompt or data reaches a managed
AI service. Everything in Option A still applies, except the LLM tier: an
inference server is added, on the client's own hardware or a GPU instance in
their account.

### B1. What the model has to do

Three jobs, in decreasing difficulty:

1. **Tool-calling assistant** — choose among ~37 tools, chain several calls,
   and answer strictly in figures the server computed. This is the demanding
   one: reliable structured/tool output, not creative writing.
2. **Text-to-SQL** — write correct PostgreSQL against a large schema
   (138 tables in the reference EPMS database) with retrieved context.
3. **Short written output** — briefs, work-order instructions, handover notes.

A small model can do (3). (1) and (2) set the bar.

### B2. Recommended models

| Tier | Model class | Weights | Why |
|---|---|---|---|
| **Recommended** | A 30–32B dense open-weight instruct model with strong tool-calling and code/SQL ability (e.g. Qwen3-32B class) | Apache-2.0 | Best quality-per-GPU at this workload; handles tool calling and SQL at a level close to the managed model in use today |
| Budget | A 14B–24B instruct model (e.g. Qwen3-14B, Mistral Small class) | Apache-2.0 | Fits one 48 GB GPU with room for context; expect more retries on complex SQL |
| Highest quality | A 70B-class model (e.g. Llama 3.3 70B) served quantised | Community/open licence — check terms | Closest to the managed model; roughly doubles hardware |
| Support model | A 7–8B instruct model | Apache-2.0 | Cheap routing/classification tier, can share the same GPU |
| Embeddings | A small sentence-embedding model (the build already uses a 130 MB one) | Apache-2.0 | **CPU is sufficient**, no GPU needed |

Served with a standard inference server (vLLM or equivalent) behind an
OpenAI-compatible endpoint. **The application needs no code change** — it
already routes every AI call through one provider-agnostic layer, with the
provider chosen by configuration.

### B3. Hardware

Assumes 32 k token context (large schema prompts and tool traces), quantised
weights (FP8/AWQ), and human-paced load.

| Tier | GPU | System | Concurrency (estimate) |
|---|---|---|---|
| Pilot / PoC | 1 × 48 GB (L40S or RTX 6000 Ada) | 16 vCPU, 128 GB RAM, 1 TB NVMe | 32B model quantised; ~5–10 concurrent questions |
| Production, recommended | 2 × 48 GB **or** 1 × 80 GB (H100/H200) | 32 vCPU, 256 GB RAM, 2 TB NVMe | 32B at full context; ~20–40 concurrent, room for the small routing model |
| 70B-class | 2 × 80 GB | 64 vCPU, 512 GB RAM, 4 TB NVMe | Reserve for measured need, not by default |

Plus, for a true on-premise server room: redundant power (a 2-GPU node draws
roughly 1.5–2.5 kW under load), rack cooling for that heat, a spare node or a
support contract for availability, and network access from the platform's
subnet.

If "on-premise" only means "not a shared AI service", the same models can run
on GPU instances inside the client's own AWS account — same isolation, no
hardware to buy, and it can be switched off outside working hours.

### B4. What the client should weigh

- **Quality.** An open 32B model is close to, but not equal to, the managed
  model on tool calling and complex SQL. We would not ask anyone to take that
  on trust: the build includes an evaluation harness with an independent judge
  model, so we can measure both on the same question set and show the
  difference before anything is switched over.
- **Cost shape.** Option A is per-token with no idle cost. Option B is a fixed
  capital or reserved-instance cost that runs whether anyone asks a question or
  not. At pilot volumes, Option A is far cheaper; the crossover only arrives at
  sustained heavy use.
- **Who operates it.** Option B makes the client the owner of GPU capacity,
  driver and server upgrades, model updates, and inference availability.
- **What it does not change.** The AI layer writes words, never figures. Every
  number is computed by the server from the client's data and audited back
  against it, and no model in the platform can write to SAP or EPMS. That
  guarantee is identical under both options.

---

## 3. One-page summary for the client

| | Option A — cloud AI | Option B — on-premise LLM |
|---|---|---|
| Application | 2–10 containers, 2–4 vCPU / 4–8 GB each | Same |
| Database | Managed PostgreSQL, 2–8 vCPU, 100 GB–2 TB | Same |
| Storage | 50 GB – 2 TB object storage | Same |
| Model retraining | ~35 s and < 400 MB RAM per estate, nightly, CPU only | Same |
| GPU | **None** | 1–2 × 48 GB (or 1 × 80 GB) |
| AI service | Managed, private endpoint, per-token | Self-hosted 32B-class open model |
| Data leaving the client's account | None (except outbound public weather/satellite pulls) | None |
| Writes to SAP / EPMS | None — read-only, drafts for human approval | Same |
