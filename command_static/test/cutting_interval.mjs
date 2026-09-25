/* Realised cutting interval, checked end to end.
 *
 * The endpoint must measure every block from the client's own cutting days
 * and the panel must render those blocks as map references. Modelled on
 * smoke.mjs: real page, real server, fails on any console error.
 *
 *     node test/cutting_interval.mjs [baseURL]
 *
 * Expects the vite dev server (which proxies /gis to dashboard_server.py on
 * 8001) or the FastAPI server itself. BASE is the page URL; the API is
 * resolved against its origin.
 */
import { chromium } from 'playwright';
import { firstSentence } from '../src/lib/fmt.js';

const BASE = process.argv[2] || 'http://localhost:5173/command-static/';
const SHOT = process.argv[3] || null;
const errors = [];
const skipped = [];
const note = (...a) => console.log(...a);

/* Under vite dev, src/map/init.js imports `boot` from ../main.js, so once any
   file save has been through HMR vite stamps that import as main.js?t=... -
   a second instance of the entry beside the one the HTML loads - and boot()
   runs twice against one map. That is a dev-server artefact of the circular
   import, not a panel fault, and it goes away on a vite restart or a build.
   Only that exact signature is set aside; everything else still fails. */
const HMR_DOUBLE_BOOT = /Source "estates" already exists[\s\S]*main\.js\?t=\d+/;
const fail = msg => { errors.push(msg); note('  x ' + msg); };
const expect = (cond, msg) => { if (!cond) fail(msg); };

// ── the API ──────────────────────────────────────────────────────────────
const api = new URL('/gis/cutting-interval?estate=EC&top=12', BASE).href;
note(`-> ${api}`);
const t0 = Date.now();
const res = await fetch(api);
expect(res.ok, `endpoint answered ${res.status}`);
const d = res.ok ? await res.json() : {};
note(`  answered in ${Date.now() - t0}ms`);
expect(d.available === true, `available: ${d.available} ${d.reason || ''}`);
expect(typeof d.summary === 'string' && d.summary.length > 40, 'summary sentence missing');
expect(d.totals && d.totals.blocks === 291, `blocks measured: ${d.totals && d.totals.blocks}, expected 291`);
expect(Array.isArray(d.series) && d.series.length === 5, `months in series: ${d.series && d.series.length}, expected 5`);
for (const m of d.series || []) {
  expect(m.median >= 3 && m.median <= 20, `${m.label} median ${m.median} d outside 3..20`);
}
expect(d.totals && d.totals.median_round >= 3 && d.totals.median_round <= 20,
  `overall median ${d.totals && d.totals.median_round} outside 3..20`);
expect(d.target && typeof d.target.median === 'number', 'synthetic target median missing');
expect(/real/.test(d.provenance || '') && /synthetic/.test(d.provenance || ''),
  'provenance must name the real/synthetic split');
expect((d.most_stretched || []).length > 0 && d.most_stretched[0].block_label, 'most_stretched empty or unlabelled');
expect((d.longest_gap || []).length > 0 && d.longest_gap[0].block_label, 'longest_gap empty or unlabelled');
expect((d.most_stretched || []).length <= 12, 'top=12 not honoured');
expect(d.closure && d.closure.days >= 3, 'the Lebaran closure was not detected');
note(`  ${d.totals && d.totals.blocks} blocks, ${d.totals && d.totals.intervals} rounds, medians ${
  (d.series || []).map(m => `${m.label.slice(0, 3)} ${m.median}`).join(', ')}`);
note(`  summary: ${d.summary}`);

