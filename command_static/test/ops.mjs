/* The operating rhythm, driven end to end through its windows.
 *
 * The smoke test proves every window and section renders. This proves they
 * work: open the harvest window, edit a gang's headcount, re-run, see the
 * plan change; send a gang's blocks to the map and come back to the same
 * place; filter the ledger; draft the assignment; find it in Did it work;
 * switch the weeding window to spraying; edit an assumption. Then put the
 * shared demo state back.
 *
 *     node test/ops.mjs [baseURL]
 *
 * Expects dashboard_server.py to be running.
 */
import { chromium } from 'playwright';

const BASE = process.argv[2] || 'http://127.0.0.1:8001';
const SHOT = process.argv[3] || null;       // optional directory for screenshots
const errors = [];
const note = (...a) => console.log(...a);

const browser = await chromium.launch({ channel: 'chrome' });
const page = await browser.newPage({ viewport: { width: 1600, height: 950 } });
page.on('pageerror', e => errors.push(`pageerror: ${e.message}`));
page.on('console', m => {
  if (m.type() === 'error' && !/Failed to load resource/.test(m.text())) errors.push(`console: ${m.text()}`);
});

const text = sel => page.evaluate(s => ((document.querySelector(s) || {}).textContent || '').replace(/\\s+/g, ' ').trim(), sel);
const visible = sel => page.evaluate(s => { const n = document.querySelector(s); return !!n && !n.hidden; }, sel);
const shot = async name => { if (SHOT) await page.screenshot({ path: `${SHOT}/${name}.png` }); };

const DOMAIN = { ops_harvest: 'harvesting', ops_weed: 'upkeep', outcomes: 'governance', assumptions: 'governance' };
async function openWindow(key) {
  await page.evaluate(d => {
    const b = document.querySelector(`.dom-ico[data-domain="${d}"]`);
    if (!b.classList.contains('on')) b.click();
  }, DOMAIN[key]);
  await page.waitForTimeout(300);
  await page.evaluate(k => document.querySelector(`#panels [data-panel="${k}"]`).click(), key);
  await page.waitForFunction(() => document.querySelectorAll('#fp-nav [data-sec]').length > 1
    && !/^Loading/.test((document.getElementById('fp-content') || {}).textContent.trim()), null, { timeout: 30000 });
  await page.waitForTimeout(200);
}
async function section(id) {
  await page.click(`#fp-nav [data-sec="${id}"]`);
  await page.waitForFunction(s => {
    const on = document.querySelector('#fp-nav .fp-nav-i.on');
    return on && on.dataset.sec === s && !/^Loading/.test(document.getElementById('fp-content').textContent.trim());
  }, id, { timeout: 20000 });
  await page.waitForTimeout(150);
}

await page.goto(`${BASE}/command`, { waitUntil: 'domcontentloaded' });
await page.waitForFunction(() => {
  const el = document.getElementById('intro');
  return !el || el.hidden || getComputedStyle(el).opacity === '0' || getComputedStyle(el).display === 'none';
}, null, { timeout: 30000 });
await page.waitForTimeout(2000);

// ── the harvest window opens instead of the dock ─────────────────────────
await openWindow('ops_harvest');
if (!(await visible('#fp-bg'))) errors.push('harvest did not open a window');
if (await visible('#wb')) errors.push('harvest also opened the dock');
const groups = await page.$$eval('#fp-nav .fp-nav-l', els => els.map(e => e.textContent.trim()));
note('  menu groups:', groups.join(' | '));
for (const g of ['Tomorrow', 'Ledger', 'Why', 'About']) if (!groups.includes(g)) errors.push(`menu missing group ${g}`);
const title0 = await text('.fp-sec-t h3');
const lead0 = await text('#fp-content .kpis');
note('  plan:', lead0);
if (!/present/.test(lead0)) errors.push('plan section has no headline figures');
await shot('window-plan');

// Edit the first gang, re-run, the plan changes.
const firstCrew = await page.$eval('#fp-content [data-present]', el => el.dataset.present);
const before = await page.$eval('#fp-content [data-present]', el => Number(el.value));
await page.fill('#fp-content [data-present]', String(Math.max(0, before - 6)));
await page.dispatchEvent('#fp-content [data-present]', 'change');
if (!/edits/.test(await text('#fp-content [data-replan]'))) errors.push('edit did not mark the plan dirty');
await page.click('#fp-content [data-replan]');
await page.waitForFunction(l => {
  const p = document.querySelector('#fp-content .kpis');
  return p && p.textContent.replace(/\\s+/g, ' ').trim() !== l;
}, lead0, { timeout: 30000 }).catch(() => errors.push('re-run with an edited headcount produced the same plan'));
note('  after edit:', await text('#fp-content .kpis'));
const edited = await page.$eval(`#fp-content tr[data-crew="${firstCrew}"]`, el => el.classList.contains('edited'));
if (!edited) errors.push('edited gang not marked on the plan');

