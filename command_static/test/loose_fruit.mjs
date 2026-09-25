/* Loose fruit recovery: the endpoint's figures are sane and the panel renders
 * against the live source.
 *
 *     node test/loose_fruit.mjs [baseURL] [payload.json]
 *
 * The default base is the vite dev server, which serves the source and
 * proxies /gis to the FastAPI process on 8001, so both must be running. Pass
 * http://127.0.0.1:8001/command to run the same checks against the built page.
 *
 * The optional payload file is for a stuck backend: when given, the figure
 * checks run against that JSON (dump it with gis.loose_fruit.position) and
 * the browser's /gis/loose-fruit request is answered with it, so the panel
 * is still proven against the real numbers.
 */
import { readFileSync } from 'node:fs';
import { chromium } from 'playwright';

const BASE = process.argv[2] || 'http://localhost:5173/command-static/';
const PAYLOAD = process.argv[3] || null;
const errors = [];
const note = (...a) => console.log(...a);
const check = (ok, msg) => { if (!ok) errors.push(msg); };

// ── the endpoint ─────────────────────────────────────────────────────────
const api = new URL('/gis/loose-fruit?estate=EC&top=12', BASE).href;
let d = {}, ms = 0;
if (PAYLOAD) {
  note(`-> ${PAYLOAD} (payload file; the endpoint is not fetched)`);
  d = JSON.parse(readFileSync(PAYLOAD, 'utf8'));
} else {
  note(`-> ${api}`);
  const t0 = Date.now();
  const res = await fetch(api);
  ms = Date.now() - t0;
  check(res.ok, `endpoint returned ${res.status}`);
  d = res.ok ? await res.json() : {};
  check(ms < 2000, `endpoint took ${ms}ms`);
}

check(d.available === true, 'available is not true');
check(typeof d.summary === 'string' && d.summary.length > 60, 'summary sentence missing');
for (const k of ['totals', 'distribution', 'benchmark', 'by_month', 'by_division',
                 'worst_blocks', 'best_blocks', 'biggest_falls', 'value', 'provenance',
                 'note', 'caveat']) {
  check(d[k] !== undefined && d[k] !== null, `payload lacks ${k}`);
}

const t = d.totals || {};
check(t.blocks === 291, `blocks: expected 291, got ${t.blocks}`);
check(t.loose_per_bunch > 0.5 && t.loose_per_bunch < 5, `estate loose per bunch ${t.loose_per_bunch}`);
check(t.loose_fruits > 1e6, `loose fruits ${t.loose_fruits}`);
check(t.bunches > 1e6, `bunches ${t.bunches}`);
check(t.records > 100000, `records ${t.records}`);
check(t.records_with_count_pct > 0 && t.records_with_count_pct < 100, `records_with_count_pct ${t.records_with_count_pct}`);

// The window is EC's recording window in its database snapshot: contiguous
// calendar months from January 2025, however far the snapshot now reaches.
const months = (d.by_month || []).map(m => m.month);
const contiguous = months.every((m, i) => i === 0 || (() => {
  const [y, mo] = months[i - 1].split('-').map(Number);
  return m === `${y + (mo === 12 ? 1 : 0)}-${String(mo % 12 + 1).padStart(2, '0')}`;
})());
check(months[0] === '2025-01' && months.length >= 5 && contiguous,
  `months ${JSON.stringify(months)}`);
check(JSON.stringify(months) === JSON.stringify((d.window || {}).months || []),
  'by_month does not cover the window');
for (const m of d.by_month || []) {
  check(m.loose_per_bunch > 0.5 && m.loose_per_bunch < 5, `${m.month}: ratio ${m.loose_per_bunch}`);
  check(m.blocks > 250, `${m.month}: only ${m.blocks} blocks`);
}

const q = d.distribution || {};
check(q.n === 291, `distribution n ${q.n}`);
check(q.min < q.q1 && q.q1 < q.median && q.median < q.q3 && q.q3 < q.max,
  `quartiles not ordered: ${JSON.stringify(q)}`);
check(q.min > 0.3 && q.max < 5, `distribution range ${q.min}..${q.max}`);

const b = d.benchmark || {};
check(b.loose_per_bunch === q.q3, `benchmark ${b.loose_per_bunch} is not the upper quartile ${q.q3}`);
check(b.recovery_pct > 50 && b.recovery_pct <= 100, `recovery ${b.recovery_pct}%`);
check(b.blocks_below + b.blocks_at_or_above === 291, 'benchmark split does not sum to 291');
check(b.gap_fruits > 0, `gap fruits ${b.gap_fruits}`);
check(/measured/.test(b.basis || ''), 'benchmark basis does not say it is measured');

