import { chromium } from 'playwright';
const b = await chromium.launch({ channel: 'chrome' });
const p = await b.newPage({ viewport: { width: 1600, height: 950 } });
p.on('pageerror', e => console.log('pageerror:', e.message));
p.on('console', m => { if (m.type() === 'error' && !/Failed to load/.test(m.text())) console.log('console:', m.text()); });
await p.goto('http://127.0.0.1:8001/command', { waitUntil: 'domcontentloaded' });
await p.waitForTimeout(9000);
// paint NDRE, then compare it with recorded yield
await p.click('#metric-btn');
await p.waitForTimeout(400);
await p.evaluate(() => document.querySelector('#metrics [data-metric="ndre"]').click());
await p.waitForTimeout(1800);
await p.click('#metric-btn');
await p.waitForTimeout(400);
await p.evaluate(() => {
  const b = document.querySelector('#metrics [data-metric="bunches_per_ha"]');
  b.querySelector('.metric-cmp').click();
});
await p.waitForTimeout(2600);
console.log(await p.evaluate(() => {
  const t = (document.getElementById('wb-title') || {}).textContent;
  const body = (document.getElementById('wb-body').textContent || '').replace(/\s+/g, ' ').trim();
  const refs = document.querySelectorAll('#wb-body [data-block]').length;
  return `  tab: ${t}\n  blocks pointing: ${refs}\n  body: ${body.slice(0, 230)}`;
}));
await b.close();
