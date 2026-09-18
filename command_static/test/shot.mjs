/* Screenshot the command page.
 *   node test/shot.mjs <out.png> [domain] [panel] [--hover-row]
 */
import { chromium } from 'playwright';
const args = process.argv.slice(2);
const hoverRow = args.includes('--hover-row');
const [out = 'shot.png', domain, panel] = args.filter(a => !a.startsWith('--'));
const b = await chromium.launch({ channel: 'chrome' });
const p = await b.newPage({ viewport: { width: 1600, height: 950 } });
p.on('pageerror', e => console.log('pageerror:', e.message));
await p.goto('http://127.0.0.1:8001/command', { waitUntil: 'domcontentloaded' });
await p.waitForTimeout(9000);
if (domain) {
  await p.evaluate(d => document.querySelector(`.dom-ico[data-domain="${d}"]`)?.click(), domain);
  await p.waitForTimeout(400);
}
if (panel) {
  await p.evaluate(k => document.querySelector(`#panels [data-panel="${k}"]`)?.click(), panel);
  await p.waitForTimeout(2600);
  // Shut the flyout so the map's own controls are visible.
  await p.evaluate(() => document.querySelector('.dom-ico.on')?.click());
  await p.waitForTimeout(500);
}
if (hoverRow) {
  const row = await p.$('#wb-body .blk-row[data-block]');
  if (row) { await row.hover(); await p.waitForTimeout(700); }
}
await p.screenshot({ path: out });
console.log('wrote', out);
await b.close();
