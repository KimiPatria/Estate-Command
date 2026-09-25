/* The four forecasts, checked end to end.
 *
 * The smoke test proves every section renders. This proves the forecasts do
 * what buildplan_ml.md says: each is graded against the method it replaces,
 * each finds what the generated data holds and invents nothing, the answer
 * key never leaves the server, switching a forecast off puts the plan back on
 * the old method, a replayed spray day decides on the forecast rather than
 * the rain that fell, and the outlook window explains all of it in words.
 *
 *     node test/models.mjs [baseURL] [screenshotDir]
 *
 * Expects dashboard_server.py to be running. Puts the register back after.
 */
import { chromium } from 'playwright';

const BASE = process.argv[2] || 'http://127.0.0.1:8001';
const SHOT = process.argv[3] || null;
const errors = [];
const note = (...a) => console.log(...a);
const fail = msg => { errors.push(msg); console.log('FAIL', msg); };
const expect = (cond, msg) => { if (!cond) fail(msg); };

async function api(path, opts) {
  const r = await fetch(`${BASE}${path}`, opts);
  if (!r.ok) throw new Error(`${path} -> ${r.status}`);
  return r.json();
}
const setAsm = (key, value) => api('/gis/assumptions', {
  method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ key, value }),
});

// ── the API ────────────────────────────────────────────────────────────────

const outlook = await api('/gis/forecast/outlook');
expect(outlook.summary.length >= 3, 'outlook has a plain summary');
expect(outlook.trust.length === 4, 'four forecasts graded');
for (const t of outlook.trust) {
  expect(t.grade && t.grade.label && t.headline, `${t.model} carries a grade and a sentence`);
}
note('  grades:', outlook.trust.map(t => `${t.model} ${t.grade.label}`).join(' · '));
expect(/% chance/.test(outlook.summary[0]), 'the summary states rain as a chance');

const rain = await api('/gis/forecast/rain');
expect(rain.forecast.available && rain.forecast.trained_on === 'real', 'rain is trained on real data');
expect(rain.backtest.thresholds['15'].improvement_vs_month_average_pct > 0, 'rain beats the month average');
expect(/chance/.test(rain.forecast.chances.washoff.meaning), 'rain explains the chance in words');

const acc = await api('/gis/forecast/accuracy');
expect(acc.headcount.backtest.segments.all.improvement_pct > 0, 'headcount beats the trailing average');
expect(acc.headcount.recovery.all_recovered, 'headcount finds what the data holds and invents no payday effect');
expect(acc.work_done.backtest.modes.six_in_the_morning.all.order_improvement_pct > 0, 'work done beats the ledger average at six in the morning');
expect(acc.work_done.recovery.all_recovered, 'work done recovers the rain and road effects');
const speedChecks = Object.fromEntries(acc.speeds.recovery.rows.map(r => [r.effect, r.recovered]));
expect(speedChecks.prune_speed && speedChecks.weed_speed, 'learned crew speeds line up with the hidden speeds');
expect(speedChecks.harvest_block_pace, 'harvest block pace repeats from one period to the next');
note('  checks: headcount', acc.headcount.recovery.rows.filter(r => r.recovered).length, 'of', acc.headcount.recovery.rows.length,
  '· work done', acc.work_done.recovery.rows.filter(r => r.recovered !== false).length, 'of', acc.work_done.recovery.rows.length,
  '· speeds', acc.speeds.recovery.rows.filter(r => r.recovered).length, 'of', acc.speeds.recovery.rows.length);

// The answer key stays on the server.
const cap = await api('/gis/ops/capacity');
const harvest = await api('/gis/ops/harvest/plan');
expect(!JSON.stringify(cap).includes('"skill"') && !JSON.stringify(harvest).includes('"skill"'),
  'no payload carries the generator\'s skill column');

// The plan carries the forecasts, in words.
const f = harvest.forecast;
expect(f && f.work_done.in_use && /Expect about \d+% of the plan/.test(f.work_done.plain), 'the harvest plan says how much will get done');
expect(f.headcount.in_use && /Expect [\d,]+ to [\d,]+/.test(f.headcount.plain), 'the harvest plan gives a headcount range');
expect(harvest.crews.every(c => c.present_low <= c.present && c.present <= c.present_high), 'each crew\'s most likely headcount sits in its range');
expect(harvest.totals.expected_qty_low <= harvest.totals.expected_qty && harvest.totals.expected_qty <= harvest.totals.expected_qty_high,
  'expected work done sits inside its band');

