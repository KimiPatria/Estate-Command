// Forecast pages end-to-end check: the shared look (theme, topbar) and the
// Database control (live files vs the staging database) on /forecast/analyst
// (the full page; /forecast is the outlook, see outlook_page.mjs),
// /model-health and /experiment/forecast.
//   node test/forecast_page.mjs [base] [shotDir]
// Needs the dashboard server up. The staging checks need the staging forecast
// built (forecast/build_staging_series.py, forecast/lab/build_artifacts.py
// --source staging, forecast/lab/select_staging.py); without it they are
// reported as skipped and the live checks still run.
import { chromium } from 'playwright';

const base = process.argv[2] || 'http://127.0.0.1:8001';
const shots = process.argv[3] || null;
const fails = [];
const check = (ok, msg) => { console.log(`${ok ? 'ok  ' : 'FAIL'} ${msg}`); if (!ok) fails.push(msg); };
const skip = (msg) => console.log(`skip ${msg}`);

const browser = await chromium.launch({ channel: 'chrome' });
const context = await browser.newContext({ viewport: { width: 1360, height: 1000 }, colorScheme: 'light' });
const page = await context.newPage();
const errors = [];
page.on('pageerror', e => errors.push(e.message));
page.on('console', m => {
  if (m.type() === 'error' && !m.text().startsWith('Failed to load resource')) errors.push(m.text());
});
page.on('response', r => {
  if (r.status() >= 400 && !r.url().endsWith('/favicon.ico')) errors.push(`${r.status()} ${r.url()}`);
});
const external = [];
page.on('request', r => {
  const u = new URL(r.url());
  // Fonts and map tiles are the only things these pages may fetch from elsewhere.
  if (!['127.0.0.1', 'localhost'].includes(u.hostname) && r.resourceType() === 'script') external.push(r.url());
});

