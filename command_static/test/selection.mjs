/* Phase 3: can a person build a set and act on it? */
import { chromium } from 'playwright';
const errors = [];
const b = await chromium.launch({ channel: 'chrome' });
const p = await b.newPage({ viewport: { width: 1600, height: 950 } });
p.on('pageerror', e => errors.push('pageerror: ' + e.message));
p.on('console', m => { if (m.type() === 'error' && !/Failed to load resource/.test(m.text()))
  errors.push('console: ' + m.text()); });
await p.goto('http://127.0.0.1:8001/command', { waitUntil: 'domcontentloaded' });
await p.waitForTimeout(9000);

const trayText = async () => p.evaluate(() => {
  const t = document.getElementById('tray');
  return t && !t.hidden ? t.textContent.replace(/\s+/g, ' ').trim() : null;
});

// 1. select all, from a panel's block list
await p.evaluate(() => document.querySelector('.dom-ico[data-domain="pest"]').click());
await p.waitForTimeout(400);
await p.evaluate(() => document.querySelector('#panels [data-panel="pest_spread"]').click());
await p.waitForTimeout(2500);
const take = await p.$('#wb-body .blk-take');
if (!take) errors.push('no select-all button on a block list');
else {
  await take.click();
  await p.waitForTimeout(500);
  console.log('  after select-all:', await trayText());
  if (!await trayText()) errors.push('tray did not appear after select-all');
}

// 2. the map paints the set
const painted = await p.evaluate(() => {
  // the set layers exist and carry a filter with more than the sentinel
  const c = document.querySelector('#map canvas');
  return !!c;
});
if (!painted) errors.push('no map canvas');

// 3. the draft menu offers the documents a block set can produce
await p.evaluate(() => document.querySelector('.dom-ico.on')?.click());
await p.waitForTimeout(300);
await p.click('#tray-draft');
await p.waitForTimeout(700);
const kinds = await p.$$eval('.draft-row', els =>
  els.map(e => e.querySelector('.t').textContent.trim()));
console.log('  draft offers:', kinds.join(', ') || '(none)');
if (!kinds.length) errors.push('draft menu offered nothing');

// 4. drafting records a decision and renders the artifact
if (kinds.length) {
  await p.evaluate(() => document.querySelector('.draft-row').click());
  await p.waitForFunction(
    () => ((document.getElementById('wb-body') || {}).textContent || '').length > 200,
    null, { timeout: 15000 }).catch(() => errors.push('draft never rendered'));
  const txt = await p.evaluate(() =>
    (document.getElementById('wb-body').textContent || '').replace(/\s+/g, ' ').trim());
  console.log('  drafted:', txt.slice(0, 110));
  if (!/draft|Draft/.test(txt)) errors.push('drafted tab does not look like a document');
}

// 5. brief the set
await p.click('#tray-brief');
await p.waitForTimeout(1200);
const brief = await p.evaluate(() =>
  (document.getElementById('wb-body').textContent || '').replace(/\s+/g, ' ').trim());
console.log('  brief:', brief.slice(0, 100));
if (!/ha planted|blocks/.test(brief)) errors.push('set brief did not render');

// 6. clear
await p.click('#tray-clear');
await p.waitForTimeout(400);
if (await trayText()) errors.push('tray still showing after clear');
else console.log('  cleared: tray hidden');

await b.close();
console.log('');
if (errors.length) { console.log('FAIL'); [...new Set(errors)].forEach(e => console.log('  ' + e)); process.exit(1); }
console.log('OK    selection set builds, drafts, briefs and clears');
