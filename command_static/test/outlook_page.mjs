// Production outlook (/forecast) end-to-end check: the plain-language page
// everyone lands on. Headline in words, four tiles (tonnes and budget as N/A
// placeholders), the trend chart as the centerpiece, one mini chart per estate,
// the track record in words, and no model jargon anywhere on the page.
//   node test/outlook_page.mjs [base] [shotDir]
// Needs the dashboard server up. Staging checks are skipped when the staging
// forecast is not built.
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

const ready = () => page.waitForSelector('#state-result:not(.hidden)', { timeout: 60000 });
const shot = async (name) => {
  if (!shots) return;
  await page.waitForTimeout(700);
  await page.screenshot({ path: `${shots}/outlook_${name}.png` });
};
const chartInfo = () => page.evaluate(() => {
  const c = Chart.getChart('trend-chart');
  const ds = (l) => c.data.datasets.find(d => d.label === l);
  return { labels: c.data.labels, actual: ds('Actual').data, forecast: ds('Forecast').data,
           ly: ds('Last year').data, names: c.data.datasets.map(d => d.label) };
});
// Words a non-technical reader should never meet on this page.
const JARGON = /smape|\bmape\b|\bmase\b|coverage|interval|quantile|backtest|sarimax|lightgbm|treeshap|shap\b|chronos|horizon|conformal|ensemble|zero-shot|walk-forward|benchmark/i;

// ── live ────────────────────────────────────────────────────────────────
await page.goto(`${base}/forecast`, { waitUntil: 'networkidle' });
await page.evaluate(() => { localStorage.clear(); });
await page.goto(`${base}/forecast`, { waitUntil: 'networkidle' });
await ready();
const data = await page.evaluate(() => fetch('/forecast/data?horizon=3&estate=k3&source=live').then(r => r.json()));

check(await page.title() === 'EPMS · Production Outlook', '/forecast serves the outlook');
check(await page.locator('header.topbar .app-nav a.on').innerText() === 'Forecast', 'topbar nav marks Forecast');
check((await page.locator('#analyst-link').getAttribute('href')) === '/forecast/analyst', 'analyst view is one link away');
check((await page.locator('#db-chip').innerText()).includes('Live'), 'database chip starts on Live');

const headline = await page.locator('#headline').innerText();
check(/^K3 production is expected to (rise|ease|hold steady|.*then).* over the next 3 months/.test(headline),
      `headline says where K3 is heading in words (${headline})`);

const tiles = await page.locator('#tiles .tile').allInnerTexts();
check(tiles.length === 4, 'four headline tiles');
check(/Next month/.test(tiles[0]) && /bunches/.test(tiles[0]) && /Likely/.test(tiles[0]), 'next-month tile: value, unit, likely range');
check(/Tonnes: N\/A/.test(tiles[1]), 'tonnes placeholder is N/A');
check(/vs budget/.test(tiles[3]) && /N\/A/.test(tiles[3]), 'budget placeholder is N/A');
const total = data.forecast.reduce((s, f) => s + f.ensemble, 0);
check(tiles[1].includes(Math.round(total / 1e3) + 'K'), `3-month tile shows the forecast total (${total})`);

const ci = await chartInfo();
check(ci.labels.length === 12 + 3, 'chart: 12 months of actuals + 3 forecast months');
check(ci.forecast.filter(v => v != null).length === 4, 'chart: forecast starts at the last actual month');
check(ci.names.includes('Last year') && ci.names.filter(n => !n.startsWith('_')).length === 3, 'chart: actual, forecast and last year only');
const byMonth = new Map(data.history.map(h => [h.month, h.value]));
const lyOk = ci.labels.every((m, i) => {
  const [y, mo] = m.split('-'); const prev = byMonth.get(`${+y - 1}-${mo}`);
  return prev == null ? ci.ly[i] == null : ci.ly[i] === prev;
});
check(lyOk, 'chart: last-year line is the same calendar month one year earlier');
check(await page.locator('#trend-legend').innerText().then(t => /Budget · N\/A/.test(t)), 'legend keeps a Budget · N/A slot');

check(await page.locator('#num-body tr').count() === 3, 'numbers table: one row per forecast month');

if (data.track_record) {
  const t = await page.locator('#track').innerText();
  check(new RegExp(`within ±10% of actual in ${data.track_record.within} of ${data.track_record.n}`).test(t),
        `track record in words (${t})`);
  check(await page.locator('#track .dot').count() === data.track_record.n, 'one dot per tested month');
} else {
  check(await page.locator('#track').evaluate(n => n.classList.contains('hidden')), 'no track record: line hidden');
}

// the revision line appears only when there is an earlier forecast of the same months
check(await page.locator('#rev-note').evaluate(n => n.classList.contains('hidden')) === !data.revision,
      'revision line follows the payload');
await page.evaluate(() => render({ ...lastData, revision: { previous_anchor: '2026-03', months: ['2026-05', '2026-06'],
  previous: 100, current: 103, pct: 3.0 } }));
check((await page.locator('#rev-note').innerText()).startsWith('Up 3.0% since the previous forecast for May – Jun 2026'),
      'revision line reads as a change since the previous forecast');