const rows = (list, name, n) => {
  check(Array.isArray(list) && list.length === n, `${name}: expected ${n} rows, got ${(list || []).length}`);
  for (const r of list || []) {
    check(!!r.block_label && !!r.division_code, `${name}: row lacks block_label/division_code`);
    check(r.loose_per_bunch > 0 && r.loose_per_bunch < 5, `${name}: ${r.block_label} ratio ${r.loose_per_bunch}`);
  }
};
rows(d.worst_blocks, 'worst_blocks', 12);
rows(d.best_blocks, 'best_blocks', 12);
if (d.worst_blocks?.length && d.best_blocks?.length) {
  check(d.worst_blocks[0].loose_per_bunch < d.best_blocks[0].loose_per_bunch, 'worst is not below best');
  check(d.worst_blocks[0].loose_per_bunch === q.min, 'worst block is not the minimum');
  check(d.best_blocks[0].loose_per_bunch === q.max, 'best block is not the maximum');
  check(d.worst_blocks.every(r => r.recovery_pct < 100), 'a worst block sits above the benchmark');
  check(d.best_blocks.every(r => r.recovery_pct >= 100), 'a best block sits below the benchmark');
}
check((d.biggest_falls || []).length > 0, 'no biggest_falls');
check((d.biggest_falls || []).every(r => r.delta < 0 && r.block_label), 'a fall row is not a fall');
check((d.by_division || []).length >= 5, `by_division has ${(d.by_division || []).length} rows`);

const v = d.value || {};
check(v.gap_fruits === b.gap_fruits, 'value.gap_fruits differs from benchmark.gap_fruits');
check(v.gap_t > 0 && v.gap_idr > 0, `value gap_t ${v.gap_t} gap_idr ${v.gap_idr}`);
check(/placeholder/.test(v.kg_per_loose_fruit_provenance || ''), 'fruit weight is not labelled a placeholder');
check(/assum/.test(v.price_provenance || ''), 'price is not labelled assumed');
check(/^real/.test(d.provenance), `provenance does not lead with real: ${d.provenance}`);
if (d.synthetic_comparison) {
  check(d.synthetic_comparison.provenance === 'synthetic', 'synthetic comparison not labelled');
}

note(`  ${ms}ms · ${t.blocks} blocks · ${t.loose_per_bunch}/bunch · quartiles ${q.q1}/${q.median}/${q.q3}`
  + ` · benchmark ${b.loose_per_bunch} · recovery ${b.recovery_pct}% · gap ${b.gap_fruits}`);
note(`  months: ${(d.by_month || []).map(m => `${m.label} ${m.loose_per_bunch}`).join(', ')}`);
note(`  worst: ${(d.worst_blocks || []).slice(0, 3).map(r => `${r.block_label} ${r.loose_per_bunch}`).join(', ')}`);
note(`  best:  ${(d.best_blocks || []).slice(0, 3).map(r => `${r.block_label} ${r.loose_per_bunch}`).join(', ')}`);

// ── the panel ────────────────────────────────────────────────────────────
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

if (PAYLOAD) {
  await page.route('**/gis/loose-fruit*', r => r.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify(d) }));
}

note(`-> ${BASE}`);
await page.goto(BASE, { waitUntil: 'domcontentloaded' });
await page.waitForFunction(() => {
  const el = document.getElementById('intro');
  return !el || el.hidden || getComputedStyle(el).opacity === '0'
      || getComputedStyle(el).display === 'none';
}, null, { timeout: 30000 }).catch(() => errors.push('intro curtain never lifted'));
await page.waitForTimeout(3000);

await page.evaluate(() => document.querySelector('.dom-ico[data-domain="harvesting"]').click());
await page.waitForTimeout(400);
const listed = await page.evaluate(() => !!document.querySelector('#panels [data-panel="loose_fruit"]'));
check(listed, 'harvesting flyout lists no loose_fruit panel');

if (listed) {
  const t1 = Date.now();
  await page.evaluate(() => document.querySelector('#panels [data-panel="loose_fruit"]').click());
  const rendered = await page.waitForFunction(
    () => ((document.getElementById('wb-body') || {}).textContent || '').trim().length > 100,
    null, { timeout: 15000 }).then(() => true, () => false);
  check(rendered, 'panel never rendered');
  note(`  panel painted in ${Date.now() - t1}ms`);

  const body = await page.evaluate(() => (document.getElementById('wb-body') || {}).textContent || '');
  check(!/not yet built/.test(body), 'panel says "not yet built"');
  check(!/failed to render/.test(body), 'panel says "failed to render"');
  check(/per bunch/i.test(body), 'panel does not show a per-bunch figure');
  check(/best quarter/i.test(body), 'panel does not name the benchmark');
  check(/placeholder/i.test(body), 'panel does not label the fruit weight a placeholder');

  const refs = await page.evaluate(() => document.querySelectorAll('#wb-body [data-block]').length);
  check(refs > 0, `no [data-block] rows in the panel (${refs})`);
  const charts = await page.evaluate(() => document.querySelectorAll('#wb-body svg.lf-chart').length);
  check(charts === 1, `expected one month chart, found ${charts}`);
  const lead = await page.evaluate(() =>
    ((document.querySelector('#wb-body .lf-lead') || {}).textContent || '').replace(/\s+/g, ' ').trim());
  check(lead.length > 60, 'summary sentence is not on screen');
  note(`  ${refs} rows point at the map`);
  note(`  on screen: ${lead}`);
}

await browser.close();

note('');
if (errors.length) {
  note(`FAIL  ${errors.length} problem(s):`);
  [...new Set(errors)].forEach(e => note('  ' + e));
  process.exit(1);
}
note('OK    loose fruit endpoint and panel');
