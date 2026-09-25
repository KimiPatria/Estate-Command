/* The store, checked end to end.
 *
 * The smoke test proves every section renders. This proves the stores build
 * does what buildplan_stores.md says: the MM record reconciles to itself and
 * to the operations ledger, the answer key never leaves the server, lead times
 * and losses come back as planted, the replay reproduces the rush buys it was
 * generated from, switching the model off puts every material back on SAP's
 * settings, a higher service level never lowers a reorder point, and the
 * window drafts a requisition from the same figures it shows.
 *
 *     node test/stores.mjs [baseURL] [screenshotDir]
 *
 * Expects dashboard_server.py to be running. Puts the register and the
 * decision log back after.
 */
import { chromium } from 'playwright';

const BASE = process.argv[2] || 'http://127.0.0.1:8001';
const SHOT = process.argv[3] || null;
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

// ── the API ──────────────────────────────────────────────────────────────

const paths = ['/gis/stores', '/gis/stores?group=fertiliser', '/gis/stores/material?matnr=FE-001',
  '/gis/stores/material?matnr=FU-001', '/gis/stores/material?matnr=SP-004', '/gis/stores/lead-times',
  '/gis/stores/ledger?limit=50', '/gis/stores/purchase-orders?kind=rush', '/gis/stores/accuracy',
  '/gis/stores/document?matnr=AC-001', '/gis/stores/document?matnr=AC-001&kind=mrp_settings_change'];
const bodies = {};
for (const p of paths) {
  try { bodies[p] = await raw(p); } catch (e) { fail(`${p} did not return 200: ${e.message}`); }
}
{
  const r = await fetch(`${BASE}/gis/stores/material?matnr=NOPE`);
  expect(r.status === 404, 'an unknown material is a 404');
}

// The answer key stays on the server.
for (const [p, b] of Object.entries(bodies)) {
  expect(!/delay_cause|vessel_missed/.test(b), `${p} carries no answer key`);
}
const allPos = await raw('/gis/stores/purchase-orders?limit=1000');
expect(!/delay_cause|vessel_missed/.test(allPos), 'purchase orders carry no answer key');

const O = JSON.parse(bodies['/gis/stores']);
expect(O.available && O.materials.length === 17, 'seventeen materials in the store');
expect(O.summary.length >= 2 && O.trust.length === 3, 'a plain summary and three trust grades');
for (const m of O.materials) {
  expect(m.plain && m.plain.headline && ['order_now', 'this_week', 'covered', 'overstocked'].includes(m.status),
    `${m.matnr} carries a status and a sentence`);
}
note('  store:', JSON.stringify(O.counts), '·', O.trust.map(t => `${t.model} ${t.grade.label}`).join(' · '));

const A = JSON.parse(bodies['/gis/stores/accuracy']);
expect(A.ledger.all_pass, `the MM record reconciles: ${JSON.stringify(A.ledger.checks)}`);
expect(A.leadtime.backtest.improvement_pct > 0, 'lead times beat the supplier\'s quote');
expect(A.leadtime.backtest.coverage_ok, 'the one-in-ten line holds on 85-95% of held-out orders');
expect(A.leadtime.recovery.all_recovered,
  `lead times find what was planted: ${A.leadtime.recovery.rows.filter(r => !r.recovered).map(r => r.effect).join(', ') || 'all'}`);
expect(A.consumption.recovery.all_recovered, 'handling and storage losses come back as planted');
expect(A.consumption.backtest.by_kind.programme > 50, 'the programme forecast beats SAP\'s average by far');
note('  checks: lead times', A.leadtime.recovery.rows.filter(r => r.recovered).length, 'of', A.leadtime.recovery.rows.length,
  '· losses', A.consumption.recovery.rows.filter(r => r.recovered).length, 'of', A.consumption.recovery.rows.length);

const R = A.replay;
expect(R.available && Object.keys(R.materials).length === 17, 'the replay covers every material');
expect(R.replay_check.passes, `replaying SAP's settings reproduces the recorded rush buys (${R.replay_check.reproduced} of ${R.replay_check.recorded_rush_orders})`);
for (const m of Object.values(R.materials)) {
  expect(m.old.total_cost_idr >= 0 && m.new.total_cost_idr >= 0, `${m.matnr}: both policies costed`);
  if (m.better) expect(m.new.total_cost_idr < m.old.total_cost_idr, `${m.matnr}: learned only where it costs less`);
}
expect(R.used_cost_idr <= R.old_cost_idr, 'the store never costs more than on SAP\'s settings');
note('  replay:', R.plain.headline);

const M = JSON.parse(bodies['/gis/stores/material?matnr=FE-001']);
expect(M.projection.p50.length === 151 && M.projection.p10.every((v, i) => v <= M.projection.p90[i] + 1e-6),
  'fertiliser projects 150 days, far enough to see the next round');
expect(M.round && M.round.programme > 0, 'fertiliser names its next round');
{
  const D = JSON.parse(bodies['/gis/stores/material?matnr=FU-001']);
  expect(D.projection.p50.length === 91, 'everything else projects 90 days');
}
expect(Math.abs(M.safety_from_supplier + M.safety_from_use - M.safety_stock) < 1, 'the buffer splits into supplier and use');
expect(M.cost_curve.rows.length === 6, 'the cost of six service levels');

// ── the switch and the service level ─────────────────────────────────────

await setAsm('use_stock_model', 0);
{
  const off = await api('/gis/stores');
  expect(off.materials.every(m => m.policy_in_force === 'sap'), 'switched off, every material is on SAP\'s settings');
}
await resetAsm('use_stock_model');

