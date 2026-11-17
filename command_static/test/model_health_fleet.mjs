// Model health overview (/model-health): every forecast at a glance, the health
// matrix, the accuracy bars and the data-quality watchdog, and the drill-down into
// one forecast's detail.
//   node test/model_health_fleet.mjs [base] [shotDir]
// Needs the dashboard server up (the overview warms ~30 s after a start).
import { chromium } from 'playwright';

const base = process.argv[2] || 'http://127.0.0.1:8001';
const shots = process.argv[3] || null;
const fails = [];
const check = (ok, msg) => { console.log(`${ok ? 'ok  ' : 'FAIL'} ${msg}`); if (!ok) fails.push(msg); };

const browser = await chromium.launch({ channel: 'chrome' });
const context = await browser.newContext({ viewport: { width: 1600, height: 1100 }, colorScheme: 'light' });
const page = await context.newPage();
const errors = [];
page.on('pageerror', e => errors.push(e.message));
page.on('console', m => { if (m.type() === 'error' && !m.text().startsWith('Failed to load resource')) errors.push(m.text()); });
page.on('response', r => { if (r.status() >= 400 && !r.url().endsWith('/favicon.ico')) errors.push(`${r.status()} ${r.url()}`); });
const shot = async (name, full = true) => {
  if (!shots) return;
  await page.waitForTimeout(600);
  await page.screenshot({ path: `${shots}/mh_${name}.png`, fullPage: full });
};

// ── the API ─────────────────────────────────────────────────────────────────
const fleet = await page.request.get(`${base}/forecast/fleet`, { timeout: 300000 }).then(r => r.json());
const expected = fleet.cards.length;
check(expected >= 7, `fleet lists every forecast (${expected})`);
check(fleet.cards.every(c => ['action', 'watch', 'healthy', 'not_forecast'].includes(c.status)), 'every card has a status');
check(fleet.cards.every(c => fleet.checks.every(k => c.cells[k.id] && c.cells[k.id].level)), 'every card has every check');
check(fleet.cards.some(c => c.group && c.source === 'staging'), 'the staging group has its own card');
const wd = await page.request.get(`${base}/forecast/watchdog?source=live&estate=ec`).then(r => r.json());
check(wd.findings.length > 0 && wd.findings.every(f => f.source === 'live' && f.estate === 'ec'), 'watchdog filters by source and estate');
check(wd.findings.some(f => f.check === 'cross_source'), 'watchdog compares EC between the two databases');
const run = await page.request.post(`${base}/forecast/watchdog/run`).then(r => r.json());
check(run.summary && run.summary.totals, 'watchdog can be re-run on demand');

// ── the overview ────────────────────────────────────────────────────────────
await page.goto(`${base}/model-health`, { waitUntil: 'networkidle' });
await page.evaluate(() => { try { localStorage.removeItem('mh-fleet-filter'); } catch (e) {} });
await page.goto(`${base}/model-health`, { waitUntil: 'networkidle' });
await page.waitForSelector('#state-fleet:not(.hidden)', { timeout: 300000 });
check(await page.locator('.fc-card').count() === expected, `one card per forecast (${expected})`);
check(await page.locator('#fleet-kpis .kpi-card').count() === 5, 'five summary tiles');
check(await page.locator('#db-chip').isHidden(), 'no Database chip on the overview (it shows both)');
check(await page.locator('.fc-card .pill').count() === expected, 'every card shows its status with a word');
check(await page.locator('.fc-card svg.spark').count() >= expected - 2, 'served forecasts draw a sparkline');
check(await page.locator('#matrix-wrap tr.mrow').count() === expected, 'the matrix has a row per forecast');
check(await page.locator('#matrix-wrap tr.mrow').first().locator('.mcell').count() === fleet.checks.length, 'the matrix has a cell per check');
check(await page.locator('#bars-wrap svg.bars g[data-href]').count() >= 5, 'accuracy bars for the scored forecasts');
const wdRows = await page.locator('#wd-tbody tr').count();
check(wdRows > 0, `watchdog table lists findings (${wdRows})`);
// the Forecast column of each live-vs-staging row (hasText is case-insensitive: "EC" is in "forecasts")
const xsrc = await page.locator('#wd-tbody tr', { hasText: 'Live vs staging' }).locator('td:nth-child(2) b').allInnerTexts();
check(xsrc.length > 0 && new Set(xsrc).size === xsrc.length,
      `a live-vs-staging finding is listed once, not under each source (${xsrc.join(', ')})`);
