/* How many blocks does each panel point at? Phase 2's whole claim is that
 * panels drive the map, so this counts the rows that actually resolved. */
import { chromium } from 'playwright';
const b = await chromium.launch({ channel: 'chrome' });
const p = await b.newPage({ viewport: { width: 1600, height: 950 } });
p.on('pageerror', e => console.log('  pageerror:', e.message));
await p.goto('http://127.0.0.1:8001/command', { waitUntil: 'domcontentloaded' });
await p.waitForTimeout(9000);
const domains = await p.$$eval('.dom-ico', els => els.map(e => e.dataset.domain));
let pointing = 0, total = 0;
for (const d of domains) {
  await p.evaluate(k => document.querySelector(`.dom-ico[data-domain="${k}"]`).click(), d);
  await p.waitForTimeout(300);
  const panels = await p.$$eval('#panels [data-panel]', els => els.map(e => e.dataset.panel));
  for (const key of panels) {
    await p.evaluate(k => document.querySelector(`#panels [data-panel="${k}"]`).click(), key);
    await p.waitForFunction(
      () => ((document.getElementById('wb-body')||{}).textContent||'').trim().length > 40,
      null, { timeout: 15000 }).catch(() => {});
    const n = await p.evaluate(() =>
      document.querySelectorAll('#wb-body [data-block]').length);
    const painted = await p.evaluate(() => {
      const el = document.querySelector('#wb-body .blk-hint');
      return el ? el.textContent.trim().slice(0, 28) : '';
    });
    total++;
    if (n) pointing++;
    console.log(`  ${n ? '*' : ' '} ${(d + '/' + key).padEnd(28)} ${String(n).padStart(3)} rows  ${painted}`);
  }
}
console.log(`\n  ${pointing} of ${total} panels point at the map`);
await b.close();
