/* ffa_decay: the endpoint's shape and honesty, then the dock render.
 *
 *     node test/ffa.mjs [baseURL]
 *
 * Expects the vite dev server (proxying /gis to dashboard_server.py) at the
 * base, which defaults to http://localhost:5173/command-static/.
 */
import { chromium } from 'playwright';
import { firstSentence } from '../src/lib/fmt.js';

const BASE = process.argv[2] || 'http://localhost:5173/command-static/';
const errors = [];
const note = (...a) => console.log(...a);
const check = (ok, msg) => { if (!ok) errors.push(msg); };

// ── the endpoint ─────────────────────────────────────────────────────────
const api = new URL('/gis/ffa?estate=EC&top=12', BASE).href;
note(`-> ${api}`);
const res = await fetch(api);
check(res.ok, `endpoint answered ${res.status}`);
const d = res.ok ? await res.json() : {};
const raw = JSON.stringify(d);

check(d.available === true, 'endpoint not available: ' + (d.reason || ''));
check(typeof d.summary === 'string' && d.summary.length > 40, 'summary sentence missing');
check(typeof d.provenance === 'string' && /synthetic/.test(d.provenance), 'provenance does not say synthetic');
check(typeof d.caveat === 'string' && /planted/i.test(d.caveat), 'caveat does not name the planted rule');
check(!/is_planted_anomaly/.test(raw), 'the answer key leaked into the payload');

const trips = d.totals && d.totals.trips;
check(trips >= 18000 && trips <= 22000, `expected ~19k trips, got ${trips}`);

const monotone = (rows, tol) => rows.every((r, i) => i === 0 || r.mean_ffa_pct >= rows[i - 1].mean_ffa_pct - tol);
check(Array.isArray(d.buckets) && d.buckets.length >= 3, 'cut-to-mill buckets missing');
check(Array.isArray(d.turnaround_buckets) && d.turnaround_buckets.length >= 3, 'turnaround buckets missing');
if (Array.isArray(d.buckets)) {
  check(d.buckets.reduce((a, b) => a + b.trips, 0) === trips, 'cut-to-mill buckets do not sum to the trip count');
  check(d.buckets.every(b => typeof b.mean_ffa_pct === 'number' && b.label), 'a cut-to-mill bucket lacks its figures');
  // The wait was not planted, so only ask for no reversal here.
  check(monotone(d.buckets, 0.02), 'FFA is not monotone-ish across cut-to-mill buckets');
}
if (Array.isArray(d.turnaround_buckets)) {
  check(d.turnaround_buckets.reduce((a, b) => a + b.trips, 0) === trips, 'turnaround buckets do not sum to the trip count');
  // FFA was planted against turnaround, so it must climb bucket by bucket.
  check(monotone(d.turnaround_buckets, 0.0), 'FFA does not rise with turnaround, which was planted');
}

const rec = d.recovery || {};
check(typeof rec.slope_planted === 'number' && typeof rec.slope_recovered === 'number', 'recovery block incomplete');
if (typeof rec.slope_planted === 'number' && typeof rec.slope_recovered === 'number') {
  const ratio = rec.slope_recovered / rec.slope_planted;
  check(ratio > 0.85 && ratio < 1.15, `recovered slope ${rec.slope_recovered} is far from planted ${rec.slope_planted}`);
}
check(d.threshold && typeof d.threshold.ffa_pct === 'number' && d.threshold.placeholder === true, 'threshold is not a labelled placeholder');
check(d.delay_split && typeof d.delay_split.queue_share_of_turnaround_pct === 'number', 'queue vs road split missing');
check(Array.isArray(d.worst_ffa_blocks) && d.worst_ffa_blocks.length > 0 && d.worst_ffa_blocks.every(b => b.block_label && b.division_code !== undefined),
  'worst FFA blocks missing or without block_label');