// overflow: nothing wider than the window
const overflow = await page.evaluate(() => document.querySelector('.fc-content').scrollWidth - document.querySelector('.fc-content').clientWidth);
check(overflow <= 1, `no horizontal page scroll (${overflow}px)`);
await shot('fleet_light');

// hover gives the reading behind a cell
// (hover() scrolls the matrix into view, and a scroll hides the tip: move once more, as a mouse would)
const cell0 = page.locator('#matrix-wrap tr.mrow').first().locator('.mcell').first();
await cell0.hover();
const cb = await cell0.boundingBox();
await page.mouse.move(cb.x + cb.width / 2 + 1, cb.y + cb.height / 2);
check(await page.locator('#tip').isVisible() && (await page.locator('#tip').innerText()).includes('Accuracy'),
      'hovering a matrix cell shows the reading behind it');

// filters
await page.locator('#f-source button[data-v="staging"]').click();
const nStg = fleet.cards.filter(c => c.source === 'staging').length;
check(await page.locator('.fc-card').count() === nStg, `source filter keeps the staging cards (${nStg})`);
const wdSrc = await page.locator('#wd-tbody tr .src-tag').allInnerTexts();
check(wdSrc.length > 0 && wdSrc.every(t => t.toLowerCase() === 'staging'), 'watchdog follows the source filter');
await page.locator('#f-source button[data-v="all"]').click();
await page.locator('#f-status button[data-v="action"]').click();
const nAct = fleet.cards.filter(c => c.status === 'action').length;
check(await page.locator('.fc-card').count() === nAct, `status filter keeps the cards needing action (${nAct})`);
await page.locator('#f-status button[data-v="all"]').click();

// dark theme
await page.locator('#theme-btn').click();
await shot('fleet_dark', false);
await page.locator('#theme-btn').click();

// ── drill down ──────────────────────────────────────────────────────────────
const target = fleet.cards.find(c => c.source === 'staging' && !c.group && c.status !== 'not_forecast');
await page.locator(`.fc-card[data-href="${target.href}"]`).click();
await page.waitForSelector('#state-result:not(.hidden)', { timeout: 120000 });
check(new URL(page.url()).searchParams.get('estate') === target.estate, `a card opens that forecast's detail (${target.label})`);
check(await page.locator('#kpi-row .kpi-card').count() === 4, 'detail renders its KPI row');
check((await page.locator('#title-main').innerText()) === target.label, 'detail is titled with the estate');
check(await page.locator('#dq-findings tr').count() > 0, 'detail lists the watchdog findings for the estate');
check(await page.locator('#mon-tbody tr').count() > 0, 'detail lists the monitoring rows');
await page.waitForSelector('#switcher:not(.hidden)', { timeout: 60000 });
check(await page.locator('#switcher .sw-chip.on').count() === 1, 'the switcher marks the forecast in view');
await shot('detail_staging');
await page.locator('#crumb').click();
await page.waitForSelector('#state-fleet:not(.hidden)', { timeout: 120000 });
check(true, '"All forecasts" returns to the overview');

// a forecast that is not served still opens, with its data checks
const nf = fleet.cards.find(c => c.status === 'not_forecast');
if (nf) {
  check(await page.locator(`.fc-card.not_forecast[data-href="${nf.href}"] .fcc-reason`).count() === 1, `${nf.label} ${nf.source}: the card says why it is not forecast`);
}

// phone width: one column, no sideways scroll
await page.setViewportSize({ width: 390, height: 900 });
await page.waitForTimeout(300);
const ow = await page.evaluate(() => document.querySelector('.fc-content').scrollWidth - document.querySelector('.fc-content').clientWidth);
check(ow <= 1, `phone width: no horizontal page scroll (${ow}px)`);
await shot('fleet_phone', false);

check(errors.length === 0, `no console or network errors${errors.length ? ': ' + errors.slice(0, 3).join(' | ') : ''}`);
await browser.close();
console.log(fails.length ? `\n${fails.length} failed` : '\nall passed');
process.exit(fails.length ? 1 : 0);