// Show one gang's blocks on the map: the window minimises, the bar says so.
await page.click(`#fp-content tr[data-crew="${firstCrew}"] .fp-map-btn`);
await page.waitForTimeout(900);
if (await visible('#fp-bg')) errors.push('show on map did not minimise the window');
if (!(await visible('#fp-bar'))) errors.push('no restore bar after show on map');
const cap = await text('#fp-bar');
note('  minimised:', cap);
if (!/outlined/.test(cap)) errors.push(`restore bar caption: ${cap}`);
await shot('window-minimised');
await page.click('#fp-restore');
await page.waitForTimeout(300);
if (!(await visible('#fp-bg'))) errors.push('restore did not bring the window back');
if ((await text('.fp-sec-t h3')) !== title0) errors.push('restore came back to a different section');

// Section-level show on map, then close from the bar.
await section('unreached');
if (!(await page.$('.fp-map-all'))) errors.push('not-reached section has no show-on-map');

// The ledger: filters apply across its sections.
await section('orders');
const n0 = await text('.fp-filter-n');
await page.selectOption('#fp-content select[data-filter="status"]', 'weathered_off');
await page.waitForFunction(t => {
  const n = document.querySelector('.fp-filter-n');
  return n && n.textContent.replace(/\\s+/g, ' ').trim() !== t;
}, n0, { timeout: 20000 });
note('  orders filtered:', await text('.fp-filter-n'));
await section('summary');
const sel = await page.$eval('#fp-content select[data-filter="status"]', el => el.value);
if (sel !== 'weathered_off') errors.push('ledger filter did not carry to another section');
await page.click('#fp-content [data-clear-filters]');
await page.waitForFunction(() => !/match/.test(document.querySelector('.fp-filter-n').textContent), null, { timeout: 20000 });
await shot('window-ledger');

// Why: the arithmetic is on screen.
await section('objective');
const why = await text('#fp-content');
for (const k of ['Binding constraint', 'gap to bound', 'Method']) if (!why.includes(k)) errors.push(`objective missing "${k}"`);
await section('contiguity');
if (!/adjacent pairs/i.test(await text('#fp-content'))) errors.push('contiguity section empty');

// Draft the assignment from the plan section.
await section('plan');
await page.click('#fp-content .acts .accept');
await page.waitForSelector('#fp-content .artifact', { timeout: 30000 });
const art = await text('#fp-content .artifact-h');
note('  artifact:', art);
if (!/assignment drafted/i.test(art)) errors.push(`artifact header: ${art}`);
const log = await (await page.request.get(`${BASE}/gis/decisions?limit=1`)).json();
const d = log.decisions[0];
if (!d || !d.due_date || !d.expected_effect || !(d.order_ref || []).length) errors.push('decision missing loop fields');

// Escape closes the window.
await page.keyboard.press('Escape');
await page.waitForTimeout(200);
if (await visible('#fp-bg')) errors.push('Escape did not close the window');

// Did it work lists the plan.
await openWindow('outcomes');
await section('plans');
const oc = await text('#fp-content');
if (!/beyond ledger|pending/.test(oc)) errors.push('accepted plan not listed in Did it work');
note('  outcomes:', oc.slice(0, 110));
await page.click('#fp-x');

// The weeding window switches to spraying.
await openWindow('ops_weed');
const weedLead = await text('#fp-content .kpis');
await page.click('#fp-extra [data-variant="spray"]');
await page.waitForFunction(l => {
  const p = document.querySelector('#fp-content .kpis');
  // Spraying is planned for teams, weeding for crews, held or not.
  return p && /teams/.test(p.textContent) && p.textContent.replace(/\\s+/g, ' ').trim() !== l;
}, weedLead, { timeout: 30000 }).catch(() => errors.push('spraying variant did not load'));
note('  spraying:', await text('#fp-content .kpis'));
await page.click('#fp-x');

// The register: open a group, edit a value.
await openWindow('assumptions');
await section('pricing');
const key = 'ffb_price_idr_kg';
await page.fill(`#fp-content input[data-asm="${key}"]`, '2800');
await page.dispatchEvent(`#fp-content input[data-asm="${key}"]`, 'change');
await page.waitForFunction(() => /set to/.test((document.querySelector('#fp-content [data-asm-note]') || {}).textContent || ''),
  null, { timeout: 15000 });
note('  assumption:', await text('#fp-content [data-asm-note]'));
const navCount = await text('#fp-nav [data-sec="pricing"] .n');
if (!/edited/.test(navCount)) errors.push(`menu count did not update after an edit: ${navCount}`);
const reg = await (await page.request.get(`${BASE}/gis/assumptions`)).json();
if (!reg.assumptions.find(a => a.key === key && a.overridden && a.value === 2800)) errors.push('assumption override not persisted');
await shot('window-register');
await page.click('#fp-x');

// Put the shared demo state back.
await page.request.delete(`${BASE}/gis/assumptions`);
await page.request.delete(`${BASE}/gis/decisions`);
await browser.close();

console.log('');
if (errors.length) {
  console.log(`FAIL  ${errors.length} problem(s)`);
  errors.forEach(e => console.log('  - ' + e));
  process.exit(1);
}
console.log('OK    windows: plan edited and re-run, blocks sent to the map and back, ledger filtered, assignment drafted, outcome tracked, variant switched, assumption saved');