const parts = O.materials.filter(m => m.group === 'SPARE').map(m => m.matnr);
const ropAt = async matnr => (await api(`/gis/stores/material?matnr=${matnr}`)).reorder_point;
const low = {};
await setAsm('service_level_parts', 85);
for (const p of parts) low[p] = await ropAt(p);
await setAsm('service_level_parts', 97);
for (const p of parts) {
  const hi = await ropAt(p);
  expect(hi >= low[p], `${p}: 97% never reorders lower than 85% (${low[p]} → ${hi})`);
}
await resetAsm('service_level_parts');

// ── the window ───────────────────────────────────────────────────────────

const browser = await chromium.launch({ channel: 'chrome' });
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
page.on('console', m => { if (m.type() === 'error' && !/Failed to load resource/.test(m.text())) errors.push(`console: ${m.text()}`); });
page.on('pageerror', e => errors.push(`pageerror: ${e.message}`));
const text = sel => page.evaluate(s => (document.querySelector(s) || {}).textContent || '', sel);
const shot = async name => { if (SHOT) await page.screenshot({ path: `${SHOT}/stores-${name}.png` }); };

await page.goto(`${BASE}/command`, { waitUntil: 'domcontentloaded' });
await page.waitForFunction(() => { const el = document.getElementById('intro'); return !el || el.hidden || getComputedStyle(el).opacity === '0'; },
  null, { timeout: 30000 }).catch(() => fail('intro never lifted'));
await page.waitForTimeout(2000);

await page.evaluate(() => document.querySelector('.dom-ico[data-domain="commercial"]').click());
await page.waitForTimeout(400);
const listed = await page.$$eval('#panels [data-panel]', els => els.map(e => e.dataset.panel));
expect(listed.includes('stores'), 'the commercial domain lists the store');
// Rail hints land after boot, the store's last in its chain; wait for it.
await page.waitForFunction(() => /to order|nothing due/.test(
  (document.querySelector('#panels [data-panel="stores"]') || {}).textContent || ''), null, { timeout: 20000 }).catch(() => {});
const hint = await text('#panels [data-panel="stores"]');
expect(/to order|nothing due/.test(hint), `the rail hint says what is due ("${hint.replace(/\s+/g, ' ').trim()}")`);
await page.evaluate(() => document.querySelector('#panels [data-panel="stores"]').click());
await page.waitForFunction(() => /In short/.test((document.getElementById('fp-content') || {}).textContent || ''),
  null, { timeout: 60000 }).catch(() => fail('the store window never rendered'));
expect(/to order this week/.test(await text('#fp-content')), 'at a glance: the store\'s KPIs');
await shot('overview');

// A section is rendered when its own title is up, not when the menu moves:
// the previous section's body stays on screen while the next one fetches.
const go = async id => {
  const before = await text('#fp-content .fp-sec-h h3');
  await page.evaluate(i => document.querySelector(`#fp-nav [data-sec="${i}"]`).click(), id);
  await page.waitForFunction(([i, prev]) => {
    const on = document.querySelector('#fp-nav .fp-nav-i.on');
    const h = (document.querySelector('#fp-content .fp-sec-h h3') || {}).textContent || '';
    return on && on.dataset.sec === i && h && h !== prev;
  }, [id, before], { timeout: 60000 }).catch(() => fail(`section ${id} did not render`));
};

await go('g:FERT');
await page.evaluate(() => document.querySelector('#fp-content [data-mat="FE-001"]').click());
await page.waitForFunction(() => document.querySelector('#fp-content svg.st-chart'), null, { timeout: 30000 })
  .catch(() => fail('opening a material drew no projection'));
const mat = await text('#fp-content');
expect(/Why the buffer is that size/.test(mat) && /SAP's settings against the learned ones/.test(mat), 'the material explains its buffer against SAP');
expect(/What the service level costs/.test(mat), 'the material prices its service level');
expect(await page.$('#fp-nav [data-sec="material"]'), 'the open material joins the menu');
await shot('material-npk');

for (const id of ['leadtimes', 'season', 'movements', 'pos', 'rush', 'replay', 'checks', 'how']) {
  await go(id);
  if (id === 'leadtimes' || id === 'replay') await shot(id);
}
expect((await page.$$('#fp-content .fc-howblock')).length === 3, 'how: three plain explanations');

// Draft a requisition from a material that is due.
const due = O.materials.find(m => m.order_qty > 0 && (m.status === 'order_now' || m.status === 'this_week'))
  || O.materials.find(m => m.order_qty > 0);
if (due) {
  await go('order');
  await page.evaluate(m => document.querySelector(`#fp-content [data-mat="${m}"]`).click(), due.matnr);
  await page.waitForFunction(() => document.querySelector('#fp-content button.accept[data-decide]'), null, { timeout: 30000 })
    .catch(() => fail('a due material offers no requisition'));
  await page.evaluate(() => document.querySelector('#fp-content button.accept[data-decide]').click());
  await page.waitForFunction(() => /Purchase requisition \(stores\) drafted/.test((document.getElementById('fp-content') || {}).textContent || ''),
    null, { timeout: 90000 }).catch(() => fail('raising a requisition drafted nothing'));
  const log = await api('/gis/decisions?limit=5');
  const d = log.decisions.find(x => x.artifact_kind === 'material_requisition');
  expect(d && d.artifact && d.artifact.lines[0].material === due.matnr, 'the requisition is logged with the material');
  expect(d && Math.abs(d.artifact.lines[0].quantity - due.order_qty) < 1e-6, 'the requisition carries the quantity the window showed');
  await page.evaluate(() => document.querySelector('#fp-content .fp-actbar').scrollIntoView());
  await shot('requisition');
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
note('OK    stores: record reconciles, lead times and losses recovered, replay trusted, switch and service level behave, window drafts');