// ── the page ─────────────────────────────────────────────────────────────
const browser = await chromium.launch({ channel: 'chrome' });
const page = await browser.newPage({ viewport: { width: 1600, height: 950 } });
page.on('console', m => {
  if (m.type() !== 'error' || /Failed to load resource/.test(m.text())) return;
  if (HMR_DOUBLE_BOOT.test(m.text())) { skipped.push(m.text().split('\n')[0]); return; }
  errors.push(`console: ${m.text()}`);
});
page.on('pageerror', e => errors.push(`pageerror: ${e.message}`));
page.on('response', r => {
  if (r.status() >= 400 && !/arcgisonline|fonts\.|favicon/.test(r.url())) {
    errors.push(`http ${r.status()}: ${new URL(r.url()).pathname}`);
  }
});

note(`-> ${BASE}`);
await page.goto(BASE, { waitUntil: 'domcontentloaded' });
await page.waitForFunction(() => {
  const el = document.getElementById('intro');
  return !el || el.hidden || getComputedStyle(el).opacity === '0'
      || getComputedStyle(el).display === 'none';
}, null, { timeout: 30000 }).catch(() => fail('intro curtain never lifted'));
await page.waitForTimeout(3000);

const hasDomain = await page.evaluate(() => {
  const el = document.querySelector('.dom-ico[data-domain="harvesting"]');
  if (!el) return false;
  el.click();
  return true;
});
expect(hasDomain, 'harvesting domain icon not found');
await page.waitForTimeout(400);

const hasPanel = await page.evaluate(() => {
  const el = document.querySelector('#panels [data-panel="cutting_interval"]');
  if (!el) return false;
  el.click();
  return true;
});
expect(hasPanel, 'cutting_interval panel button not listed under harvesting');

const rendered = await page.waitForFunction(
  () => ((document.getElementById('wb-body') || {}).textContent || '').trim().length > 100
     && !/^Loading/.test(((document.getElementById('wb-body') || {}).textContent || '').trim()),
  null, { timeout: 20000 }).then(() => true, () => false);
expect(rendered, 'panel never rendered more than 100 characters');

const body = await page.evaluate(() => ({
  text: (document.getElementById('wb-body') || {}).textContent || '',
  refs: document.querySelectorAll('#wb-body [data-block]').length,
  svg: document.querySelectorAll('#wb-body svg').length,
  tables: document.querySelectorAll('#wb-body table.tbl').length,
  real: document.querySelectorAll('#wb-body .prov.real').length,
  synth: document.querySelectorAll('#wb-body .prov.synthetic').length,
  title: (document.getElementById('wb-title') || {}).textContent || '',
}));
expect(!/not yet built/i.test(body.text), 'panel still shows the placeholder');
expect(!/failed to render/i.test(body.text), 'panel failed to render');
expect(body.refs > 0, `no block references in the panel (${body.refs})`);
expect(body.svg > 0, 'no SVG chart in the panel');
expect(body.real > 0 && body.synth > 0, 'real/synthetic badges missing on screen');
expect(body.text.includes(firstSentence(d.summary)), 'headline sentence not on screen verbatim');
note(`  title: ${body.title}`);
note(`  ${body.refs} block refs, ${body.tables} tables, ${body.svg} chart, ${body.real} real / ${body.synth} synthetic badges`);
note(`  body: ${body.text.replace(/\s+/g, ' ').trim().slice(0, 160)}…`);

if (SHOT) {
  // Three frames down the dock: header and chart, the tables, the notes.
  for (let i = 0; i < 3; i++) {
    await page.evaluate(k => {
      const b = document.getElementById('wb-body');
      if (b) b.scrollTop = k * (b.clientHeight - 40);
    }, i);
    await page.waitForTimeout(150);
    await page.screenshot({ path: `${SHOT}/cutting_interval_${i + 1}.png` });
  }
  note(`  screenshots in ${SHOT}`);
}

await browser.close();

if (skipped.length) note(`  set aside ${skipped.length} HMR double-boot error(s): ${[...new Set(skipped)].join(' | ')}`);
note('');
if (errors.length) {
  note(`FAIL  ${errors.length} problem(s):`);
  [...new Set(errors)].forEach(e => note('  ' + e));
  process.exit(1);
}
note('OK    cutting interval: endpoint and panel');
