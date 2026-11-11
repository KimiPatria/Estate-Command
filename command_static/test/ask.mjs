/* Ask the map: buttons for clarifying questions and follow-ups, and what the
 * browser sends back on the next turn. /gis/ask is stubbed, so this calls no
 * model and only needs the page to load.
 *
 *     node test/ask.mjs [baseURL] [shotPath]
 *
 * Expects dashboard_server.py to be running.
 */
import { chromium } from 'playwright';

const BASE = process.argv[2] || 'http://127.0.0.1:8001';
const SHOT = process.argv[3];
const failures = [];
const check = (name, ok, detail) => {
  console.log(`${ok ? 'ok  ' : 'FAIL'} ${name}${!ok && detail !== undefined ? ` :: ${JSON.stringify(detail)}` : ''}`);
  if (!ok) failures.push(name);
};

const SET = { estate: 'EC', blocks: ['35-33'], metric: 'peer_index' };
const base = { available: true, estate: 'EC', focus: null, trace: [], tools_used: [],
  figure_audit: { checked: 2, unverified: [], clean: true }, model: 'amazon.nova-pro-v1:0',
  latency_ms: 1200, usage: {} };
const REPLIES = [
  { ...base, kind: 'clarify', answer: 'Cost or yield?',
    options: ['Cost', '<b>Yield</b>'], suggestions: [], working_set: {},
    figure_audit: { checked: 0, unverified: [], clean: true } },
  { ...base, kind: 'answer', answer: '35-33 is the worst at 0.61.', options: [],
    suggestions: ['Why is 35-33 so low?', 'What does it cost us?'], working_set: SET },
  { ...base, kind: 'answer', answer: 'Nothing recorded for that, so 0 qualify.', options: [],
    suggestions: [], working_set: { ...SET, month: '2025-04' } },
];

const browser = await chromium.launch({ channel: 'chrome' });
const page = await browser.newPage({ viewport: { width: 1600, height: 950 } });
const sent = [];
page.on('pageerror', e => check(`no page error (${e.message})`, false));
await page.route('**/gis/ask', async route => {
  sent.push(route.request().postDataJSON());
  await route.fulfill({ json: REPLIES[sent.length - 1] });
});

await page.goto(`${BASE}/command`, { waitUntil: 'domcontentloaded' });
await page.waitForFunction(() => {
  const el = document.getElementById('intro');
  return !el || el.hidden || getComputedStyle(el).display === 'none'
      || getComputedStyle(el).opacity === '0';
}, null, { timeout: 30000 });
await page.waitForTimeout(2500);
await page.evaluate(() => document.getElementById('ask-launch').click());

const waitFor = n => page.waitForFunction(
  k => document.querySelectorAll('#ask-body .qa').length === k
    && !document.querySelector('#ask-body .thinking'), n, { timeout: 10000 });
const labels = () => page.$$eval('#ask-body .ask-choices button', b => b.map(x => x.textContent));

// ── a clarifying question ────────────────────────────────────────────────
await page.fill('#ask-input', 'Is it bad?');
await page.press('#ask-input', 'Enter');
await waitFor(1);
check('first turn sends no working set', sent[0].working_set === null, sent[0].working_set);
check('options render as buttons', (await labels()).join('|') === 'Cost|<b>Yield</b>', await labels());
check('option text is escaped', await page.$('#ask-body .ask-choices b') === null);
check('options are styled as a choice', !!await page.$('#ask-body .ask-choices.options'));
check('no figure chip on a clarification', !await page.$('#ask-body .audit'));

// ── picking an option is the reply ───────────────────────────────────────
await page.click('#ask-body .ask-choices button:first-child');
await waitFor(2);
check('the option is sent as the question', sent[1].question === 'Cost', sent[1].question);
check('the clarification is marked in history',
  sent[1].history.length === 1 && sent[1].history[0].kind === 'clarify', sent[1].history);
check('suggestions replace the options',
  (await labels()).join('|') === 'Why is 35-33 so low?|What does it cost us?', await labels());
check('only the newest turn has buttons',
  await page.$$eval('#ask-body .qa:not(:first-child) .ask-choices', e => e.length) === 0);
if (SHOT) await page.locator('#ask').screenshot({ path: SHOT });

// ── the working set comes back on the next turn ──────────────────────────
await page.click('#ask-body .ask-choices button:first-child');
await waitFor(3);
check('a suggestion is sent as the question', sent[2].question === 'Why is 35-33 so low?', sent[2].question);
check('the working set is handed back', JSON.stringify(sent[2].working_set) === JSON.stringify(SET), sent[2].working_set);
check('no buttons when there are no suggestions', (await labels()).length === 0, await labels());

// ── it survives a reload ─────────────────────────────────────────────────
const stored = await page.evaluate(() => {
  const { chats, activeChat } = JSON.parse(localStorage.getItem('askMapChats.v1'));
  return chats.find(c => c.id === activeChat).history[0].workingSet;
});
check('the working set is saved with the chat', stored && stored.month === '2025-04', stored);

// Leave no test chat behind in this browser profile (a fresh one, but be tidy).
await browser.close();
console.log(failures.length ? `\n${failures.length} failed` : '\nall passed');
process.exit(failures.length ? 1 : 0);
