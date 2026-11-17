// /forecast/compare -- the Compare sources view (buildplan_forecast_mlops.md Phase 4).
// Usage: node test/forecast_compare.mjs http://127.0.0.1:8001 [shotDir]
import { chromium } from 'playwright';

const base = process.argv[2] || 'http://127.0.0.1:8001';
const shotDir = process.argv[3] || null;
let failed = 0;
const check = (ok, name) => { console.log((ok ? 'ok   ' : 'FAIL ') + name); if (!ok) failed++; };

const browser = await chromium.launch({ channel: 'chrome' });
const page = await browser.newPage({ viewport: { width: 1360, height: 900 } });
const errors = [], external = [];
page.on('pageerror', e => errors.push(e.message));
page.on('request', r => { const u = r.url(); if (!u.startsWith(base) && !u.startsWith('data:') && !u.includes('fonts.g')) external.push(u); });
const shot = async (id, name) => {
  if (!shotDir) return;
  const el = page.locator('#' + id); await el.scrollIntoViewIfNeeded(); await page.waitForTimeout(600);
  await el.screenshot({ path: `${shotDir}/cmp_${name}.png` });
};

// the forecast page links here through its view switch
await page.goto(`${base}/forecast`, { waitUntil: 'networkidle' });
check(await page.locator('.view-tabs a', { hasText: 'Compare sources' }).count() === 1, 'forecast page offers the Compare sources view');
await page.locator('.view-tabs a', { hasText: 'Compare sources' }).click();
await page.waitForURL(/\/forecast\/compare/);
await page.waitForSelector('#data-table tbody tr', { timeout: 120000 });
const api = await page.evaluate(() => fetch('/forecast/compare-data').then(r => r.json()));

// data available: real source headers, a status for each
const dataTxt = await page.locator('#data-table').innerText();
check(dataTxt.includes('ZMASAK') && dataTxt.includes('bunches_total'), 'data table shows both sources\' own column headers');
check(/populated/.test(dataTxt) && /not available/.test(dataTxt), 'data table marks populated and not-available items');
const items = api.availability.items.length;
check(await page.locator('#data-table tbody tr:not(.group)').count() === items, `one row per data item (${items})`);
check(api.availability.items.every(r => ['live', 'staging'].every(s => ['populated', 'sparse', 'empty', 'not available'].includes(r[s].status))),
      'every cell carries a status from the extract metadata');
await page.locator('#data-chips .scope-chip', { hasText: 'K3' }).click();
const k3Txt = await page.locator('#data-table').innerText();
check(!k3Txt.includes('ZMASAK') && k3Txt.includes('bunches_total'), 'per-estate view: K3 has no staging columns');
await page.locator('#data-chips .scope-chip', { hasText: 'All estates' }).click();
await shot('card-data', 'data');

// history: a bar per source and estate that exists
const bars = await page.locator('#timeline .bar').count();
const expected = api.history.reduce((n, h) => n + ['live', 'staging'].filter(s => h[s].available).length, 0);
check(bars === expected, `history draws one bar per source and estate (${bars})`);
check((await page.locator('#timeline').innerText()).includes('needs 3'), 'history says which estate a source cannot forecast');
await shot('card-history', 'history');

// agreement
check(/\d+ of \d+/.test(await page.locator('#agree-kpis').innerText()), 'agreement counts matching months');
check(await page.locator('#agree-chart').evaluate(c => c.width > 0), 'agreement chart drawn');
await shot('card-agree', 'agree');

// accuracy: own window, shared window, same model
const accTxt = await page.locator('#acc-table').innerText();
check(accTxt.includes('Served model, own window') && accTxt.includes('Same model, shared window'), 'accuracy separates own window, shared window and same model');
await shot('card-acc', 'accuracy');

// forecast and running cost
check(await page.locator('#fwd-chart').evaluate(c => c.width > 0), 'forecast chart drawn');
const costTxt = await page.locator('#cost-table').innerText();
check(/3 databases/.test(costTxt) && /1 database\b/.test(costTxt), 'running cost: live needs 3 databases, staging 1');
check(costTxt.includes('Monitoring alerts'), 'running cost lists the monitoring alerts of the served run');
await shot('card-fwd', 'forecast');
await shot('card-cost', 'cost');

// dark theme and phone width
await page.goto(`${base}/forecast/compare?theme=dark`, { waitUntil: 'networkidle' });
await page.waitForSelector('#data-table tbody tr', { timeout: 120000 });
check(await page.evaluate(() => document.documentElement.dataset.theme === 'dark'), 'dark theme applies');
await page.setViewportSize({ width: 390, height: 844 });
await page.waitForTimeout(500);
check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), 'no horizontal page scroll at phone width');

check(errors.length === 0, `no page errors (${errors.join(' | ')})`);
check(external.length === 0, `no scripts loaded from outside the server (${external.join(', ')})`);
await browser.close();
console.log(failed ? `\n${failed} failed` : '\nall passed');
process.exit(failed ? 1 : 0);