// Switching a forecast off puts the plan back on the old method.
await setAsm('use_headcount_model', 0);
const off = await api('/gis/ops/harvest/plan');
expect(off.crews.every(c => c.present_low === null || c.present_low === undefined), 'headcount off: no ranges on the plan');
expect(off.forecast.inputs.find(i => i.input === 'Headcount').used !== 'forecast', 'headcount off: the plan says it used the average');
await api('/gis/assumptions?key=use_headcount_model', { method: 'DELETE' });

// A replayed spray day decides on the forecast, not on the rain that fell.
const day = '2025-04-22';
const sprayOn = await api(`/gis/ops/spray/plan?date=${day}`);
expect(sprayOn.weather.forecast && sprayOn.weather.recorded_mm > 30, 'replay carries what fell beside the forecast');
expect(/chance of wash-off/.test(sprayOn.weather.reason || (sprayOn.forecast.spray_call || {}).plain || ''),
  'with the rain forecast on, the spray call is argued from the chance of wash-off');
await setAsm('use_rain_model', 0);
const sprayOff = await api(`/gis/ops/spray/plan?date=${day}`);
expect(/mm on the day/.test(sprayOff.weather.reason || ''), 'with it off, the replay falls back to the rain recorded that day');
await api('/gis/assumptions?key=use_rain_model', { method: 'DELETE' });
note('  spray on', day + ':', (sprayOn.forecast.spray_call || { verdict: sprayOn.weather.reason }).verdict, '· off:', sprayOff.weather.reason);

// ── the windows ────────────────────────────────────────────────────────────

const browser = await chromium.launch({ channel: 'chrome' });
const page = await browser.newPage({ viewport: { width: 1600, height: 950 } });
page.on('pageerror', e => fail(`pageerror: ${e.message}`));
page.on('console', m => {
  if (m.type() === 'error' && !/Failed to load resource/.test(m.text())) fail(`console: ${m.text()}`);
});
const text = sel => page.evaluate(s => ((document.querySelector(s) || {}).textContent || '').replace(/\s+/g, ' ').trim(), sel);
const shot = async name => { if (SHOT) await page.screenshot({ path: `${SHOT}/${name}.png` }); };

async function openWindow(domain, key) {
  await page.evaluate(d => {
    const b = document.querySelector(`.dom-ico[data-domain="${d}"]`);
    if (!b.classList.contains('on')) b.click();
  }, domain);
  await page.waitForTimeout(300);
  await page.evaluate(k => document.querySelector(`#panels [data-panel="${k}"]`).click(), key);
  await page.waitForFunction(() => document.querySelectorAll('#fp-nav [data-sec]').length > 1
    && !/^(Loading|Checking)/.test((document.getElementById('fp-content') || {}).textContent.trim()), null, { timeout: 90000 });
  await page.waitForTimeout(250);
}
async function section(id) {
  // Sections here fetch their own data, so the menu marks the new item before
  // the content arrives: wait for the heading to change, not just the menu.
  const before = await text('#fp-content h3');
  await page.click(`#fp-nav [data-sec="${id}"]`);
  await page.waitForFunction(([s, prev]) => {
    const on = document.querySelector('#fp-nav .fp-nav-i.on');
    const h = document.querySelector('#fp-content h3');
    return on && on.dataset.sec === s && h && h.textContent.replace(/\s+/g, ' ').trim() !== prev
      && !/^Loading/.test(document.getElementById('fp-content').textContent.trim());
  }, [id, before], { timeout: 60000 });
  await page.waitForTimeout(200);
}
async function settle() {
  await page.waitForFunction(() => !/^Loading/.test(document.getElementById('fp-content').textContent.trim()), null, { timeout: 60000 });
  await page.waitForTimeout(200);
}

await page.goto(`${BASE}/command`, { waitUntil: 'domcontentloaded' });
await page.waitForFunction(() => {
  const el = document.getElementById('intro');
  return !el || el.hidden || getComputedStyle(el).opacity === '0' || getComputedStyle(el).display === 'none';
}, null, { timeout: 30000 });
await page.waitForTimeout(2000);

