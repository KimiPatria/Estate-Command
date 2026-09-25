/* abw_trend panel test: the endpoint's shape, then the panel in the real page.
 *
 *     node test/abw_trend.mjs [baseURL] [screenshotPath]
 *
 * Expects the vite dev server (which proxies /gis to the FastAPI process on
 * 8001) - or the FastAPI process itself, with the built bundle, at baseURL.
 */
import { chromium } from 'playwright';

const BASE = process.argv[2] || 'http://localhost:5173/command-static/';
const SHOT = process.argv[3] || '';
const errors = [];
const note = (...a) => console.log(...a);
const check = (ok, what) => { if (!ok) errors.push(what); };

// ── the endpoint ─────────────────────────────────────────────────────────
const api = new URL('/gis/abw-trend?estate=EC&top=12', BASE).toString();
note(`-> ${api}`);
const res = await fetch(api);
if (!res.ok) {
  note(`FAIL  endpoint answered ${res.status}`);
  process.exit(1);
}
const d = await res.json();
const raw = JSON.stringify(d);

check(d.available === true, `available: ${d.available} (${d.reason || ''})`);
check(typeof d.summary === 'string' && d.summary.length > 40, 'summary sentence missing');
check(d.totals && d.totals.blocks === 291, `totals.blocks: expected 291, got ${d.totals && d.totals.blocks}`);
check(Array.isArray(d.blocks) && d.blocks.length === 291, `blocks: expected 291 rows, got ${d.blocks && d.blocks.length}`);
check(Array.isArray(d.months) && d.months.length === 5, `months: expected 5, got ${d.months && d.months.length}`);
check(Array.isArray(d.estate_series) && d.estate_series.length === 5,
  `estate_series: expected 5, got ${d.estate_series && d.estate_series.length}`);
for (const p of d.estate_series || []) {
  check(p.abw_kg > 5 && p.abw_kg < 20, `estate ABW ${p.month} out of 5-20 kg: ${p.abw_kg}`);
}
check(d.totals && d.totals.estate_abw_kg > 5 && d.totals.estate_abw_kg < 20,
  `estate ABW out of 5-20 kg: ${d.totals && d.totals.estate_abw_kg}`);
check(/calibrat/i.test(d.caveat || ''), 'caveat does not mention the calibration');
check(/back-solved/i.test(d.caveat || '') && /23\.0 t\/ha/i.test(d.caveat || ''),
  'caveat does not carry the CSV header wording');
check(d.calibration && /back-solved/i.test(d.calibration.header || ''), 'calibration.header missing');
check(d.calibration && /weighbridge ticket/i.test(d.calibration.ask_client || ''),
  'calibration.ask_client does not name the ticket');
check(Array.isArray(d.falling) && d.falling.length > 0 && d.falling.length <= 12,
  `falling: ${d.falling && d.falling.length} rows`);
check(Array.isArray(d.rising) && d.rising.length > 0 && d.rising.length <= 12,
  `rising: ${d.rising && d.rising.length} rows`);
for (const b of [...(d.falling || []).slice(0, 3), ...(d.rising || []).slice(0, 3)]) {
  check(b.block_label && b.division_code !== undefined, `block row lacks block_label/division_code: ${JSON.stringify(b).slice(0, 80)}`);
  check(Array.isArray(b.series) && b.series.length === 5, `block ${b.block_label}: series not five months`);
}
check(Array.isArray(d.division_series) && d.division_series.length >= 2, 'division_series missing');
check(d.age_curve && Array.isArray(d.age_curve.buckets) && d.age_curve.buckets.length >= 2, 'age_curve missing');
check(d.age_curve && d.age_curve.planted_pct_per_year === 3.5, 'age_curve.planted_pct_per_year is not 3.5');
check(d.recovered_vs_planted && d.recovered_vs_planted.trend && d.recovered_vs_planted.age,
  'recovered_vs_planted missing');
check(typeof d.provenance === 'string' && /derived|synthetic/.test(d.provenance), 'provenance missing');
check(!raw.includes('is_planted_anomaly'), 'payload leaks is_planted_anomaly');
note(`  ${d.totals.blocks} blocks, ${d.totals.trips} trips, ${d.months.length} months, estate ${d.totals.estate_abw_kg} kg, moving ${d.totals.moving_blocks}`);
note(`  summary: ${d.summary}`);