check(Array.isArray(d.longest_delay_blocks) && d.longest_delay_blocks.length > 0, 'longest delay blocks missing');
check(Array.isArray(d.routes) && d.routes.length > 0, 'routes missing');
check(d.scatter && Array.isArray(d.scatter.points) && d.scatter.points.length > 100, 'scatter missing');

note(`  trips ${trips}; slope ${rec.slope_recovered}/h vs planted ${rec.slope_planted}/h (${rec.recovered_pct}%)`);
note('  cut-to-mill: ' + (d.buckets || []).map(b => `${b.label} ${b.mean_ffa_pct}% (${b.trips})`).join(' | '));
note('  turnaround:  ' + (d.turnaround_buckets || []).map(b => `${b.label} ${b.mean_ffa_pct}% (${b.trips})`).join(' | '));
note(`  over ${d.threshold && d.threshold.ffa_pct}%: ${d.totals && d.totals.over_threshold_pct}% of tonnes; queue ${d.delay_split && d.delay_split.queue_share_of_turnaround_pct}% of the ticket`);

// ── the page ─────────────────────────────────────────────────────────────
const browser = await chromium.launch({ channel: 'chrome' });
const page = await browser.newPage({ viewport: { width: 1600, height: 950 } });
page.on('console', m => {
  if (m.type() === 'error' && !/Failed to load resource/.test(m.text())) {
    errors.push(`console: ${m.text()}`);
  }
});
page.on('pageerror', e => errors.push(`pageerror: ${e.message}`));
page.on('response', r => {
  if (r.status() >= 400 && !/arcgisonline|fonts\.|favicon/.test(r.url())) {
    errors.push(`http ${r.status()}: ${new URL(r.url()).pathname}`);
  }
});

note(`-> ${BASE}`);
await page.goto(BASE, { waitUntil: 'domcontentloaded' });
await page.waitForTimeout(3000);

const domOk = await page.evaluate(() => {
  const el = document.querySelector('.dom-ico[data-domain="transport"]');
  if (!el) return false;
  el.click();
  return true;
});
check(domOk, 'no transport domain icon');
await page.waitForTimeout(400);

const panOk = await page.evaluate(() => {
  const el = document.querySelector('#panels [data-panel="ffa"]');
  if (!el) return false;
  el.click();
  return true;
});
check(panOk, 'no ffa panel in the transport flyout');

const rendered = await page.waitForFunction(
  () => ((document.getElementById('wb-body') || {}).textContent || '').trim().length > 100
     && !/^Loading/.test(((document.getElementById('wb-body') || {}).textContent || '').trim()),
  null, { timeout: 15000 }).then(() => true, () => false);
check(rendered, 'ffa panel never rendered');

const body = await page.evaluate(() => ({
  text: (document.getElementById('wb-body') || {}).textContent || '',
  blocks: document.querySelectorAll('#wb-body [data-block]').length,
  svg: document.querySelectorAll('#wb-body svg.st-chart').length,
  tables: document.querySelectorAll('#wb-body table.tbl').length,
  kpis: document.querySelectorAll('#wb-body .kpi').length,
}));
check(!/not yet built/i.test(body.text), 'panel says it is not yet built');
check(!/failed to render/i.test(body.text), 'panel failed to render');
check(!/Backend module not yet in place/i.test(body.text), 'placeholder body still showing');
check(body.blocks > 0, 'no block rows point at the map');
check(body.svg >= 1, 'no scatter drawn');
check(body.kpis >= 4, `only ${body.kpis} KPIs`);
check(d.summary && body.text.includes(firstSentence(d.summary)), 'headline sentence not on screen');
note(`  dock: ${body.blocks} block rows, ${body.tables} tables, ${body.svg} chart, ${body.kpis} KPIs`);
note('  on screen: ' + body.text.replace(/\s+/g, ' ').trim().slice(0, 160));

await browser.close();

note('');
if (errors.length) {
  note(`FAIL  ${errors.length} problem(s):`);
  [...new Set(errors)].forEach(e => note('  ' + e));
  process.exit(1);
}
note('OK    ffa endpoint and panel');