await openWindow('governance', 'forecasts');
const groups = await page.$$eval('#fp-nav .fp-nav-l', ns => ns.map(n => n.textContent.trim()));
expect(groups.join('|').startsWith('Tomorrow|Trust'), `outlook menu groups: ${groups.join('|')}`);
expect(/In short/.test(await text('#fp-content')) && (await page.$$('.fc-card')).length === 4, 'at a glance: a summary and four cards');
expect((await page.$$('.fc-trust')).length >= 4, 'at a glance: every card says how far to trust it');
await shot('outlook-glance');

await section('rain');
expect(/washes the herbicide off|wash off/.test(await text('#fp-content')) && (await page.$$('.fc-chance')).length === 3,
  'rain: three chances, each explained');
expect(/How accurate is it/.test(await text('#fp-content')), 'rain: says how accurate it is');
await shot('outlook-rain');

await section('headcount');
const hcRows = (await page.$$('#fp-content .fc-range')).length;
expect(hcRows > 20, `headcount: a range bar per crew (${hcRows})`);
await page.click('[data-crewtype="harvest"]');
await settle();
expect(/Harvest gangs/.test(await text('.fc-chips .on')), 'headcount: filters to harvest gangs');
await shot('outlook-headcount');

await section('work');
expect(/expected to get done/.test(await text('#fp-content')), 'work done: states the share expected');
const riskBtn = await page.$('#fp-content .fp-map-btn');
if (riskBtn) {
  await riskBtn.click();
  await page.waitForFunction(() => !document.getElementById('fp-bar').hidden, null, { timeout: 5000 });
  expect(/outlined/.test(await text('#fp-bar-cap')), 'work done: a block at risk goes to the map');
  await page.click('#fp-restore');
  await page.waitForTimeout(300);
}
await page.click('[data-op="spray"]');
await settle();
expect(/Spray|Hold spraying/.test(await text('#fp-content')), 'work done: spraying shows the spray call');
await shot('outlook-spray');

await section('speeds');
expect((await page.$$('#fp-content .fc-speed')).length > 5, 'speeds: a bar per crew');
expect(/faster than the average crew|slower than the average crew/.test(await text('#fp-content')), 'speeds: says it in words');
await shot('outlook-speeds');

await section('accuracy');
expect((await page.$$('#fp-content .fc-trustcard')).length >= 4, 'accuracy: a grade for every forecast');
await shot('outlook-accuracy');
// The explanations live on this one page, not repeated under every section.
await section('how');
expect((await page.$$('.fc-howblock')).length === 4, 'how: four plain explanations');
expect(/How it works/.test(await text('#fp-content')), 'how: explains how each forecast works');

// A replay day from the header.
await page.fill('[data-fc-date]', '2025-04-22');
await page.dispatchEvent('[data-fc-date]', 'change');
await page.waitForFunction(() => /replay/.test((document.getElementById('fp-extra') || {}).textContent || ''), null, { timeout: 60000 });
await settle();
await section('rain');
{ const t = await text('#fp-content'); expect(/actually fell/.test(t), `replay: rain shows what actually fell beside the chance (${t.slice(0, 160)})`); }
await page.keyboard.press('Escape');
await page.waitForTimeout(300);

// The plan window carries the strip and the Forecast section.
await openWindow('harvesting', 'ops_harvest');
expect(await page.$('.fc-strip') !== null, 'harvest plan: a forecast strip under the figures');
expect(/likely \d+ to \d+/.test(await text('#fp-content .ops-plan')), 'harvest plan: each gang shows its likely headcount');
await shot('plan-strip');
await section('forecast');
expect(/What the forecasts say/.test(await text('#fp-content h3')), 'harvest window: a Forecast section');
expect(/What this plan is built on/.test(await text('#fp-content')), 'forecast section: lists what each input was taken from');
await shot('plan-forecast');
await page.keyboard.press('Escape');

await browser.close();

// Put the shared demo state back.
await api('/gis/assumptions', { method: 'DELETE' });

if (errors.length) {
  console.log(`\nFAILED ${errors.length}`);
  errors.forEach(e => console.log('  ', e));
  process.exit(1);
}
console.log('\nOK forecasts: graded, checked against planted truth, answer key withheld, switches honoured, replay decides on the forecast, windows explain it');
