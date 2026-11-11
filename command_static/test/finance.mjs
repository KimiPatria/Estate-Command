/* Where money leaks, checked end to end (buildplan_finance.md).
 *
 * Proves the cost build does what the plan says: the ledger reconciles to
 * payroll, the material documents and the fuel issued; the block cost feed is
 * the ledger rolled up; every planted leak comes back and every decoy stays
 * off; the estate's efficiency is the sum of its parts; a register edit
 * reprices only what it should; every diagnosis is either valid or replaced
 * by the rule-based reading; the answer key never leaves the server; and the
 * window renders, drafts a cost action and closes it.
 *
 *     node test/finance.mjs [baseURL] [screenshotDir]
 *
 * Expects dashboard_server.py to be running, with Bedrock reachable for the
 * diagnosis checks (they degrade to the template and say so if not). Puts the
 * register and the decision log back after.
 */
import { readFileSync } from 'node:fs';
import { chromium } from 'playwright';

const BASE = process.argv[2] || 'http://127.0.0.1:8001';
const SHOT = process.argv[3] || null;
const DATA = new URL('../../gis/data/synthetic/', import.meta.url);
const errors = [];
const note = (...a) => console.log(...a);
const fail = msg => { errors.push(msg); console.log('FAIL', msg); };
const expect = (cond, msg) => { if (!cond) fail(msg); };

async function api(path, opts) {
  const r = await fetch(`${BASE}${path}`, opts);
  if (!r.ok) throw new Error(`${path} -> ${r.status}`);
  return r.json();
}
async function raw(path) {
  const r = await fetch(`${BASE}${path}`);
  if (!r.ok) throw new Error(`${path} -> ${r.status}`);
  return r.text();
}
const setAsm = (key, value) => api('/gis/assumptions', {
  method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ key, value }),
});
const resetAsm = key => api(`/gis/assumptions${key ? `?key=${key}` : ''}`, { method: 'DELETE' });

function csv(name) {
  const lines = readFileSync(new URL(name, DATA), 'utf-8').split(/\r?\n/).filter(l => l && !l.startsWith('#'));
  const head = lines[0].split(',');
  return lines.slice(1).map(l => Object.fromEntries(l.split(',').map((v, i) => [head[i], v])));
}

// ── the API ──────────────────────────────────────────────────────────────

const paths = ['/gis/finance/summary', '/gis/finance/summary?month=2025-03', '/gis/finance/nodes?function=transport',
  '/gis/finance/nodes?function=workforce&month=2025-04', '/gis/finance/node?id=transport:vehicle:TR-04&month=2025-03',
  '/gis/finance/node?id=group:fertiliser:5:application_labour&month=2025-04',
  '/gis/finance/checks?id=workforce:crew:U4-02&month=2025-04', '/gis/finance/ledger?limit=50',
  '/gis/finance/ledger?unit=fleet:vehicle:DU-01&limit=20', '/gis/finance/budget?month=2025-04',
  '/gis/finance/accuracy', '/gis/finance/document?id=transport:vehicle:TR-04&month=2025-03',
  '/gis/finance/outcomes', '/gis/metrics?estate=EC&metric=efficiency_pct',
  '/gis/metrics?estate=EC&metric=leakage_idr_per_ha&month=2025-04'];
const bodies = {};
for (const p of paths) {
  try { bodies[p] = await raw(p); } catch (e) { fail(`${p} did not return 200: ${e.message}`); }
}
{
  const r = await fetch(`${BASE}/gis/finance/nodes?function=nope`);
  expect(r.status === 404, 'an unknown function is a 404');
  const u = await fetch(`${BASE}/gis/finance/node?id=transport:vehicle:NOPE`);
  expect(u.status === 404, 'an unknown unit is a 404');
}

// The answer key stays on the server: only accuracy reads it, to score.
for (const [p, b] of Object.entries(bodies)) {
  if (p.includes('accuracy')) continue;
  expect(!/true_cause|mechanism|ec_fi_leak_key|planted_idr/.test(b), `${p} carries no answer key`);
}