// ── the panel ────────────────────────────────────────────────────────────
const browser = await chromium.launch({ channel: 'chrome' });
const page = await browser.newPage({ viewport: { width: 1600, height: 950 } });
// Console errors are split by phase. An error raised while the page boots,
// before this panel is touched, belongs to the shell (the vite dev page can
// boot the map twice under HMR) and is reported as a warning so it is seen;
// an error raised after the panel is opened is this panel's and fails the run.
let phase = 'boot';
const bootErrors = [];
const sink = msg => (phase === 'boot' ? bootErrors : errors).push(msg);
page.on('console', m => {
  if (m.type() === 'error' && !/Failed to load resource/.test(m.text())) {
    sink(`console: ${m.text().split('\n')[0]}`);
  }
});
page.on('pageerror', e => sink(`pageerror: ${e.message}`));
page.on('response', r => {
  if (r.status() >= 400 && !/arcgisonline|fonts\.|favicon/.test(r.url())) {
    sink(`http ${r.status()}: ${new URL(r.url()).pathname}`);
  }
});

note(`-> ${BASE}`);
await page.goto(BASE, { waitUntil: 'domcontentloaded' });
await page.waitForFunction(() => {
  const el = document.getElementById('intro');
  return !el || el.hidden || getComputedStyle(el).opacity === '0'
      || getComputedStyle(el).display === 'none';
}, null, { timeout: 30000 }).catch(() => errors.push('intro curtain never lifted'));
await page.waitForTimeout(3000);
if (bootErrors.length) {
  note(`  warn: ${bootErrors.length} console error(s) during boot, before this panel was touched:`);
  [...new Set(bootErrors)].forEach(e => note('    ' + e));
}
phase = 'panel';

const domOk = await page.evaluate(() => {
  const el = document.querySelector('.dom-ico[data-domain="harvesting"]');
  if (!el) return false;
  el.click();
  return true;
});
check(domOk, 'no harvesting domain icon');
await page.waitForTimeout(400);
const panelOk = await page.evaluate(() => {
  const el = document.querySelector('#panels [data-panel="abw_trend"]');
  if (!el) return false;
  el.click();
  return true;
});
check(panelOk, 'harvesting flyout lists no abw_trend panel');

const rendered = await page.waitForFunction(
  () => ((document.getElementById('wb-body') || {}).textContent || '').trim().length > 100,
  null, { timeout: 20000 }).then(() => true, () => false);
check(rendered, 'panel never rendered');

const body = await page.evaluate(() => {
  const el = document.getElementById('wb-body');
  return {
    text: el ? el.textContent : '',
    refs: el ? el.querySelectorAll('[data-block]').length : 0,
    svgs: el ? el.querySelectorAll('svg').length : 0,
    tables: el ? el.querySelectorAll('table.tbl').length : 0,
    title: (document.getElementById('wb-title') || {}).textContent || '',
  };
});
check(!/not yet built/i.test(body.text), 'panel says "not yet built"');
check(!/failed to render/i.test(body.text), 'panel says "failed to render"');
check(!/Backend module not yet in place/i.test(body.text), 'stub still in place');
check(body.refs > 0, `no [data-block] rows in the panel (got ${body.refs})`);
check(body.svgs >= 3, `expected the trend chart and sparklines, got ${body.svgs} svg`);
check(/calibration/i.test(body.text), 'calibration caveat not on screen');
check(/weighbridge ticket/i.test(body.text), 'the ask to the client is not on screen');
note(`  panel "${body.title}": ${body.text.trim().length} chars, ${body.refs} block rows, ${body.svgs} svg, ${body.tables} tables`);
note(`  on screen: ${body.text.replace(/\s+/g, ' ').trim().slice(0, 160)}…`);

if (SHOT) {
  await page.waitForTimeout(1200);   // let the dock finish sliding in
  await page.screenshot({ path: SHOT });
  note(`  screenshot: ${SHOT}`);
}
await browser.close();

note('');
if (errors.length) {
  note(`FAIL  ${errors.length} problem(s):`);
  [...new Set(errors)].forEach(e => note('  ' + e));
  process.exit(1);
}
note('OK    abw_trend: endpoint shape and panel render');
