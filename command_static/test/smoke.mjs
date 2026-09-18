/* Estate Command smoke test.
 *
 * The build only proves the modules parse and resolve. It says nothing about
 * module-init order or DOM wiring, so this loads the real page against the
 * real server, fails on any console error, and walks every panel in every
 * domain.
 *
 *     node test/smoke.mjs [baseURL]
 *
 * Expects dashboard_server.py to be running.
 */
import { chromium } from 'playwright';

const BASE = process.argv[2] || 'http://127.0.0.1:8001';
const errors = [];
const note = (...a) => console.log(...a);

const browser = await chromium.launch({ channel: 'chrome' });
const page = await browser.newPage({ viewport: { width: 1600, height: 950 } });

page.on('console', m => {
  // The bare "Failed to load resource" line duplicates the response hook
  // below, which names the URL; keep the useful one.
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

note(`-> ${BASE}/command`);
await page.goto(`${BASE}/command`, { waitUntil: 'domcontentloaded' });

await page.waitForFunction(() => {
  const el = document.getElementById('intro');
  return !el || el.hidden || getComputedStyle(el).opacity === '0'
      || getComputedStyle(el).display === 'none';
}, null, { timeout: 30000 }).catch(() => errors.push('intro curtain never lifted'));

await page.waitForTimeout(2500);   // let boot() settle its fetches

// ── the shell, as boot leaves it ─────────────────────────────────────────
const shell = await page.evaluate(() => ({
  domains:  document.querySelectorAll('.dom-ico').length,
  metrics:  document.querySelectorAll('#metrics button').length,
  months:   document.querySelectorAll('#months .month').length,
  canvas:   document.querySelectorAll('#map canvas').length,
  estate:   (document.getElementById('ctx-estate-v') || {}).textContent || '',
  role:     (document.getElementById('ctx-role-v') || {}).textContent || '',
  coverage: (document.getElementById('coverage-live') || {}).textContent || '',
  want:     (document.getElementById('coverage-want') || {}).textContent || '',
  metric:   (document.getElementById('metric-lbl') || {}).textContent || '',
  prov:     (document.getElementById('provenance-text') || {}).textContent || '',
  ready:    (document.getElementById('readiness-counts') || {}).textContent || '',
}));
note('  shell:', JSON.stringify(shell));
for (const [k, min] of [['domains', 6], ['metrics', 1], ['months', 1], ['canvas', 1]]) {
  if (!(shell[k] >= min)) errors.push(`${k}: expected >= ${min}, got ${shell[k]}`);
}
for (const k of ['estate', 'role', 'coverage', 'metric', 'ready']) {
  if (!shell[k].trim() || shell[k] === '—') errors.push(`${k} never resolved: "${shell[k]}"`);
}
if (/^\s*$|Loading/.test(shell.prov)) errors.push(`provenance never resolved: "${shell.prov}"`);

// ── the data request, which had no surface before this work ──────────────
await page.click('#coverage-btn');
await page.waitForFunction(
  () => ((document.getElementById('sheet-body') || {}).textContent || '').trim().length > 120,
  null, { timeout: 12000 }).catch(() => errors.push('data request never rendered'));
await page.keyboard.press('Escape');
await page.waitForTimeout(200);

// ── walk every domain, and every panel inside it ─────────────────────────
const domains = await page.$$eval('.dom-ico', els => els.map(e => e.dataset.domain));
let walked = 0, pointing = 0, sections = 0;
const slow = [];

// The manifest's word on each panel. A panel it calls live must render
// something other than the "not yet built" note: five live features fell
// through to it for a week because nothing checked.
const status = await page.evaluate(async () => {
  const d = await (await fetch('/gis/features')).json();
  const m = {};
  for (const dom of d.domains) for (const f of dom.features) m[f.panel] = f.status;
  return m;
});
// Features that open a drawer the header already owns, not a dock tab.
const DRAWERS = { readiness: '#sheet-bg', ask: '#ask' };

for (const dom of domains) {
  await page.evaluate(d => document.querySelector(`.dom-ico[data-domain="${d}"]`).click(), dom);
  await page.waitForTimeout(350);
  const panels = await page.$$eval('#panels [data-panel]', els => els.map(e => e.dataset.panel));
  if (!panels.length) { errors.push(`domain ${dom}: flyout listed no panels`); continue; }

  for (const key of panels) {
    const before = errors.length;
    const t0 = Date.now();
    await page.evaluate(k => document.querySelector(`#panels [data-panel="${k}"]`).click(), key);
    if (DRAWERS[key]) {
      const opened = await page.waitForFunction(sel => {
        const el = document.querySelector(sel);
        return el && !el.hidden;
      }, DRAWERS[key], { timeout: 8000 }).then(() => true, () => false);
      walked++;
      if (!opened) errors.push(`panel ${key}: its drawer never opened`);
      // Put the surface away so the next panel is clicked on a clean page.
      await page.evaluate(() => {
        const x = document.getElementById('ask-x');
        if (x && !document.getElementById('ask').hidden) x.click();
        const s = document.getElementById('sheet-bg');
        if (s && !s.hidden) s.hidden = true;
      });
      await page.waitForTimeout(150);
      if (errors.length > before) note(`    x ${dom}/${key}`);
      continue;
    }
    // Features people work in open a window rather than the dock. The window
    // is shown synchronously on click, so this check cannot race the load.
    const isWindow = await page.evaluate(() => {
      const bg = document.getElementById('fp-bg');
      return !!bg && !bg.hidden;
    });
    if (isWindow) {
      const ok = await page.waitForFunction(
        () => document.querySelectorAll('#fp-nav [data-sec]').length > 1
          && ((document.getElementById('fp-content') || {}).textContent || '').trim().length > 40,
        null, { timeout: 30000 }).then(() => true, () => false);
      const ms = Date.now() - t0;
      walked++;
      if (!ok) { errors.push(`window ${key}: rendered empty after ${ms}ms`); continue; }
      if (ms > 3000) slow.push(`${key} ${ms}ms`);
      // Every section in the menu, not only the first: a section that throws
      // is invisible until someone clicks it in front of a client.
      const secs = await page.$$eval('#fp-nav [data-sec]', els => els.map(e => e.dataset.sec));
      let mapped = 0;
      for (const sec of secs) {
        await page.evaluate(id => document.querySelector(`#fp-nav [data-sec="${id}"]`).click(), sec);
        const done = await page.waitForFunction(id => {
          const on = document.querySelector('#fp-nav .fp-nav-i.on');
          const body = ((document.getElementById('fp-content') || {}).textContent || '').trim();
          return on && on.dataset.sec === id && body.length > 20 && !/^Loading/.test(body)
            && !/failed to render/.test(body);
        }, sec, { timeout: 15000 }).then(() => true, () => false);
        if (!done) errors.push(`window ${key}: section ${sec} did not render`);
        mapped += await page.evaluate(() => document.querySelectorAll('#fp-content [data-map]').length);
      }
      sections += secs.length;
      if (mapped) pointing++;
      await page.click('#fp-x');
      await page.waitForTimeout(150);
      if (errors.length > before) note(`    x ${dom}/${key}`);
      continue;
    }
    // Poll rather than sleep: the fitted-model panels take seconds, everything
    // deterministic lands in well under one.
    const ok = await page.waitForFunction(
      () => ((document.getElementById('wb-body') || {}).textContent || '').trim().length > 40,
      null, { timeout: 15000 }).then(() => true, () => false);
    const ms = Date.now() - t0;
    walked++;
    const refs = await page.evaluate(() =>
      document.querySelectorAll('#wb-body [data-block]').length);
    if (refs) pointing++;
    if (!ok) errors.push(`panel ${key}: rendered empty after ${ms}ms`);
    else if (ms > 2000) slow.push(`${key} ${ms}ms`);
    const text = await page.evaluate(() =>
      ((document.getElementById('wb-body') || {}).textContent || ''));
    if (/specified and not yet built/.test(text) && status[key] !== 'planned') {
      errors.push(`panel ${key}: the manifest says ${status[key]} but the dock says not yet built`);
    }
    if (/failed to render/.test(text)) errors.push(`panel ${key}: renderer threw`);
    if (errors.length > before) note(`    x ${dom}/${key}`);
  }
}
note(`  ${sections} window sections rendered`);
note(`  walked ${walked} panels across ${domains.length} domains`);
note(`  ${pointing} of ${walked} panels point at the map`);
// Eleven is what Phase 2 reached; a drop means a payload or a table changed
// shape and a panel quietly stopped driving the map.
if (pointing < 11) errors.push(`only ${pointing} panels point at the map, expected 11+`);
if (slow.length) note('  slow: ' + slow.join(', '));

// ── the metric menu, now the only home of the 31-metric catalogue ────────
await page.evaluate(() => document.querySelector('.dom-ico.on')?.click());  // shut the flyout
await page.waitForTimeout(300);
await page.click('#metric-btn');
await page.waitForTimeout(300);
const menuOpen = await page.evaluate(() => !document.getElementById('metric-menu').hidden);
if (!menuOpen) errors.push('metric menu did not open');
await page.keyboard.press('Escape');
await page.waitForTimeout(200);

// ── the hover readout, which the choropleth never had ────────────────────
{
  const r = await page.evaluate(() => {
    const b = document.querySelector('#map canvas').getBoundingClientRect();
    return { x: b.x, y: b.y, w: b.width, h: b.height };
  });
  // MapLibre needs real movement, not a single jump, to fire a layer mousemove.
  await page.mouse.move(r.x + r.w * 0.30, r.y + r.h * 0.30);
  let chip = null;
  for (let i = 0; i <= 10 && !chip; i++) {
    await page.mouse.move(r.x + r.w * (0.30 + i * 0.02), r.y + r.h * (0.30 + i * 0.01));
    await page.waitForTimeout(110);
    chip = await page.evaluate(() => {
      const el = document.querySelector('.hover-chip');
      return el && !el.hidden ? el.textContent.replace(/\s+/g, ' ').trim() : null;
    });
  }
  if (!chip) errors.push('hovering a block showed no readout');
  else note('  hover readout: ' + chip);
}

// ── a block selection, the surface the whole map exists for ──────────────
// Shut the dock first: with it open the camera pads the estate to the left,
// so the canvas centre is off the blocks and the click would prove nothing.
await page.evaluate(() => document.getElementById('wb-x')?.click());
await page.waitForTimeout(900);

const c = await page.evaluate(() => {
  const r = document.querySelector('#map canvas').getBoundingClientRect();
  return { x: r.x, y: r.y, w: r.width, h: r.height };
});
// A few points around the middle, since the estate is not a rectangle and the
// exact centre can fall in the river that runs through it.
const tries = [[0, 0], [-0.10, -0.10], [0.10, 0.10], [-0.14, 0.08], [0.12, -0.12]];
let drawerOpen = false;
for (const [dx, dy] of tries) {
  await page.mouse.click(c.x + c.w * (0.5 + dx), c.y + c.h * (0.5 + dy));
  drawerOpen = await page.waitForFunction(
    () => document.getElementById('drawer')?.classList.contains('open'),
    null, { timeout: 2500 }).then(() => true, () => false);
  if (drawerOpen) break;
}
if (!drawerOpen) errors.push('clicking a block opened no detail panel');
else {
  const txt = await page.evaluate(() => document.getElementById('drawer').textContent || '');
  if (txt.trim().length < 80) errors.push('block detail rendered near-empty');
  note('  block detail: ' + txt.replace(/\s+/g, ' ').trim().slice(0, 70));
}

await browser.close();

note('');
if (errors.length) {
  note(`FAIL  ${errors.length} problem(s):`);
  [...new Set(errors)].forEach(e => note('  ' + e));
  process.exit(1);
}
note(`OK    ${walked} panels, ${domains.length} domains, no console errors`);
