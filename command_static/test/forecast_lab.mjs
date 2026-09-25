// Forecast Lab (/experiment/forecast) end-to-end check.
//   node test/forecast_lab.mjs [base] [shotDir]
// Needs the dashboard server up and the lab artifacts built
// (forecast/lab/build_artifacts.py). Exits non-zero on the first failed check.
import { chromium } from 'playwright';

const base = process.argv[2] || 'http://127.0.0.1:8001';
const shots = process.argv[3] || null;
const fails = [];
const check = (ok, msg) => { console.log(`${ok ? 'ok  ' : 'FAIL'} ${msg}`); if (!ok) fails.push(msg); };

const browser = await chromium.launch({ channel: 'chrome' });
const page = await browser.newPage({ viewport: { width: 1360, height: 1000 } });
const errors = [];
page.on('pageerror', e => errors.push(e.message));
page.on('console', m => {
  // Resource failures are recorded with their URL by the response hook below.
  if (m.type() === 'error' && !m.text().startsWith('Failed to load resource')) errors.push(m.text());
});
page.on('response', r => {
  // The app serves no favicon on any page; that 404 is not this page's fault.
  if (r.status() >= 400 && !r.url().endsWith('/favicon.ico')) errors.push(`${r.status()} ${r.url()}`);
});
await page.addInitScript(() => { try { localStorage.clear(); } catch (e) {} });

const shot = async (name) => {
  if (!shots) return;
  await page.evaluate(() => { document.querySelector('.fc-content').scrollTop = 0; });
  await page.waitForTimeout(700);   // let Chart.js finish animating the lines in
  await page.screenshot({ path: `${shots}/lab_${name}.png`, fullPage: true });
};
const lineCount = () => page.evaluate(() =>
  Chart.getChart('fc-chart').data.datasets.filter(d => d._model).length);
const futurePoints = (id) => page.evaluate((id) => {
  const ds = Chart.getChart('fc-chart').data.datasets.find(d => d._model && d._model.id === id);
  return ds ? ds.data.filter(v => v != null).length - 1 : -1;   // minus the anchor
}, id);

await page.goto(`${base}/experiment/forecast`, { waitUntil: 'networkidle' });
await page.waitForSelector('#state-result:not(.hidden)', { timeout: 60000 });

// Defaults: k3, months, 3-month horizon, served ensemble drawn.
check(await page.locator('#estate-chips .scope-chip.active').innerText().then(t => t.startsWith('K3')),
      'k3 selected by default');
check(await futurePoints('served_ensemble') === 3, 'served ensemble drawn for 3 months');
check(await lineCount() >= 2, 'several default models drawn');
const firstServed = await page.locator('#cmp-body tr').first().locator('td').nth(2).innerText();
check(firstServed.startsWith('114,185'), `served h1 matches /forecast (got ${firstServed})`);
await shot('k3_month_3');

// Horizon slider to 12 months: lines and table follow, no refetch needed.
await page.locator('#hz-range').fill('12');
check(await futurePoints('served_ensemble') === 12, 'slider to 12 extends the lines');
check(await page.locator('#cmp-body tr').count() === 12, 'table has 12 rows');
check((await page.locator('#hz-val').innerText()) === '12 months', 'horizon label reads 12 months');

// Toggle a model off and back on.
const chip = page.locator('#model-chips .model-chip', { hasText: 'monthly + weather' });
const before = await lineCount();
await chip.click();
check(await lineCount() === before - 1, 'deselecting a model removes its line');
await chip.click();
check(await lineCount() === before, 're-selecting brings it back');
await shot('k3_month_12');

// Weeks: monthly-only models disable, weekly ones draw, slider caps at 12.
await page.locator('#grain-toggle .hz-btn[data-grain=week]').click();
await page.waitForFunction(() => document.querySelector('#chart-title').textContent.startsWith('Weekly'));
check(await page.locator('#model-chips .model-chip:disabled', { hasText: 'Ensemble' }).count() === 1,
      'served ensemble disabled at weekly grain');
check(await futurePoints('chronos2_blocks') === 12, 'block sum drawn for 12 weeks');
check((await page.locator('#hz-range').getAttribute('max')) === '12', 'weekly slider caps at 12');
await page.locator('#hz-range').fill('5');
check(await futurePoints('chronos2_blocks') === 5, 'weekly slider to 5 shortens the line');
await shot('k3_week_5');