const ready = () => page.waitForSelector('#state-result:not(.hidden)', { timeout: 60000 });
const shot = async (name) => {
  if (!shots) return;
  await page.waitForTimeout(700);          // let Chart.js finish animating
  await page.screenshot({ path: `${shots}/fc_${name}.png` });
};
const css = (name) => page.evaluate((n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim(), name);
const bodyBg = () => page.evaluate(() => getComputedStyle(document.body).backgroundColor);
const chartMonths = () => page.evaluate(() => {
  const c = Chart.getChart('fc-chart');
  const fc = c.data.datasets.find(d => d.label === 'Forecast');
  return { labels: c.data.labels.length, forecast: fc.data.filter(v => v != null).length - 1, line: fc.borderColor };
});
const pickDb = async (id) => {
  await page.locator('#db-chip').click();
  await page.locator(`#db-pop .opt[data-db="${id}"]`).click();
};

// ── /forecast/analyst, live ─────────────────────────────────────────────────────
await page.goto(`${base}/forecast/analyst`, { waitUntil: 'networkidle' });
await page.evaluate(() => { localStorage.clear(); });
await page.goto(`${base}/forecast/analyst`, { waitUntil: 'networkidle' });
await ready();
check(await page.locator('header.topbar .app-nav a.on').innerText() === 'Forecast', 'topbar nav marks Forecast');
check(await css('--accent') === '#115e6e' && await bodyBg() === 'rgb(255, 255, 255)', 'light theme uses the chat tokens');
check((await page.locator('#db-chip').innerText()).includes('Live'), 'database chip starts on Live');
const live = await chartMonths();
check(live.forecast === 3, 'live: 3 forecast months drawn');
// Since 2026-11-12 live serves Chronos-2 too (buildplan_forecast_mlops.md D1/D9): no
// ensemble member columns and a 6-month cap, as on staging, unless K3 is on its
// ensemble fallback (then the ensemble's 8 columns and 1Y come back).
const liveFallback = await page.evaluate(() =>
  fetch('/forecast/data?estate=k3&source=live').then(r => r.json()).then(d => !!d.fallback));
check(await page.locator('#fc-tbody tr').first().locator('td').count() === (liveFallback ? 8 : 5),
      'live: Chronos-2 served (member columns only on the ensemble fallback)');
check(await page.locator('#hz-toggle .hz-btn:not(.hidden)').count() === (liveFallback ? 3 : 2),
      'live: 3M / 6M offered (1Y only on the ensemble fallback)');
check((await page.locator('body').innerText()).includes('Chronos-2'), 'live: model named as Chronos-2');
// Phase 2: the band says how often it held, and the 80% range is drawn and in the legend
check(liveFallback || await page.locator('#lg-band80').isVisible(), 'live: 80% range in the legend');
check(/held \d+ of \d+/.test(await page.locator('#kpi-row').innerText()), 'live: band tile says how often the band held');
check(!(await page.locator('#intel-card').evaluate(n => n.classList.contains('hidden'))), 'live: risk panel shown');
const tiles = await page.locator('#est-strip .est-tile:not(.na) .est-value').allInnerTexts();
check(new Set(tiles).size === tiles.length, `live: each modelled estate shows its own total (${tiles.join(', ')})`);
await shot('live_light');

// theme switch: tokens flip, the chart is redrawn in the new colours, the choice sticks
await page.locator('#theme-btn').click();
check(await css('--accent') === '#62b5ab' && await bodyBg() === 'rgb(22, 26, 25)', 'dark theme uses the chat tokens');
const dark = await chartMonths();
check(dark.line !== live.line, `chart redrawn in the dark palette (${live.line} -> ${dark.line})`);
check(await page.evaluate(() => localStorage.getItem('chat-theme')) === 'dark', 'theme choice is stored under the chat key');
await shot('live_dark');
// /model-health alone is the overview of every forecast (model_health_fleet.mjs); ?estate= is the detail
await page.goto(`${base}/model-health?estate=k3`, { waitUntil: 'networkidle' });
await ready();
check(await bodyBg() === 'rgb(22, 26, 25)', 'model health opens in the theme picked on /forecast');
check(await page.locator('#kpi-row .kpi-card').count() === 4, 'model health (live) renders its KPI row');
await page.locator('#theme-btn').click();

// ── staging ─────────────────────────────────────────────────────────────
const src = await page.evaluate(() => fetch('/forecast/sources').then(r => r.json()));
const staging = src.sources.find(s => s.id === 'staging');
if (!staging || !staging.available) {
  skip('staging checks: ' + ((staging && staging.reason) || 'no staging source'));
  await page.goto(`${base}/forecast/analyst`, { waitUntil: 'networkidle' });
  await ready();
  await page.locator('#db-chip').click();
  check(await page.locator('#db-pop .opt[data-db="staging"]').isDisabled(), 'unbuilt staging is listed but disabled');
} else {
  await page.goto(`${base}/forecast/analyst`, { waitUntil: 'networkidle' });
  await ready();
  await pickDb('staging');
  await page.waitForFunction(() => document.querySelector('#db-chip').innerText.includes('staging'));
  await ready();
  await page.waitForFunction(() => document.querySelectorAll('#scope-chips .scope-chip').length === 4);
  const chips = await page.locator('#scope-chips .scope-chip').allInnerTexts();
  check(chips.join(',') === 'All estates,EA,EB,EC', `staging: scope lists its own estates (${chips.join(',')})`);
  check(await page.locator('#hz-toggle .hz-btn:not(.hidden)').count() === 2, 'staging: horizon stops at 6M');
  check(await page.locator('#fc-tbody tr').first().locator('td').count() === 5, 'staging: member columns hidden');
  check(await page.locator('#lg-band80').isVisible(), 'staging: 80% range in the legend');
  check(/held \d+ of \d+/.test(await page.locator('#kpi-row').innerText()), 'staging: band tile says how often the band held');
  check(await page.locator('#intel-card').evaluate(n => n.classList.contains('hidden')), 'staging: risk panel hidden');
  check(await page.locator('#drv-card').evaluate(n => n.classList.contains('hidden')), 'staging: SHAP drivers hidden');
  check((await page.locator('#exec-coverage').innerText()) === '3 of 3 estates modelled', 'staging: group covers 3 of 3');
  const st = await page.locator('#est-strip .est-tile .est-value').allInnerTexts();
  check(st.length === 3 && new Set(st).size === 3, `staging: three estate tiles, each its own total (${st.join(', ')})`);
  check(await page.evaluate(() => localStorage.getItem('chat-db')) === 'staging', 'database choice is stored under the chat key');
  await shot('staging_group');

  // group total = the three estate totals
  const sums = await page.evaluate(async () => {
    const get = (e) => fetch('/forecast/data?source=staging&horizon=3&estate=' + e).then(r => r.json());
    const [all, ea, eb, ec] = await Promise.all(['all', 'ea', 'eb', 'ec'].map(get));
    return { all: all.total_forecast, parts: ea.total_forecast + eb.total_forecast + ec.total_forecast,
             model: all.model_name, anchor: all.anchor.month };
  });
  check(Math.abs(sums.all - sums.parts) <= 9, `staging: group total is the sum of the estates (${sums.all} vs ${sums.parts})`);

  // one estate, 6 months
  await page.locator('#scope-chips .scope-chip[data-scope="ec"]').click();
  await page.waitForFunction(() => document.querySelector('#exec-title').innerText.startsWith('EC'));
  await page.locator('#hz-toggle .hz-btn[data-hz="6"]').click();
  await page.waitForFunction(() => document.querySelectorAll('#fc-tbody tr').length === 6);
  check((await chartMonths()).forecast === 6, 'staging EC: 6 forecast months drawn');
  // a manual pick from Model health adds " · manual pick" after the name
  check((await page.locator('#model-pill').innerText()).startsWith(sums.model), `staging: header names the served model (${sums.model})`);
  await shot('staging_ec_6m');

  // the choice carries to the other two pages (the detail view, without ?db=)
  await page.goto(`${base}/model-health?estate=all`, { waitUntil: 'networkidle' });
  await ready();
  check((await page.locator('#db-chip').innerText()).includes('staging'), 'model health opens on the database picked on /forecast');
  check((await page.locator('.fc-title .accent').innerText()).includes('staging'), 'model health reports the staging model');
  check(await page.locator('#board-tbody tr.served').count() === 1, 'model health: served model flagged on the scoreboard');
  await shot('staging_health');

  await page.goto(`${base}/experiment/forecast`, { waitUntil: 'networkidle' });
  await ready();
  const labEstates = await page.locator('#estate-chips .scope-chip').allInnerTexts();
  check(labEstates.length === 3 && !labEstates.some(t => t.startsWith('K3')), `lab lists the staging estates only (${labEstates.map(t => t.split('\n')[0]).join(',')})`);
  check(await page.locator('#mdl-list .badge').count() === 1, 'lab marks the model served on /forecast');
  await page.locator('#view-toggle .hz-btn[data-view=backtest]').click();
  await page.waitForFunction(() => document.querySelectorAll('#lb-body tr').length >= 1, null, { timeout: 60000 });
  check(await page.locator('#promo-card').evaluate(n => n.classList.contains('hidden')), 'lab: K3 promotion gate hidden on staging');
  await shot('staging_lab_backtest');

  // and back: live is untouched
  await pickDb('live');
  await page.waitForFunction(() => [...document.querySelectorAll('#estate-chips .scope-chip')].some(b => b.innerText.startsWith('K3')));
  check(true, 'lab returns to the live estates');
  await page.goto(`${base}/forecast/analyst?db=live`, { waitUntil: 'networkidle' });
  await ready();
  check((await chartMonths()).forecast === 3 && (await page.locator('#fc-tbody tr').first().locator('td').count()) === (liveFallback ? 8 : 5),
        'live forecast unchanged after the round trip');
}

check(external.length === 0, `no scripts loaded from outside the server (${external.join(' | ')})`);
check(errors.length === 0, `no page errors (${errors.join(' | ')})`);
await browser.close();
console.log(fails.length ? `\n${fails.length} failed` : '\nall passed');
process.exit(fails.length ? 1 : 0);