const A = JSON.parse(bodies['/gis/finance/accuracy']);
expect(A.ledger && A.ledger.all_pass, `the ledger reconciles: ${JSON.stringify(A.ledger)}`);
expect(A.ledger && A.ledger.other_feeds_unchanged, 'building the ledger moved no other feed');
expect(A.passed === A.total, `every planted leak found and every decoy off: ${A.checks.filter(c => !c.passes).map(c => c.id).join(', ') || 'all'}`);
expect(A.calibration.in_band, `median unit efficiency in its band (${A.calibration.median_unit_efficiency_pct}%)`);
expect(A.coverage.share_pct >= 99.9, 'every rupiah of the ledger sits on a node');
const C = A.comparison;
expect(C.found.leak_engine > C.found.budget_variance, `the engine finds more planted leaks than budget variance (${C.found.leak_engine} against ${C.found.budget_variance})`);
expect(C.false_alarms.leak_engine <= C.false_alarms.budget_variance, 'and raises no more false alarms');
note(`  accuracy ${A.passed}/${A.total}; engine ${C.found.leak_engine}/${C.found.of} found, ${C.false_alarms.leak_engine} false alarms;`,
  `budget variance ${C.found.budget_variance}/${C.found.of}, ${C.false_alarms.budget_variance}`);

// The block cost feed is the ledger rolled up, for the window's months.
{
  const months = new Set(['2025-01', '2025-02', '2025-03', '2025-04', '2025-05']);
  const bc = csv('ec_block_costs.csv').filter(r => months.has(r.month));
  const harvest = bc.reduce((a, r) => a + Number(r.harvest_idr), 0);
  const fert = bc.reduce((a, r) => a + Number(r.fertilizer_idr), 0);
  const P = csv('ec_co_postings.csv');
  const sum = accts => P.filter(p => accts.includes(p.racct) && p.block_code).reduce((a, p) => a + Number(p.wsl), 0);
  const h = sum(['6101000', '6101100', '6101200']);
  const f = sum(['6201000', '6104000']);
  expect(Math.abs(harvest - h) <= bc.length, `block costs: harvest = ledger (${harvest} against ${h})`);
  expect(Math.abs(fert - f) <= bc.length, `block costs: fertiliser = ledger (${fert} against ${f})`);
}

// The estate's efficiency is the sum of its parts, not an average of percentages.
const O = JSON.parse(bodies['/gis/finance/summary']);
{
  const e = O.estate;
  const eff = 100 * Math.min(1, e.necessary_idr / (e.scored_spend_idr + e.lost_idr));
  expect(Math.abs(eff - e.efficiency_pct) < 0.11, `estate efficiency = sum(N) / sum(S + L) (${e.efficiency_pct} against ${eff.toFixed(1)})`);
  expect(O.top.length === 10 && O.top.every((r, i) => i === 0 || O.top[i - 1].leakage_idr >= r.leakage_idr),
    'ten leaks, largest first, by rupiah');
  expect(O.top.every(r => r.leakage_idr >= O.materiality_idr), 'nothing under the materiality line is listed');
  expect(O.top.every(r => r.why && r.do && r.do.owner), 'every row has a why, a do and an owner');
  note('  ', O.headline);
}
{
  const N = JSON.parse(bodies['/gis/finance/node?id=transport:vehicle:TR-04&month=2025-03']);
  const sum = N.drivers.filter(d => d.idr > 0).reduce((a, d) => a + d.idr, 0);
  expect(Math.abs(sum - N.leakage_idr) <= N.drivers.length, 'a unit\'s leakage is its drivers that ran over');
  expect(Math.abs(N.spend_idr + N.lost_idr - N.necessary_idr - N.leakage_idr) <= 2, 'leakage = spend + crop lost - necessary');
  expect(N.documents.length > 0 && N.documents.every(d => d.src_type && d.src_id), 'every posting names its source document');
}

// A register edit to the FFB price reprices crop lost, never spend.
{
  const before = await api('/gis/finance/summary?month=2025-04');
  await setAsm('ffb_price_idr_kg', 3200);
  const after = await api('/gis/finance/summary?month=2025-04');
  const f = (s, k) => s.functions.find(x => x.function === 'harvesting')[k];
  expect(f(after, 'lost_idr') > f(before, 'lost_idr'), 'a higher FFB price raises the crop lost');
  expect(f(after, 'spend_idr') === f(before, 'spend_idr'), 'and leaves spend where it was');
  await resetAsm('ffb_price_idr_kg');
}

