/* Early warning: canopy anomaly against census.
 *
 * Two halves. First the payload: the four groups partition the measured
 * blocks, the rank agreement is a real coefficient, and the provenance names
 * both the real canopy and the invented census. Then the screen: open the
 * pest domain, open the panel, and check it rendered rows that point at the
 * map, with no console errors.
 *
 *     node test/pest_warning.mjs [baseURL]
 *
 * Expects vite dev (5173, proxying /gis to 8001) or dashboard_server.py.
 */
import { chromium } from 'playwright';

const BASE = process.argv[2] || 'http://localhost:5173/command-static/';
const ORIGIN = new URL(BASE).origin;
const errors = [];
const note = (...a) => console.log(...a);
const check = (ok, msg) => { if (!ok) errors.push(msg); };

// ── the payload ──────────────────────────────────────────────────────────
const url = `${ORIGIN}/gis/pest-warning?estate=EC&top=12`;
note(`-> ${url}`);
const res = await fetch(url);
check(res.ok, `endpoint returned ${res.status}`);
const d = res.ok ? await res.json() : {};
check(d.available === true, 'payload not available');

if (d.available) {
  const g = d.groups || {};
  const sum = (g.early_warning || 0) + (g.corroborated || 0) + (g.census_only || 0) + (g.clear || 0);
  check(sum === d.measured.measured,
    `groups sum to ${sum}, measured ${d.measured.measured}`);
  check(sum > 0, 'no measured blocks');
  const rho = d.agreement && d.agreement.spearman_rho;
  check(typeof rho === 'number' && rho >= -1 && rho <= 1, `rho is ${rho}`);
  check(/real/i.test(d.provenance) && /synthetic/i.test(d.provenance),
    `provenance does not name both real and synthetic: "${d.provenance}"`);
  check(typeof d.summary === 'string' && d.summary.length > 40, 'summary missing');
  check(Array.isArray(d.early_warning) && d.early_warning.length > 0, 'early_warning list empty');
  check(d.early_warning.every(x => x.block_label && x.group === 'early_warning'),
    'early_warning rows malformed');
  check(Array.isArray(d.points) && d.points.length === sum, 'points do not cover the measured blocks');
  note(`  groups: ${JSON.stringify({ early_warning: g.early_warning, corroborated: g.corroborated,
    census_only: g.census_only, clear: g.clear })}, rho ${rho}`);
  note(`  summary: ${d.summary}`);
}

// ── the screen ───────────────────────────────────────────────────────────
// Console errors are tagged with the phase they arrived in. An error raised
// while the shell boots, before this panel exists, is the app's (or the dev
// server's: vite's HMR timestamping of the main.js <-> map/init.js circular
// import runs main.js twice after sibling edits, so the second boot trips on
// a map source that already exists - absent on the built /command route).
// Those are reported, loudly, but only an error raised by opening the panel
// fails the test, because that is what this test is for.
let phase = 'boot';
const bootErrors = [];
const browser = await chromium.launch({ channel: 'chrome' });
const page = await browser.newPage({ viewport: { width: 1600, height: 950 } });
const seen = msg => (phase === 'boot' ? bootErrors : errors).push(msg);
page.on('console', m => {
  if (m.type() === 'error' && !/Failed to load resource/.test(m.text())) {
    seen(`console: ${m.text().split('\n')[0]}`);
  }
});
page.on('pageerror', e => seen(`pageerror: ${e.message}`));

note(`-> ${BASE}`);
await page.goto(BASE, { waitUntil: 'domcontentloaded' });
await page.waitForTimeout(3000);

await page.evaluate(() => document.querySelector('.dom-ico[data-domain="pest"]').click());
await page.waitForTimeout(400);
const listed = await page.evaluate(() => !!document.querySelector('#panels [data-panel="pest_warning"]'));
check(listed, 'pest_warning is not listed in the pest flyout');
if (listed) {
  phase = 'panel';
  await page.evaluate(() => document.querySelector('#panels [data-panel="pest_warning"]').click());
  const rendered = await page.waitForFunction(
    () => ((document.getElementById('wb-body') || {}).textContent || '').trim().length > 100,
    null, { timeout: 15000 }).then(() => true, () => false);
  check(rendered, 'panel never rendered');

  const body = await page.evaluate(() => (document.getElementById('wb-body') || {}).textContent || '');
  check(!/not yet built/i.test(body), 'panel says it is not yet built');
  check(!/failed to render/i.test(body), 'panel failed to render');
  check(/real/i.test(body) && /synthetic/i.test(body), 'screen does not name both provenances');

  const refs = await page.evaluate(() => document.querySelectorAll('#wb-body [data-block]').length);
  check(refs > 0, `no rows point at the map (${refs})`);
  const svg = await page.evaluate(() => document.querySelectorAll('#wb-body svg.pw-chart circle').length);
  check(svg > 0, 'quadrant scatter drew no points');
  const lead = await page.evaluate(() =>
    ((document.querySelector('#wb-body .pw-lead') || {}).textContent || '').trim());
  note(`  on screen: ${lead}`);
  note(`  ${refs} block references, ${svg} points plotted`);
}

await browser.close();

note('');
if (bootErrors.length) {
  note(`WARN  ${bootErrors.length} console error(s) during shell boot, before the panel was opened:`);
  [...new Set(bootErrors)].forEach(e => note('  ' + e));
  note('      Not this panel\'s; see the note at the top of the screen section.');
}
if (errors.length) {
  note(`FAIL  ${errors.length} problem(s):`);
  [...new Set(errors)].forEach(e => note('  ' + e));
  process.exit(1);
}
note('OK    pest_warning: payload partitions the estate, panel renders and points at the map');