// estates: mini charts, biggest drop vs last year first, click opens the estate
const modeled = (data.scope.estates || []).filter(e => e.modeled && e.tile && e.tile.spark);
check(await page.locator('#est-grid .est').count() === modeled.length, `one mini chart per modelled estate (${modeled.length})`);
const order = await page.locator('#est-grid .est').evaluateAll(ns => ns.map(n => n.dataset.scope));
const want = [...modeled].sort((a, b) => (a.tile.yoy_pct ?? Infinity) - (b.tile.yoy_pct ?? Infinity)).map(e => e.id);
check(order.join() === want.join(), `estates sorted by change vs last year (${order.join()})`);
check(await page.evaluate(() => document.querySelectorAll('#est-grid canvas').length === sparks.length && sparks.length > 0),
      'each estate card draws its mini chart');
const needs = await page.locator('#needs').innerText();
check(/bunch weight/.test(needs) && /budget/.test(needs), `needs strip names the missing inputs (${needs.replace(/\n/g, ' ')})`);

const pageText = await page.locator('body').innerText();
const hit = pageText.match(JARGON);
check(!hit, `no model jargon on the page${hit ? ' (found "' + hit[0] + '")' : ''}`);
await shot('live');

// direction words, on made-up paths
const words = await page.evaluate(() => {
  const h = (v) => [{ month: '2026-01', value: v, underrecorded: false }];
  const f = (...vs) => vs.map((v, i) => ({ month: `2026-0${i + 2}`, ensemble: v }));
  return [
    direction({ history: h(100), forecast: f(104, 108, 112) }).text,
    direction({ history: h(100), forecast: f(98, 97, 96) }).text,
    direction({ history: h(100), forecast: f(101, 99, 102) }).text,
    direction({ history: h(100), forecast: f(85, 90, 120) }).text,
    direction({ history: h(100), forecast: f(120, 110, 101) }).text,
    direction({ history: [{ month: '2026-01', value: 10, underrecorded: true }], forecast: f(100, 101, 102) }).text,
  ];
});
check(words.join('|') === 'rise|ease|hold steady|dip in Feb 2026, then rise|peak in Feb 2026, then ease back|hold steady',
      `direction rule (${words.join(' | ')})`);

// 6 months
await page.locator('#hz-toggle .hz-btn[data-hz="6"]').click();
await page.waitForFunction(() => document.querySelectorAll('#num-body tr').length === 6, null, { timeout: 60000 });
check((await chartInfo()).forecast.filter(v => v != null).length === 7, '6 months: six forecast months drawn');
check(/over the next 6 months/.test(await page.locator('#headline').innerText()), '6 months: headline follows');

// click an estate card
const other = order.find(id => id !== 'k3');
if (other) {
  await page.locator(`#est-grid .est[data-scope="${other}"]`).click();
  await page.waitForFunction((id) => document.querySelector('.scope-chip.active')?.dataset.scope === id, other, { timeout: 60000 });
  await ready();
  check((await page.locator('#est-grid .est.on').getAttribute('data-scope')) === other, `clicking ${other} opens it`);
  check((await page.locator('#analyst-link').getAttribute('href')).includes('estate=' + other), 'analyst link keeps the estate');
}

// dark theme redraws the charts
await page.locator('#theme-btn').click();
check(await page.evaluate(() => getComputedStyle(document.body).backgroundColor) === 'rgb(22, 26, 25)', 'dark theme');
check(await page.evaluate(() => Chart.getChart('trend-chart').data.datasets.find(d => d.label === 'Actual').borderColor) === '#62b5ab',
      'trend chart redrawn in the dark palette');
await shot('dark');
await page.locator('#theme-btn').click();

// ── staging ─────────────────────────────────────────────────────────────
const src = await page.evaluate(() => fetch('/forecast/sources').then(r => r.json()));
const staging = src.sources.find(s => s.id === 'staging');
if (!staging || !staging.available) {
  skip('staging checks: ' + ((staging && staging.reason) || 'no staging source'));
} else {
  await page.goto(`${base}/forecast?db=staging`, { waitUntil: 'networkidle' });
  await ready();
  await page.waitForFunction(() => document.querySelectorAll('#est-grid .est').length === 3, null, { timeout: 60000 });
  check(/^Group production is expected to/.test(await page.locator('#headline').innerText()), 'staging: group headline');
  check((await page.locator('#ol-sub').innerText()).startsWith('EA, EB, EC combined'), 'staging: subtitle names the estates summed');
  check(await page.locator('#est-grid .est').count() === 3, 'staging: three estate mini charts');
  const st = await page.locator('body').innerText();
  check(!JARGON.test(st), 'staging: no model jargon on the page');
  await shot('staging');
}

// ── the analyst page is still the full page ─────────────────────────────
await page.goto(`${base}/forecast/analyst`, { waitUntil: 'networkidle' });
await page.waitForSelector('#state-result:not(.hidden)', { timeout: 60000 });
check(await page.locator('#fc-chart').count() === 1 && await page.locator('#kpi-row .kpi-card').count() > 0,
      'analyst page keeps the full chart and KPI row');
check((await page.locator('.view-tabs a.on').innerText()) === 'Analyst', 'analyst page marks its tab');

check(errors.length === 0, `no page errors (${errors.join(' | ')})`);
await browser.close();
console.log(fails.length ? `\n${fails.length} failed` : '\nall passed');
process.exit(fails.length ? 1 : 0);