// Estate switch: EC weekly, then EA (weekly only), then back to months.
await page.locator('#estate-chips .scope-chip', { hasText: 'EC' }).click();
await page.waitForFunction(() => document.querySelector('#chart-title').textContent.includes('EC'));
check(await futurePoints('chronos2_blocks') === 5, 'EC weekly keeps the 5-week horizon');
await page.locator('#estate-chips .scope-chip', { hasText: 'EA' }).click();
await page.waitForFunction(() => document.querySelector('#chart-title').textContent.includes('EA'));
check(await lineCount() >= 1, 'EA weekly draws');
await page.locator('#grain-toggle .hz-btn[data-grain=month]').click();
await page.waitForFunction(() => document.querySelector('#chart-title').textContent.startsWith('Monthly'));
check(!(await page.locator('#chart-title').innerText()).includes('EA'),
      'months on EA (1 complete month) moves to another estate');
check(await page.locator('#estate-chips .scope-chip:disabled', { hasText: 'EA' }).count() === 1,
      'EA chip disabled at monthly grain');

// EC monthly: the served model is flagged stale.
await page.locator('#estate-chips .scope-chip', { hasText: 'EC' }).click();
await page.waitForFunction(() => document.querySelector('#chart-title').textContent.includes('EC'));
check(await page.locator('#model-chips .model-chip', { hasText: 'Ensemble' }).locator('.badge').count() === 1,
      'EC served ensemble flagged stale');
await shot('ec_month_12');

// Band picker switches the shaded model.
await page.locator('#band-chips .scope-chip', { hasText: 'None' }).click();
check(await page.evaluate(() => !Chart.getChart('fc-chart').data.datasets.some(d => d.label === '_upper')),
      'band None removes the band');

// ── Backtest view ──────────────────────────────────────────────────────────
await page.locator('#estate-chips .scope-chip', { hasText: 'K3' }).click();
await page.waitForFunction(() => document.querySelector('#chart-title').textContent.includes('K3'));
await page.locator('#hz-range').fill('3');
await page.locator('#view-toggle .hz-btn[data-view=backtest]').click();
await page.waitForFunction(() => document.querySelectorAll('#gate-a .chk').length === 3, null, { timeout: 60000 });
check(await page.locator('#chart-title').isHidden(), 'forecast chart hidden in backtest view');
check((await page.locator('#promo-verdict').innerText()).includes('HOLD'), 'gate verdict HOLD (no shadow months yet)');
check(await page.locator('#gate-a .chk.pass').count() === 3, 'gate A: all three backtest checks pass');
check(await page.locator('#gate-b .chk.fail').count() === 3, 'gate B: shadow checks not yet met');

const lbRows = await page.locator('#lb-body tr').count();
const errLines = await page.evaluate(() => Chart.getChart('err-chart').data.datasets.length);
check(lbRows >= 2 && errLines === lbRows, `leaderboard and error chart list the same models (${lbRows}/${errLines})`);
check(await page.locator('#lb-body td.best').count() >= 1, 'leaderboard marks a best model per column');
const covH1 = await page.locator('#lb-body tr', { hasText: 'monthly + weather' }).locator('td').nth(1).innerText();
check(covH1.startsWith('10,329'), `challenger h1 MAE on the common set matches the gate (got ${covH1})`);

// Replay: moving the origin changes what is drawn.
const t1 = await page.locator('#rp-title').innerText();
await page.locator('#origin-range').fill('0');
const t2 = await page.locator('#rp-title').innerText();
check(t1 !== t2, 'origin slider replays another origin');
check(await page.evaluate(() => Chart.getChart('rp-chart').data.datasets.filter(d => d._model).length) === lbRows,
      'replay draws every selected model');
await page.locator('#metric-chips .scope-chip[data-metric=smape]').click();
check((await page.locator('#lb-body td').nth(1).innerText()).endsWith('%'), 'sMAPE switch reformats the leaderboard');
await page.locator('#metric-chips .scope-chip[data-metric=mae]').click();
await shot('k3_backtest');

// Weekly backtest on EC.
await page.locator('#grain-toggle .hz-btn[data-grain=week]').click();
await page.locator('#estate-chips .scope-chip', { hasText: 'EC' }).click();
await page.waitForFunction(() => document.querySelector('#err-title').textContent.includes('EC · weekly'));
check(await page.locator('#model-chips .model-chip:disabled', { hasText: 'Ensemble' }).count() === 1,
      'weekly backtest: served ensemble unavailable');
check(await page.locator('#lb-body tr').count() >= 1, 'EC weekly leaderboard has rows');
await shot('ec_week_backtest');

// Back to the forecast view: the forward chart comes back.
await page.locator('#view-toggle .hz-btn[data-view=forecast]').click();
await page.waitForFunction(() => document.querySelector('#chart-title').offsetParent !== null);
check(await lineCount() >= 1, 'forecast view renders again after the backtest view');

check(errors.length === 0, `no page errors (${errors.join(' | ')})`);
await browser.close();
console.log(fails.length ? `\n${fails.length} failed` : '\nall passed');
process.exit(fails.length ? 1 : 0);