// ── the diagnosis ────────────────────────────────────────────────────────

let diagnosed = 0;
for (const r of O.top.slice(0, 3)) {
  const d = await api(`/gis/finance/diagnosis?id=${encodeURIComponent(r.id)}&month=${O.month}`);
  if (!d.available) {
    expect(d.template && d.template.why && d.template.do, `${r.label}: without the model, the rule-based reading stands`);
    note(`  diagnosis ${r.label}: ${d.reason}`);
    continue;
  }
  diagnosed++;
  const ids = new Set(d.checks.map(c => c.id));
  expect(d.diagnosis.cause.evidence.every(e => ids.has(e.id)), `${r.label}: evidence cites only checks the server ran`);
  expect(d.audit && d.audit.clean, `${r.label}: no figure the server did not give`);
  expect(d.diagnosis.actions.length && d.diagnosis.actions.every(a => a.owner && a.saving_idr_month !== undefined),
    `${r.label}: actions from the playbook, with owner and the server's saving`);
  note(`  diagnosis ${r.label}: ${d.diagnosis.headline}`);
}
await setAsm('use_leak_diagnosis', 0);
{
  const d = await api(`/gis/finance/diagnosis?id=${encodeURIComponent(O.top[0].id)}&month=${O.month}&refresh=true`);
  expect(!d.available && d.switched_off && d.template, 'switched off: the rule-based reading, and no model call');
}
await resetAsm('use_leak_diagnosis');
if (diagnosed) {
  const d = await api(`/gis/finance/diagnosis?id=${encodeURIComponent('workforce:crew:U4-02')}&month=2025-04&lang=id`);
  expect(!d.available || (d.lang === 'id' && d.audit.clean), 'in Bahasa Indonesia the figures still pass the audit');
  if (d.available) note('  in Bahasa:', d.diagnosis.headline);
}

// ── the window ───────────────────────────────────────────────────────────

const browser = await chromium.launch({ channel: 'chrome' });
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
page.on('console', m => { if (m.type() === 'error' && !/Failed to load resource/.test(m.text())) errors.push(`console: ${m.text()}`); });
page.on('pageerror', e => errors.push(`pageerror: ${e.message}`));
const text = sel => page.evaluate(s => (document.querySelector(s) || {}).textContent || '', sel);
const shot = async name => { if (SHOT) await page.screenshot({ path: `${SHOT}/finance-${name}.png` }); };

await page.goto(`${BASE}/command`, { waitUntil: 'domcontentloaded' });
await page.waitForFunction(() => { const el = document.getElementById('intro'); return !el || el.hidden || getComputedStyle(el).opacity === '0'; },
  null, { timeout: 30000 }).catch(() => fail('intro never lifted'));
await page.waitForTimeout(2000);

const first = await page.$eval('#rail-doms .dom-ico', b => b.dataset.domain).catch(() => null);
expect(first === 'cost', `cost efficiency is first on the rail (${first})`);
await page.evaluate(() => document.querySelector('.dom-ico[data-domain="cost"]').click());
await page.waitForTimeout(400);
const listed = await page.$$eval('#panels [data-panel]', els => els.map(e => e.dataset.panel));
expect(['leakage', 'efficiency_map', 'budget', 'margin'].every(p => listed.includes(p)), `the cost domain lists its four entries (${listed})`);
await page.waitForFunction(() => /leaked/.test((document.querySelector('#panels [data-panel="leakage"]') || {}).textContent || ''),
  null, { timeout: 30000 }).catch(() => {});
expect(/leaked/.test(await text('#panels [data-panel="leakage"]')), 'the rail hint says how much leaked');

await page.evaluate(() => document.querySelector('#panels [data-panel="leakage"]').click());
await page.waitForFunction(() => document.querySelector('#fp-content .lk-hero'), null, { timeout: 90000 })
  .catch(() => fail('the leak window never rendered'));
expect((await page.$$('#fp-content tr[data-unit]')).length === 10, 'top leaks: ten rows');
await shot('top');

const go = async id => {
  const before = await text('#fp-content .fp-sec-h h3');
  await page.evaluate(i => document.querySelector(`#fp-nav [data-sec="${i}"]`).click(), id);
  await page.waitForFunction(([i, prev]) => {
    const on = document.querySelector('#fp-nav .fp-nav-i.on');
    const h = (document.querySelector('#fp-content .fp-sec-h h3') || {}).textContent || '';
    return on && on.dataset.sec === i && h && h !== prev;
  }, [id, before], { timeout: 60000 }).catch(() => fail(`section ${id} did not render`));
};

for (const id of ['glance', 'f:harvesting', 'f:transport', 'f:fleet', 'f:fertiliser', 'f:upkeep', 'f:pest',
  'f:workforce', 'f:stores', 'f:roads', 'f:overhead', 'postings', 'budget', 'planted', 'compare', 'evals', 'bench', 'how']) {
  await go(id);
  if (['glance', 'f:workforce', 'compare'].includes(id)) await shot(id.replace(':', '-'));
}

// Open a unit from the month selector's March: TR-04's fuel.
await page.evaluate(() => document.querySelector('#fp-extra [data-month="2025-03"]').click());
await page.waitForFunction(() => (document.querySelector('#fp-extra [data-month="2025-03"]') || {}).className === 'on',
  null, { timeout: 60000 }).catch(() => fail('the month did not switch'));
await go('f:transport');
expect(/March 2025/.test(await text('#fp-content .fp-sec-h h3')), 'the sections follow the month');
await page.evaluate(() => document.querySelector('#fp-content tr[data-unit="transport:vehicle:TR-04"]').click());
await page.waitForFunction(() => document.querySelector('#fp-content svg.lk-wf'), null, { timeout: 30000 })
  .catch(() => fail('opening a unit drew no waterfall'));
await page.waitForFunction(() => !document.querySelector('#fp-content .lk-diag.wait'), null, { timeout: 90000 })
  .catch(() => fail('the diagnosis never arrived'));
const unit = await text('#fp-content');
expect(/From benchmark to actual/.test(unit) && /Likely cause/.test(unit), 'the unit shows its waterfall and its likely cause');
await shot('unit-tr04');

// Draft an action, then close it from Actions.
const btn = await page.$('#fp-content .lk-diag button.accept[data-decide]') || await page.$('#fp-content button.accept[data-decide]');
if (btn) {
  await btn.click();
  await page.waitForFunction(() => /Cost action drafted/.test((document.getElementById('fp-content') || {}).textContent || ''),
    null, { timeout: 90000 }).catch(() => fail('drafting an action drafted nothing'));
  const log = await api('/gis/decisions?limit=5');
  const d = log.decisions.find(x => x.artifact_kind === 'cost_action');
  expect(d && d.artifact.lines[0].unit_id === 'transport:vehicle:TR-04', 'the cost action is logged against TR-04');
  expect(d && d.expected_effect && d.expected_effect.driver === 'fuel_gap', 'it records the driver Did it work reads back');
  await go('actions');
  await page.evaluate(() => document.querySelector('#fp-content form[data-close] button').click());
  await page.waitForFunction(() => /Cause found:/.test((document.getElementById('fp-content') || {}).textContent || ''),
    null, { timeout: 30000 }).catch(() => fail('closing the action did not record the cause'));
  const out = await api('/gis/finance/outcomes');
  expect(out.actions.length && out.actions[0].closed, 'the closed action carries the cause found');
  await shot('actions');
} else {
  fail('the unit offered no Draft action');
}

await browser.close();

// ── put things back ──────────────────────────────────────────────────────
await resetAsm();
await api('/gis/decisions', { method: 'DELETE' });

note('');
if (errors.length) {
  note(`FAIL  ${errors.length} problem(s):`);
  [...new Set(errors)].forEach(e => note('  ' + e));
  process.exit(1);
}
note('OK    finance: ledger reconciles, leaks found and decoys off, efficiency sums, diagnosis valid, window drafts and closes');
