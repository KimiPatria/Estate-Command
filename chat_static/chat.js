/* EPMS AI Chat: page logic.
 *
 * One conversation is a column of messages. An answer is built up live from
 * the /chat/stream events: the steps the server is working through, the result
 * table, a chart, then the text as it is written. A reopened chat is drawn
 * from the same data, stored with each turn, so it looks the way it did.
 */
'use strict';

/* ── tiny helpers ─────────────────────────────────────────────────── */
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const root = document.documentElement;

function esc(s) {
  return String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
function el(tag, cls, html) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (html !== undefined) n.innerHTML = html;
  return n;
}
function store(key, value) {
  try {
    if (value === undefined) return localStorage.getItem(key);
    localStorage.setItem(key, value);
  } catch (e) { /* private mode: settings just don't persist */ }
  return null;
}
function uuid() {
  if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => {
    const r = Math.random() * 16 | 0;
    return (c === 'x' ? r : (r & 0x3 | 0x8)).toString(16);
  });
}
const cssVar = name => getComputedStyle(root).getPropertyValue(name).trim();
const reducedMotion = () => window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches;
const isMobile = () => window.matchMedia && matchMedia('(max-width: 860px)').matches;

/* ── icons (24px stroke set) ──────────────────────────────────────── */
const ICONS = {
  plus: '<path d="M12 5v14M5 12h14"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/>',
  'arrow-up': '<path d="M12 19V5M5 12l7-7 7 7"/>',
  'arrow-down': '<path d="M12 5v14M5 12l7 7 7-7"/>',
  'arrow-right': '<path d="M5 12h14M13 6l6 6-6 6"/>',
  stop: '<rect x="7" y="7" width="10" height="10" rx="2" fill="currentColor" stroke="none"/>',
  copy: '<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V6a2 2 0 0 1 2-2h9"/>',
  check: '<path d="m5 12.5 4.5 4.5L19 7.5"/>',
  up: '<path d="M7 10v12"/><path d="M15 5.88 14 10h5.83a2 2 0 0 1 1.92 2.56l-2.33 8A2 2 0 0 1 17.5 22H4a2 2 0 0 1-2-2v-8a2 2 0 0 1 2-2h2.76a2 2 0 0 0 1.79-1.11L12 2a3.13 3.13 0 0 1 3 3.88Z"/>',
  down: '<path d="M17 14V2"/><path d="M9 18.12 10 14H4.17a2 2 0 0 1-1.92-2.56l2.33-8A2 2 0 0 1 6.5 2H20a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2h-2.76a2 2 0 0 0-1.79 1.11L12 22a3.13 3.13 0 0 1-3-3.88Z"/>',
  refresh: '<path d="M21 12a9 9 0 1 1-9-9c2.52 0 4.93 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/>',
  pencil: '<path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z"/>',
  more: '<circle cx="5" cy="12" r="1.5" fill="currentColor" stroke="none"/><circle cx="12" cy="12" r="1.5" fill="currentColor" stroke="none"/><circle cx="19" cy="12" r="1.5" fill="currentColor" stroke="none"/>',
  pin: '<path d="M12 17v5"/><path d="M9 3h6l-1 6 3 3v2H7v-2l3-3-1-6Z"/>',
  trash: '<path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/>',
  chevron: '<path d="m6 9 6 6 6-6"/>',
  sidebar: '<rect x="3" y="4" width="18" height="16" rx="2.5"/><path d="M9 4v16"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
  moon: '<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8Z"/>',
  database: '<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v14c0 1.7 3.6 3 8 3s8-1.3 8-3V5"/><path d="M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/>',
  sliders: '<path d="M4 6h9M19 6h1M4 12h3M13 12h7M4 18h11M20 18h0"/><circle cx="16" cy="6" r="2.2"/><circle cx="10" cy="12" r="2.2"/><circle cx="17.5" cy="18" r="2.2"/>',
  download: '<path d="M12 4v12M6 11l6 6 6-6M5 20h14"/>',
  x: '<path d="M6 6l12 12M18 6 6 18"/>',
  chart: '<path d="M4 20V10M10 20V4M16 20v-7M21 20H3"/>',
  table: '<rect x="3" y="4" width="18" height="16" rx="2.5"/><path d="M3 10h18M9 4v16"/>',
  code: '<path d="m8 8-4 4 4 4M16 8l4 4-4 4"/>',
  alert: '<path d="M12 9v4M12 17h.01"/><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z"/>',
  spark: '<path d="m12 3 1.9 5.6L19.5 10l-5.6 1.9L12 17.5l-1.9-5.6L4.5 10l5.6-1.4Z" fill="currentColor"/>',
  cpu: '<rect x="6" y="6" width="12" height="12" rx="2"/><path d="M10 2v4M14 2v4M10 18v4M14 18v4M2 10h4M2 14h4M18 10h4M18 14h4"/>',
  book: '<path d="M5 4h11a3 3 0 0 1 3 3v13H8a3 3 0 0 1-3-3Z"/><path d="M5 17a3 3 0 0 1 3-3h11"/>',
  report: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8Z"/><path d="M14 3v5h5M9 13h6M9 17h6"/>',
  forecast: '<path d="M3 17l5-6 4 3 5-7 4 4"/><path d="M3 21h18"/>',
};
function ico(name) {
  return `<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[name] || ''}</svg>`;
}

/* ── state ────────────────────────────────────────────────────────── */
const state = {
  sessionId: uuid(),
  sessions: [],
  busy: false,
  abort: null,
  view: null,           // the answer being written
  views: [],            // every answer on screen (for theme changes)
  stick: true,          // keep the newest text in view
  editing: false,
  lastUser: null,       // text of the newest user message
  lastSaved: false,     // is the newest exchange stored on the server?
  database: store('chat-db') || 'live',
  model: store('chat-model-v2') || '',      // '' = automatic
  source: store('chat-source') || 'sql',
  dbs: [],
  models: [],
  modelLabels: {},
};
const SOURCES = [
  { key: 'sql', label: 'Database', desc: 'Query the EPMS data', icon: 'database' },
  { key: 'knowledge_base', label: 'Documents', desc: 'Search the knowledge base', icon: 'book' },
  { key: 'both', label: 'Both', desc: 'Combine data and documents', icon: 'spark' },
];
const thread = $('#thread');
const scroller = $('#scroller');
const input = $('#msg-input');
const sendBtn = $('#send-btn');

const db = () => state.dbs.find(d => d.key === state.database) || { key: state.database, label: 'Live estates', estates: [], starters: [] };

/* ── formatting ───────────────────────────────────────────────────── */
function fmtNum(v) {
  if (typeof v !== 'number' || !isFinite(v)) return String(v ?? '');
  const a = Math.abs(v);
  const digits = Number.isInteger(v) ? 0 : a < 10 ? 2 : a < 1000 ? 1 : 0;
  return v.toLocaleString('en-US', { maximumFractionDigits: digits, minimumFractionDigits: 0 });
}
const compact = new Intl.NumberFormat('en-US', { notation: 'compact', maximumFractionDigits: 1 });
// Whole numbers that are labels, not amounts: no thousands separator.
const PLAIN_INT = /^(?:year|tahun|month|bulan|week|minggu|day|id|no|block|blok|werks|divnr)$|(?:_id|_no|code|kode|_year|_tahun)$/i;

function md(text) {
  // Half-written bold while streaming would flash raw asterisks.
  if ((text.match(/\*\*/g) || []).length % 2) text += '**';
  if (window.marked && window.DOMPurify) {
    const html = marked.parse(text, { gfm: true, breaks: true });
    return DOMPurify.sanitize(html, {
      ALLOWED_TAGS: ['p', 'br', 'strong', 'em', 'b', 'i', 'ul', 'ol', 'li', 'code', 'pre', 'blockquote',
        'h1', 'h2', 'h3', 'h4', 'hr', 'table', 'thead', 'tbody', 'tr', 'th', 'td', 'a', 'del', 'span'],
      ALLOWED_ATTR: ['href', 'align', 'title'],
    });
  }
  // Libraries missing: paragraphs and bold only, everything escaped.
  return esc(text).split(/\n{2,}/).map(p =>
    `<p>${p.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>').replace(/\n/g, '<br>')}</p>`).join('');
}
if (window.DOMPurify) {
  DOMPurify.addHook('afterSanitizeAttributes', node => {
    if (node.tagName === 'A') { node.setAttribute('target', '_blank'); node.setAttribute('rel', 'noopener noreferrer'); }
  });
}

function toast(msg) {
  const t = $('#toast');
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.remove('show'), 2600);
}
function announce(msg) { $('#live').textContent = msg; }
async function copyText(text, done = 'Copied') {
  try {
    await navigator.clipboard.writeText(text);
  } catch (e) {
    const ta = el('textarea'); ta.value = text; document.body.appendChild(ta); ta.select();
    try { document.execCommand('copy'); } catch (_) {}
    ta.remove();
  }
  toast(done);
}
function flashCheck(btn, name) {
  btn.innerHTML = ico('check');
  setTimeout(() => { btn.innerHTML = ico(name); }, 1400);
}

/* ── scrolling ────────────────────────────────────────────────────── */
function nearBottom() { return scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 90; }
function stick(force) {
  if (force) state.stick = true;
  if (state.stick) scroller.scrollTop = scroller.scrollHeight;
  $('#to-bottom').hidden = state.stick || !thread.querySelector('.msg');
}
scroller.addEventListener('scroll', () => {
  state.stick = nearBottom();
  $('#to-bottom').hidden = state.stick || !thread.querySelector('.msg');
}, { passive: true });
$('#to-bottom').addEventListener('click', () => { scroller.scrollTo({ top: scroller.scrollHeight, behavior: reducedMotion() ? 'auto' : 'smooth' }); state.stick = true; });

/* ── empty state ──────────────────────────────────────────────────── */
function showWelcome() {
  destroyViews();
  const d = db();
  const hour = new Date().getHours();
  const hello = hour < 11 ? 'Good morning' : hour < 17 ? 'Good afternoon' : 'Good evening';
  const starters = (d.starters && d.starters.length ? d.starters : [
    'Total harvest in the last 30 days', 'Which blocks had the most ripe bunches this year?']).slice(0, 4);
  thread.innerHTML = `
    <div class="welcome" id="welcome">
      <h1>${hello}. What would you like to know?</h1>
      <p>Asking <button type="button" id="w-db">${esc(d.label)}</button>${d.estates && d.estates.length ? ' · ' + esc(d.estates.join(', ')) : ''}</p>
      <div class="starters">
        ${starters.map(s => `<button type="button" class="starter">${ico('arrow-right')}<span>${esc(s)}</span></button>`).join('')}
      </div>
    </div>`;
  $('#w-db').addEventListener('click', e => { e.stopPropagation(); toggleTools(true); });
  $$('.starter', thread).forEach(b => b.addEventListener('click', () => send(b.textContent)));
  setTitle('New chat');
  stick(true);
}

/* ── messages ─────────────────────────────────────────────────────── */
function addUser(text) {
  const m = el('div', 'msg user');
  m.innerHTML = `<div class="user-pill"></div>
    <div class="msg-tools">
      <button class="tool" data-act="copy" title="Copy" aria-label="Copy message">${ico('copy')}</button>
      <button class="tool only-last" data-act="edit" title="Edit" aria-label="Edit message">${ico('pencil')}</button>
    </div>`;
  $('.user-pill', m).textContent = text;
  $('[data-act=copy]', m).addEventListener('click', e => { copyText(text); flashCheck(e.currentTarget, 'copy'); });
  $('[data-act=edit]', m).addEventListener('click', () => startEdit(text));
  thread.appendChild(m);
  return m;
}

function newView() {
  const node = el('div', 'msg ai');
  node.innerHTML = `
    <div class="steps running">
      <button type="button" class="steps-head" aria-expanded="false">
        <span class="steps-label">Thinking</span><span class="chev">${ico('chevron')}</span>
      </button>
      <ol class="steps-list"></ol>
    </div>
    <div class="answer" aria-live="off"></div>
    <div class="slot-result"></div>
    <div class="slot-facts"></div>
    <div class="msg-tools" hidden></div>
    <div class="slot-follow"></div>`;
  const v = {
    el: node, steps: new Map(), text: '', shown: 0, raf: 0,
    data: { columns: [], rows: [], row_count: 0, chart: null, sql: null, citations: [] },
    final: null, finished: false, tab: undefined, tabChosen: false, chartType: null, chartObj: null, sort: null,
  };
  v.done = new Promise(res => { v.resolve = res; });
  $('.steps-head', node).addEventListener('click', () => {
    const s = $('.steps', node);
    if (!$('.steps-list', node).children.length) return;
    const open = s.classList.toggle('open');
    $('.steps-head', node).setAttribute('aria-expanded', String(open));
  });
  state.views.push(v);
  return v;
}
function destroyViews() {
  state.views.forEach(v => { if (v.chartObj) v.chartObj.destroy(); cancelAnimationFrame(v.raf); });
  state.views = [];
}

/* steps */
function setStep(v, s) {
  const prev = v.steps.get(s.id) || {};
  const step = { ...prev, ...s };
  v.steps.set(s.id, step);
  let li = $(`[data-step="${s.id}"]`, v.el);
  if (!li) {
    li = el('li', 'step');
    li.dataset.step = s.id;
    $('.steps-list', v.el).appendChild(li);
  }
  li.className = 'step ' + step.status;
  const ms = step.ms >= 100 ? `<span class="step-ms">${(step.ms / 1000).toFixed(1)}s</span>` : '';
  li.innerHTML = `<div class="step-line"><span>${esc(step.label)}</span>${
    step.detail ? `<span class="step-detail">${esc(step.detail)}</span>` : ''}${ms}</div>${
    step.sql ? `<pre class="code">${esc(step.sql)}</pre>` : ''}`;
  if (step.status === 'running') {
    $('.steps-label', v.el).textContent = step.label;
    announce(step.label);
  }
}
function stepsSummary(f) {
  const secs = f.elapsed_ms ? ` · ${(f.elapsed_ms / 1000).toFixed(1)}s` : '';
  const name = f.database_label || (state.dbs.find(d => d.key === f.database) || {}).label || 'the database';
  if (f.error_type === 'off_domain') return null;
  if (f.error_type) return "Couldn't get data for this" + secs;
  if (f.route === 'sql') return `Queried ${name} · ${fmtNum(f.row_count)} row${f.row_count === 1 ? '' : 's'}${secs}`;
  if (f.route === 'overview') {
    const n = (f.parts || []).length;
    return `Looked at ${n} measure${n === 1 ? '' : 's'} in ${name}${secs}`;
  }
  if (f.route === 'context') return (f.chart ? 'Charted the previous result' : 'Answered from the conversation') + secs;
  if (f.route === 'forecast') return 'Read the forecast page' + secs;
  if (f.route === 'knowledge_base') return 'Searched the knowledge base' + secs;
  if (f.route === 'both') return `Queried ${name} and the knowledge base${secs}`;
  return null;
}
function closeSteps(v, label) {
  const box = $('.steps', v.el);
  if (!box) return;
  if (!label) { box.remove(); return; }
  box.classList.remove('running');
  $('.steps-label', box).textContent = label;
  if (!$('.steps-list', box).children.length) $('.chev', box).remove();
}

/* answer text: typed out a little behind the stream, so it reads smoothly */
function paint(v) {
  const a = $('.answer', v.el);
  a.innerHTML = md(v.text.slice(0, v.shown));
  a.classList.toggle('typing', !v.finished);
}
function pump(v) {
  if (v.raf || v.finished) return;
  const tick = () => {
    v.raf = 0;
    const gap = v.text.length - v.shown;
    if (gap > 0) {
      v.shown = (document.hidden || reducedMotion()) ? v.text.length
        : Math.min(v.text.length, v.shown + Math.max(2, Math.ceil(gap / 9)));
      paint(v);
      stick();
    }
    if (v.shown < v.text.length) v.raf = requestAnimationFrame(tick);
    else if (v.final) finish(v);
  };
  v.raf = requestAnimationFrame(tick);
  // A background tab gets no animation frames: don't leave the answer hanging.
  if (document.hidden) { cancelAnimationFrame(v.raf); tick(); }
}

/* ── result card: chart / table / query ───────────────────────────── */
const TYPE_LABELS = { bar: 'Columns', hbar: 'Bars', line: 'Line', area: 'Area', pie: 'Donut' };
function chartTypes(c) {
  if (c.type === 'scatter' || c.type === 'forecast') return [];
  if (c.type === 'pie') return ['pie', 'hbar'];
  const many = c.series.length > 1;
  return [...new Set([c.type, 'bar', many ? null : 'hbar', 'line'].filter(Boolean))];
}

function renderResult(v) {
  const slot = $('.slot-result', v.el);
  const d = v.data;
  if (v.chartObj) { v.chartObj.destroy(); v.chartObj = null; }
  const hasRows = d.columns.length && d.rows.length;
  if (!hasRows && !d.sql && !d.chart && !(d.citations && d.citations.length)) { slot.innerHTML = ''; return; }

  const tabs = [];
  if (d.chart) tabs.push(['chart', 'Chart', 'chart']);
  if (hasRows) tabs.push(['table', 'Table', 'table']);
  if (d.sql) tabs.push(['sql', 'Query', 'code']);
  if (d.citations && d.citations.length) tabs.push(['cite', `Sources (${d.citations.length})`, 'book']);
  if (!v.tabChosen) {
    v.tab = d.chart ? 'chart' : (hasRows && (d.rows.length > 1 || d.columns.length > 3)) ? 'table'
      : (d.citations && d.citations.length && !hasRows) ? null : null;
  }
  if (v.tab && !tabs.some(t => t[0] === v.tab)) v.tab = null;
  if (d.chart && !v.chartType) v.chartType = d.chart.type;

  const shown = d.rows.length;
  const meta = hasRows
    ? (d.row_count > shown ? `first ${fmtNum(shown)} of ${fmtNum(d.row_count)} rows` : `${fmtNum(d.row_count || shown)} row${(d.row_count || shown) === 1 ? '' : 's'}`)
    : '';
  const types = d.chart && v.tab === 'chart' ? chartTypes(d.chart) : [];
  const card = el('div', 'result' + (v.tab ? ' has-pane' : ''));
  card.innerHTML = `
    <div class="result-head">
      <div class="tabs" role="tablist">${tabs.map(([k, label, icon]) =>
        `<button type="button" class="tab${v.tab === k ? ' on' : ''}" role="tab" aria-selected="${v.tab === k}" data-tab="${k}">${ico(icon)}<span>${label}</span></button>`).join('')}</div>
      <span class="result-meta">${meta}</span>
      <div class="result-tools">
        ${types.length > 1 ? `<div class="seg" role="group" aria-label="Chart type">${types.map(t =>
          `<button type="button" class="${v.chartType === t ? 'on' : ''}" data-type="${t}">${TYPE_LABELS[t]}</button>`).join('')}</div>` : ''}
        ${v.tab === 'table' || v.tab === 'sql' ? `<button type="button" class="tool" data-act="copy" title="Copy" aria-label="Copy">${ico('copy')}</button>` : ''}
        ${v.tab === 'table' || v.tab === 'chart' ? `<button type="button" class="tool" data-act="download" title="${v.tab === 'chart' ? 'Download image' : 'Download CSV'}" aria-label="Download">${ico('download')}</button>` : ''}
      </div>
    </div>
    <div class="pane pane-${v.tab || 'none'}${v.tab ? ' on' : ''}"></div>`;
  slot.innerHTML = '';
  slot.appendChild(card);

  $$('.tab', card).forEach(b => b.addEventListener('click', () => {
    v.tabChosen = true;
    v.tab = v.tab === b.dataset.tab ? null : b.dataset.tab;      // click again to fold away
    renderResult(v);
  }));
  $$('.seg button', card).forEach(b => b.addEventListener('click', () => { v.chartType = b.dataset.type; renderResult(v); }));
  const copyBtn = $('[data-act=copy]', card);
  if (copyBtn) copyBtn.addEventListener('click', () => {
    copyText(v.tab === 'sql' ? d.sql : toDelimited(d, '\t'), v.tab === 'sql' ? 'Query copied' : 'Table copied');
    flashCheck(copyBtn, 'copy');
  });
  const dl = $('[data-act=download]', card);
  if (dl) dl.addEventListener('click', () => (v.tab === 'chart' ? downloadChart(v) : downloadCsv(d)));

  const pane = $('.pane', card);
  if (v.tab === 'chart') {
    const tall = v.chartType === 'hbar' ? Math.max(220, d.chart.labels.length * 30 + 50) : null;
    pane.innerHTML = `${d.chart.title ? `<div class="chart-title">${esc(d.chart.title)}</div>` : ''}
      <div class="chart-box"${tall ? ` style="height:${tall}px"` : ''}><canvas role="img" aria-label="${esc(d.chart.title || 'Chart of the result')}"></canvas></div>
      ${d.chart.truncated ? '<div class="chart-note">Showing the first part of the result. The table has every row.</div>' : ''}`;
    drawChart(v, $('canvas', pane));
  } else if (v.tab === 'table') {
    pane.appendChild(buildTable(v));
  } else if (v.tab === 'sql') {
    pane.innerHTML = `<pre class="code">${esc(d.sql)}</pre>`;
  } else if (v.tab === 'cite') {
    pane.innerHTML = d.citations.map(c => `<div class="cite">${esc(c.text || '')}</div>`).join('');
  }
}

function isNumCol(rows, c) {
  let seen = false;
  for (const r of rows) {
    const x = r[c];
    if (x === null || x === undefined || x === '') continue;
    if (typeof x !== 'number') return false;
    seen = true;
  }
  return seen;
}
function cell(v, col, numeric) {
  if (v === null || v === undefined) return '';
  if (numeric) return Number.isInteger(v) && PLAIN_INT.test(col) ? String(v) : fmtNum(v);
  return String(v).replace(/^(\d{4}-\d{2}-\d{2})[ T]00:00:00(\.0+)?$/, '$1');
}
function buildTable(v) {
  const { columns, rows, row_count } = v.data;
  const nums = Object.fromEntries(columns.map(c => [c, isNumCol(rows, c)]));
  let view = rows;
  if (v.sort) {
    const { col, dir } = v.sort;
    view = [...rows].sort((a, b) => {
      const x = a[col], y = b[col];
      if (x === y) return 0;
      if (x === null || x === undefined) return 1;
      if (y === null || y === undefined) return -1;
      return (nums[col] ? x - y : String(x).localeCompare(String(y), undefined, { numeric: true })) * dir;
    });
  }
  const wrap = el('div');
  wrap.innerHTML = `<div class="tbl-wrap"><table class="tbl">
    <thead><tr>${columns.map(c => `<th class="${nums[c] ? 'num' : ''}" aria-sort="${v.sort && v.sort.col === c ? (v.sort.dir > 0 ? 'ascending' : 'descending') : 'none'}">
      <button type="button" data-col="${esc(c)}"><span>${esc(c.replace(/_/g, ' '))}</span><span class="dir">${v.sort && v.sort.col === c ? (v.sort.dir > 0 ? '▲' : '▼') : ''}</span></button></th>`).join('')}</tr></thead>
    <tbody>${view.map(r => `<tr>${columns.map(c => `<td class="${nums[c] ? 'num' : ''}">${esc(cell(r[c], c, nums[c]))}</td>`).join('')}</tr>`).join('')}</tbody>
  </table></div>${row_count > rows.length ? `<div class="tbl-foot">Showing the first ${fmtNum(rows.length)} of ${fmtNum(row_count)} rows.</div>` : ''}`;
  $$('th button', wrap).forEach(b => b.addEventListener('click', () => {
    const col = b.dataset.col;
    v.sort = v.sort && v.sort.col === col ? (v.sort.dir > 0 ? { col, dir: -1 } : null) : { col, dir: 1 };
    renderResult(v);
  }));
  return wrap;
}
function toDelimited(d, sep) {
  const q = x => {
    const s = x === null || x === undefined ? '' : String(x);
    return sep === ',' && /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s.replace(/[\t\n]/g, ' ');
  };
  return [d.columns.map(q).join(sep), ...d.rows.map(r => d.columns.map(c => q(r[c])).join(sep))].join('\n');
}
function saveBlob(blob, name) {
  const a = el('a');
  a.href = URL.createObjectURL(blob);
  a.download = name;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 2000);
}
function downloadCsv(d) {
  saveBlob(new Blob(['﻿' + toDelimited(d, ',')], { type: 'text/csv;charset=utf-8' }), 'epms-result.csv');
  if (d.row_count > d.rows.length) toast(`Saved the first ${fmtNum(d.rows.length)} of ${fmtNum(d.row_count)} rows`);
}
function downloadChart(v) {
  if (!v.chartObj) return;
  const src = v.chartObj.canvas;
  const out = el('canvas');
  out.width = src.width; out.height = src.height;
  const ctx = out.getContext('2d');
  ctx.fillStyle = cssVar('--surface'); ctx.fillRect(0, 0, out.width, out.height);
  ctx.drawImage(src, 0, 0);
  out.toBlob(b => b && saveBlob(b, 'epms-chart.png'));
}

/* charts */
function shortLabel(label, kind) {
  const s = String(label);
  if (kind === 'date') {
    let m = s.match(/^(\d{4})-(\d{2})-(\d{2})/);
    if (m) return new Date(+m[1], +m[2] - 1, +m[3]).toLocaleDateString('en-GB', { day: 'numeric', month: 'short' });
    m = s.match(/^(\d{4})-(\d{2})$/);
    if (m) return new Date(+m[1], +m[2] - 1, 1).toLocaleDateString('en-GB', { month: 'short', year: '2-digit' });
  }
  return s.length > 18 ? s.slice(0, 17) + '…' : s;
}
function drawChart(v, canvas) {
  if (!window.Chart) { canvas.parentElement.innerHTML = '<div class="chart-note">Charts are unavailable (library not loaded). Use the table.</div>'; return; }
  const c = v.data.chart;
  const type = v.chartType || c.type;
  const colors = [1, 2, 3, 4, 5, 6, 7, 8].map(i => cssVar('--s' + i));
  const surface = cssVar('--surface'), muted = cssVar('--muted'), text = cssVar('--text'), grid = cssVar('--grid');
  const horizontal = type === 'hbar';
  const pie = type === 'pie';
  const forecast = type === 'forecast';
  const linear = type === 'line' || type === 'area' || forecast;
  const points = c.labels.length;

  let datasets;
  if (forecast) {
    // Actuals, then the forecast dashed from the last actual month, with the
    // likely range shaded between two hidden lines (same shape as /forecast).
    const band = c.band || { low: [], high: [] };
    const actualC = colors[0], fcC = colors[1];
    const hidden = { borderWidth: 0, pointRadius: 0, pointHoverRadius: 0, borderColor: 'transparent', spanGaps: false, tension: 0 };
    datasets = [
      { label: '_low', data: band.low, fill: false, backgroundColor: fcC + '26', ...hidden },
      { label: band.name || 'Likely range', data: band.high, fill: '-1', backgroundColor: fcC + '26', ...hidden },
      { label: c.series[0].name, data: c.series[0].values, borderColor: actualC, backgroundColor: actualC, borderWidth: 2, tension: 0, spanGaps: false,
        pointRadius: points > 40 ? 0 : 3, pointHoverRadius: 5.5, pointBackgroundColor: actualC, pointBorderColor: surface, pointBorderWidth: 1.5 },
      { label: c.series[1].name, data: c.series[1].values, borderColor: fcC, backgroundColor: fcC, borderWidth: 2, borderDash: [6, 4], tension: 0, spanGaps: false,
        pointRadius: 3, pointHoverRadius: 5.5, pointBackgroundColor: fcC, pointBorderColor: surface, pointBorderWidth: 1.5 },
    ];
  } else if (type === 'scatter') {
    datasets = c.series.map((s, i) => ({
      label: s.name, data: s.points.map(p => ({ x: p[0], y: p[1] })),
      backgroundColor: colors[i], borderColor: surface, borderWidth: 1.5, pointRadius: 4.5, pointHoverRadius: 6,
    }));
  } else if (pie) {
    datasets = [{ label: c.series[0].name, data: c.series[0].values, backgroundColor: colors, borderColor: surface, borderWidth: 2, hoverOffset: 4 }];
  } else {
    datasets = c.series.map((s, i) => linear ? {
      label: s.name, data: s.values, borderColor: colors[i], backgroundColor: type === 'area' ? colors[i] + '26' : colors[i],
      fill: type === 'area', borderWidth: 2, tension: 0, spanGaps: false,
      pointRadius: points > 40 ? 0 : 3.5, pointHoverRadius: 5.5, pointBackgroundColor: colors[i], pointBorderColor: surface, pointBorderWidth: 1.5,
    } : {
      label: s.name, data: s.values, backgroundColor: colors[i], borderRadius: 4, borderSkipped: 'start',
      maxBarThickness: 34, categoryPercentage: .78, barPercentage: c.series.length > 1 ? .9 : .82,
    });
  }

  const tick = { color: muted, font: { family: 'Inter, system-ui, sans-serif', size: 11.5 } };
  const valueAxis = { beginAtZero: !linear || c.series.length === 1, grid: { color: grid, drawTicks: false }, border: { display: false },
    ticks: { ...tick, padding: 8, maxTicksLimit: 6, callback: x => compact.format(x) },
    title: { display: !!c.y_label && !pie, text: c.y_label, color: muted, font: { size: 11.5 } } };
  const labelAxis = { grid: { display: false }, border: { color: grid },
    ticks: { ...tick, maxRotation: 0, autoSkip: true, autoSkipPadding: 14,
      callback(value) { return shortLabel(this.getLabelForValue(value), c.x_kind); } } };
  let scales = {};
  if (type === 'scatter') {
    scales = { x: { ...valueAxis, beginAtZero: false, title: { display: true, text: c.x_label, color: muted, font: { size: 11.5 } } }, y: { ...valueAxis, beginAtZero: false } };
  } else if (!pie) {
    scales = horizontal ? { x: valueAxis, y: { ...labelAxis, ticks: { ...labelAxis.ticks, autoSkip: false } } } : { x: labelAxis, y: valueAxis };
  }

  v.chartObj = new Chart(canvas, {
    type: pie ? 'doughnut' : type === 'scatter' ? 'scatter' : linear ? 'line' : 'bar',
    data: { labels: c.labels, datasets },
    options: {
      responsive: true, maintainAspectRatio: false,
      indexAxis: horizontal ? 'y' : 'x',
      animation: reducedMotion() ? false : { duration: 450 },
      cutout: pie ? '58%' : undefined,
      interaction: type === 'scatter' || pie ? { mode: 'nearest', intersect: true } : { mode: 'index', intersect: false, axis: horizontal ? 'y' : 'x' },
      layout: { padding: { top: 4, right: 8 } },
      scales,
      plugins: {
        legend: {
          display: pie || c.series.length > 1, position: pie ? 'right' : 'top', align: pie ? 'center' : 'start',
          labels: { color: text, usePointStyle: true, pointStyle: 'circle', boxWidth: 7, boxHeight: 7, padding: 14, font: { family: 'Inter, system-ui, sans-serif', size: 12 },
            filter: item => item.text !== '_low' },
        },
        tooltip: {
          backgroundColor: text, titleColor: surface, bodyColor: surface, padding: 10, cornerRadius: 8, boxPadding: 4, usePointStyle: true,
          titleFont: { family: 'Inter, system-ui, sans-serif', size: 12, weight: '600' }, bodyFont: { family: 'Inter, system-ui, sans-serif', size: 12 },
          callbacks: {
            label(ctx) {
              if (type === 'scatter') return `${c.x_label}: ${fmtNum(ctx.parsed.x)}, ${c.y_label}: ${fmtNum(ctx.parsed.y)}`;
              if (ctx.dataset.label === '_low') return null;
              const val = pie ? ctx.parsed : horizontal ? ctx.parsed.x : ctx.parsed.y;
              if (val === null || val === undefined) return null;
              if (pie) {
                const total = ctx.dataset.data.reduce((a, b) => a + (b || 0), 0);
                return ` ${ctx.label}: ${fmtNum(val)} (${total ? (val / total * 100).toFixed(1) : 0}%)`;
              }
              return ` ${ctx.dataset.label}: ${fmtNum(val)}`;
            },
          },
        },
      },
    },
  });
}

/* ── finishing an answer ──────────────────────────────────────────── */
function finish(v) {
  if (v.finished) return;
  v.finished = true;
  const f = v.final;
  const a = $('.answer', v.el);
  v.shown = v.text.length;
  if (f.error_type) {
    a.classList.remove('typing');
    a.innerHTML = `<div class="notice">${ico('alert')}<div>${md(v.text)}</div></div>`;
  } else {
    paint(v);
  }
  closeSteps(v, stepsSummary(f));
  renderResult(v);

  const facts = [];
  if ((f.route === 'sql' || f.route === 'overview') && !f.error_type && f.tables_used && f.tables_used.length) {
    // The tables the query actually reads, not every table that was considered.
    const inSql = f.tables_used.filter(t => new RegExp('\\b' + t.replace(/\W/g, '') + '\\b', 'i').test(f.sql || ''));
    const used = inSql.length ? inSql : f.tables_used;
    facts.push('<span>From</span>' + used.map(t => `<span class="fact">${esc(t)}</span>`).join(''));
  }
  if (f.estates_carried && f.estates_carried.length) facts.push(`<span class="fact plain">Still on ${esc(f.estates_carried.join(', '))}</span>`);
  if (f.estates_failed && f.estates_failed.length) facts.push(`<span class="fact plain">No data from ${esc(f.estates_failed.join(', '))}</span>`);
  $('.slot-facts', v.el).innerHTML = facts.length ? `<div class="facts">${facts.join('')}</div>` : '';

  const tools = $('.msg-tools', v.el);
  const model = state.modelLabels[f.provider_label] || f.provider_label || '';
  tools.hidden = false;
  tools.innerHTML = `
    <button class="tool" data-act="copy" title="Copy" aria-label="Copy answer">${ico('copy')}</button>
    ${f.turn_id ? `<button class="tool${f.feedback === 'up' ? ' on' : ''}" data-act="up" title="Good answer" aria-label="Good answer" aria-pressed="${f.feedback === 'up'}">${ico('up')}</button>
    <button class="tool${f.feedback === 'down' ? ' on' : ''}" data-act="down" title="Bad answer" aria-label="Bad answer" aria-pressed="${f.feedback === 'down'}">${ico('down')}</button>` : ''}
    <button class="tool only-last" data-act="regen" title="Try again" aria-label="Regenerate answer">${ico('refresh')}</button>
    ${model ? `<span class="msg-meta">${esc(model)}</span>` : ''}`;
  $('[data-act=copy]', tools).addEventListener('click', e => { copyText(v.text); flashCheck(e.currentTarget, 'copy'); });
  $('[data-act=regen]', tools).addEventListener('click', () => state.lastUser && send(state.lastUser, { replace: true }));
  ['up', 'down'].forEach(kind => {
    const b = $(`[data-act=${kind}]`, tools);
    if (b) b.addEventListener('click', () => rate(v, kind));
  });

  setFollowUps(v, f.route === 'clarify' ? f.options : f.suggestions, f.route === 'clarify');
  announce('Answer ready');
  stick();
  v.resolve();
}
function setFollowUps(v, items, isOptions) {
  $$('.follow', thread).forEach(n => n.remove());       // only the newest answer offers a next step
  if (!items || !items.length) return;
  const box = el('div', 'follow' + (isOptions ? ' options' : ''));
  items.forEach((text, i) => {
    const b = el('button', 'follow-btn', `${ico('arrow-right')}<span>${esc(text)}</span>`);
    b.type = 'button';
    b.style.animationDelay = `${i * 60}ms`;
    b.addEventListener('click', () => send(text));
    box.appendChild(b);
  });
  $('.slot-follow', v.el).appendChild(box);
}
async function rate(v, kind) {
  const f = v.final;
  const next = f.feedback === kind ? null : kind;
  f.feedback = next;
  $$('[data-act=up],[data-act=down]', v.el).forEach(b => {
    const on = b.dataset.act === next;
    b.classList.toggle('on', on);
    b.setAttribute('aria-pressed', String(on));
  });
  try {
    await fetch('/chat/feedback', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: state.sessionId, turn_id: f.turn_id, rating: next }),
    });
    if (next) toast(next === 'up' ? 'Thanks for the feedback' : "Thanks. This query won't be reused as an example");
  } catch (e) { toast('Could not save feedback'); }
}

function showProblem(v, message, canRetry) {
  cancelAnimationFrame(v.raf); v.raf = 0;
  v.finished = true;
  closeSteps(v, null);
  $('.answer', v.el).classList.remove('typing');
  const box = el('div', 'notice', `${ico('alert')}<div><span>${esc(message)}</span>${
    canRetry ? '<button type="button" class="link-btn">Try again</button>' : ''}</div>`);
  $('.answer', v.el).appendChild(box);
  const b = $('.link-btn', box);
  if (b) b.addEventListener('click', () => state.lastUser && send(state.lastUser, { replace: true }));
  v.resolve();
}
function showStopped(v) {
  cancelAnimationFrame(v.raf); v.raf = 0;
  v.finished = true;
  v.text = v.text.slice(0, v.shown);
  closeSteps(v, v.steps.size ? 'Stopped' : null);
  if (v.text) paint(v); else $('.answer', v.el).classList.remove('typing');
  renderResult(v);
  $('.answer', v.el).insertAdjacentHTML('afterend', '<div class="stopped">You stopped this answer.</div>');
  const tools = $('.msg-tools', v.el);
  tools.hidden = false;
  tools.innerHTML = `<button class="tool only-last" data-act="regen" title="Try again" aria-label="Try again">${ico('refresh')}</button>`;
  $('[data-act=regen]', tools).addEventListener('click', () => state.lastUser && send(state.lastUser, { replace: true }));
  v.resolve();
}

/* ── sending ──────────────────────────────────────────────────────── */
function setBusy(on) {
  state.busy = on;
  document.body.classList.toggle('busy', on);
  sendBtn.classList.toggle('stop', on);
  sendBtn.innerHTML = ico(on ? 'stop' : 'arrow-up');
  sendBtn.setAttribute('aria-label', on ? 'Stop' : 'Send message');
  syncSend();
}
function syncSend() { sendBtn.disabled = !state.busy && !input.value.trim(); }
function grow() {
  input.style.height = 'auto';
  input.style.height = Math.min(input.scrollHeight, 200) + 'px';
}
function removeLastExchange() {
  const nodes = $$('.msg', thread);
  const last = nodes[nodes.length - 1], prev = nodes[nodes.length - 2];
  if (last && last.classList.contains('ai')) {
    const i = state.views.findIndex(v => v.el === last);
    if (i >= 0) { if (state.views[i].chartObj) state.views[i].chartObj.destroy(); state.views.splice(i, 1); }
    last.remove();
    if (prev && prev.classList.contains('user')) prev.remove();
  } else if (last) last.remove();
}

async function send(text, opts = {}) {
  text = (text || '').trim();
  if (!text || state.busy) return;
  const replace = !!opts.replace;
  const wasSaved = state.lastSaved;
  if (replace) removeLastExchange();
  cancelEdit(true);
  toggleTools(false);
  const w = $('#welcome'); if (w) w.remove();
  $$('.follow', thread).forEach(n => n.remove());

  addUser(text);
  const v = newView();
  thread.appendChild(v.el);
  state.view = v;
  state.lastUser = text;
  state.lastSaved = false;
  input.value = ''; grow();
  setBusy(true);
  stick(true);
  if (!state.sessions.some(s => s.id === state.sessionId)) setTitle(text);

  const ctrl = new AbortController();
  state.abort = ctrl;
  try {
    const res = await fetch('/chat/stream', {
      method: 'POST', signal: ctrl.signal,
      headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
      body: JSON.stringify({
        message: text, session_id: state.sessionId, source: state.source,
        model: state.model || null, database: state.database, replace_last: replace && wasSaved,
      }),
    });
    if (!res.ok || !res.body) {
      const e = await res.json().catch(() => ({}));
      throw new Error(e.detail || `The server answered ${res.status}`);
    }
    await readEvents(res.body, (event, data) => onEvent(v, event, data));
    if (!v.final && !v.finished) throw new Error('The connection closed before the answer arrived.');
    await v.done;
  } catch (err) {
    if (err.name === 'AbortError') showStopped(v);
    else if (!v.finished) showProblem(v, navigator.onLine === false ? 'You are offline.' : (err.message || 'Something went wrong.'), true);
  } finally {
    state.abort = null;
    state.view = null;
    setBusy(false);
    stick();
    if (!isMobile()) input.focus();
  }
}

async function readEvents(body, handle) {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buf = '';
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let cut;
    while ((cut = buf.indexOf('\n\n')) >= 0) {
      const block = buf.slice(0, cut);
      buf = buf.slice(cut + 2);
      let event = 'message', data = '';
      for (const line of block.split('\n')) {
        if (line.startsWith('event:')) event = line.slice(6).trim();
        else if (line.startsWith('data:')) data += line.slice(5).trimStart();
      }
      if (data) { try { handle(event, JSON.parse(data)); } catch (e) { console.error('bad event', event, e); } }
    }
  }
}

function onEvent(v, event, d) {
  if (event === 'step') setStep(v, d);
  else if (event === 'delta') { v.text += d.text; pump(v); }
  else if (event === 'table') { Object.assign(v.data, { columns: d.columns, rows: d.rows, row_count: d.row_count }); renderResult(v); stick(); }
  else if (event === 'chart') { v.data.chart = d; renderResult(v); stick(); }
  else if (event === 'error') showProblem(v, d.detail || 'Something went wrong.', true);
  else if (event === 'final') {
    applyFinal(v, d);
    state.sessionId = d.session_id || state.sessionId;
    state.lastSaved = true;
    setUrl(state.sessionId);
    loadSessions();
    pump(v);
  }
}
function applyFinal(v, f) {
  v.final = f;
  const answer = f.answer || '';
  if (answer !== v.text) {
    // The stored answer is the truth; keep what is already typed if it still fits.
    if (!answer.startsWith(v.text.slice(0, v.shown))) v.shown = 0;
    v.text = answer;
  }
  Object.assign(v.data, {
    columns: f.columns && f.columns.length ? f.columns : v.data.columns,
    rows: f.rows && f.rows.length ? f.rows : v.data.rows,
    row_count: f.row_count || v.data.row_count,
    chart: f.error_type ? null : (f.chart || v.data.chart),
    sql: f.sql || null,
    citations: f.citations || [],
  });
  if (f.error_type) { v.data.columns = []; v.data.rows = []; }
  (f.steps || []).forEach(s => setStep(v, s));
}

function stopOrSend() {
  if (!state.busy) return send(input.value, state.editing ? { replace: true } : {});
  const v = state.view;
  if (v && v.final) { v.shown = v.text.length; pump(v); return; }   // already answered: just finish typing
  if (state.abort) state.abort.abort();
}

/* editing the last message */
function startEdit(text) {
  if (state.busy) return;
  state.editing = true;
  $('#edit-banner').hidden = false;
  input.value = text; grow(); syncSend();
  input.focus();
  input.setSelectionRange(text.length, text.length);
}
function cancelEdit(keepText) {
  if (!state.editing) return;
  state.editing = false;
  $('#edit-banner').hidden = true;
  if (!keepText) { input.value = ''; grow(); syncSend(); }
}

/* ── sessions ─────────────────────────────────────────────────────── */
function setTitle(t) {
  $('#chat-title').textContent = t;
  document.title = t && t !== 'New chat' ? `${t} · EPMS AI` : 'EPMS AI Chat';
}
function setUrl(sid) {
  const url = new URL(location.href);
  url.search = sid ? `?c=${encodeURIComponent(sid)}` : '';
  history.replaceState(null, '', url);
}
function parseTs(ts) {
  // SQLite CURRENT_TIMESTAMP is UTC "YYYY-MM-DD HH:MM:SS"
  return new Date(ts.includes('T') ? ts : ts.replace(' ', 'T') + 'Z');
}
function groupOf(s) {
  if (s.pinned) return 'Pinned';
  const d = parseTs(s.updated_at);
  const day0 = new Date(); day0.setHours(0, 0, 0, 0);
  const days = Math.floor((day0 - new Date(d.getFullYear(), d.getMonth(), d.getDate())) / 864e5);
  if (days <= 0) return 'Today';
  if (days === 1) return 'Yesterday';
  if (days <= 7) return 'Previous 7 days';
  if (days <= 30) return 'Previous 30 days';
  return d.toLocaleDateString('en-GB', { month: 'long', year: 'numeric' });
}
async function loadSessions() {
  try {
    const res = await fetch('/chat/sessions');
    if (!res.ok) return;
    state.sessions = (await res.json()).sessions || [];
    renderSessions();
    const cur = state.sessions.find(s => s.id === state.sessionId);
    if (cur) setTitle(cur.title);
  } catch (e) { /* the sidebar is not critical */ }
}
function renderSessions() {
  const list = $('#session-list');
  const q = $('#chat-search').value.trim().toLowerCase();
  const items = state.sessions.filter(s => !q || s.title.toLowerCase().includes(q));
  if (!items.length) {
    list.innerHTML = `<div class="sb-empty">${q ? 'No chats match your search.' : 'Your chats will appear here.'}</div>`;
    return;
  }
  let html = '', group = null;
  for (const s of items) {
    const g = groupOf(s);
    if (g !== group) { html += `<div class="sb-group">${g}</div>`; group = g; }
    html += `<div class="sb-item${s.id === state.sessionId ? ' active' : ''}" data-id="${esc(s.id)}">
      <button type="button" class="sb-link" title="${esc(s.title)}">${esc(s.title)}</button>
      ${s.pinned ? `<span class="sb-pin">${ico('pin')}</span>` : ''}
      <button type="button" class="sb-more" aria-label="Chat options" aria-haspopup="menu">${ico('more')}</button>
    </div>`;
  }
  list.innerHTML = html;
}
$('#session-list').addEventListener('click', e => {
  const item = e.target.closest('.sb-item');
  if (!item) return;
  if (e.target.closest('.sb-more')) openSessionMenu(item, e.target.closest('.sb-more'));
  else if (e.target.closest('.sb-link')) openSession(item.dataset.id);
});
$('#chat-search').addEventListener('input', renderSessions);

function storedTurn(t) {
  const m = t.meta || {};
  return {
    answer: t.answer, sql: t.sql, columns: t.columns || [], rows: t.rows || [], row_count: t.row_count || 0,
    route: t.route, error_type: m.error_type || null, chart: m.chart || null, steps: m.steps || [],
    tables_used: m.tables || [], estates_carried: m.carried || [], database: m.database,
    provider_label: m.model, elapsed_ms: m.elapsed_ms || 0, turn_id: t.id, feedback: t.feedback,
    options: m.options || [], suggestions: m.suggestions || [], citations: [], parts: m.parts || [],
  };
}
async function openSession(sid, quiet) {
  if (state.busy) { toast('Wait for the answer to finish, or stop it first'); return; }
  closeDrawer();
  try {
    const res = await fetch('/chat/sessions/' + encodeURIComponent(sid));
    if (!res.ok) throw new Error();
    const data = await res.json();
    const turns = data.turns || [];
    if (!turns.length) throw new Error();
    destroyViews();
    thread.innerHTML = '';
    cancelEdit();
    state.sessionId = sid;
    for (const t of turns) {
      addUser(t.question).style.animation = 'none';
      const v = newView();
      v.el.style.animation = 'none';
      thread.appendChild(v.el);
      applyFinal(v, storedTurn(t));
      finish(v);
    }
    const last = turns[turns.length - 1];
    state.lastUser = last.question;
    state.lastSaved = true;
    const lastDb = (last.meta || {}).database;
    if (lastDb && state.dbs.some(d => d.key === lastDb && d.available) && lastDb !== state.database) {
      state.database = lastDb; store('chat-db', lastDb); renderChips();
    }
    const s = state.sessions.find(x => x.id === sid);
    setTitle(s ? s.title : turns[0].question);
    setUrl(sid);
    renderSessions();
    stick(true);
  } catch (e) {
    if (!quiet) toast('Could not open that chat');
    else newChat();
  }
}
function newChat() {
  if (state.busy) { toast('Wait for the answer to finish, or stop it first'); return; }
  state.sessionId = uuid();
  state.lastUser = null;
  state.lastSaved = false;
  cancelEdit();
  showWelcome();
  setUrl(null);
  renderSessions();
  closeDrawer();
  if (!isMobile()) input.focus();
}

/* per-chat menu: rename / pin / delete */
const menu = $('#menu');
function closeMenu() {
  menu.hidden = true;
  $$('.sb-item.menu-open').forEach(n => n.classList.remove('menu-open'));
}
function openSessionMenu(item, anchor) {
  const sid = item.dataset.id;
  const s = state.sessions.find(x => x.id === sid);
  if (!s) return;
  closeMenu();
  item.classList.add('menu-open');
  menu.innerHTML = `
    <button type="button" role="menuitem" data-act="rename">${ico('pencil')}<span>Rename</span></button>
    <button type="button" role="menuitem" data-act="pin">${ico('pin')}<span>${s.pinned ? 'Unpin' : 'Pin'}</span></button>
    <button type="button" role="menuitem" data-act="delete" class="danger">${ico('trash')}<span>Delete</span></button>`;
  menu.hidden = false;
  const r = anchor.getBoundingClientRect();
  menu.style.left = Math.min(r.left, innerWidth - menu.offsetWidth - 8) + 'px';
  menu.style.top = Math.min(r.bottom + 4, innerHeight - menu.offsetHeight - 8) + 'px';
  menu.onclick = async e => {
    const b = e.target.closest('button');
    if (!b) return;
    const act = b.dataset.act;
    if (act === 'rename') { closeMenu(); renameSession(item, s); return; }
    if (act === 'pin') {
      closeMenu();
      await patchSession(sid, { pinned: !s.pinned });
    } else if (act === 'delete') {
      if (b.dataset.sure !== '1') { b.dataset.sure = '1'; $('span', b).textContent = 'Click again to delete'; return; }
      closeMenu();
      try {
        await fetch('/chat/sessions/' + encodeURIComponent(sid), { method: 'DELETE' });
        if (sid === state.sessionId && !state.busy) newChat();
        loadSessions();
      } catch (err) { toast('Could not delete the chat'); }
    }
  };
  $('button', menu).focus();
}
async function patchSession(sid, body) {
  try {
    const res = await fetch('/chat/sessions/' + encodeURIComponent(sid), {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error();
    await loadSessions();
  } catch (e) { toast('Could not update the chat'); }
}
function renameSession(item, s) {
  const link = $('.sb-link', item);
  const box = el('input', 'sb-rename');
  box.value = s.title; box.maxLength = 60; box.setAttribute('aria-label', 'Chat name');
  link.replaceWith(box);
  box.focus(); box.select();
  let done = false;
  const commit = async save => {
    if (done) return; done = true;
    const title = box.value.trim();
    if (save && title && title !== s.title) await patchSession(s.id, { title });
    else renderSessions();
  };
  box.addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); commit(true); }
    else if (e.key === 'Escape') { e.stopPropagation(); commit(false); }
  });
  box.addEventListener('blur', () => commit(true));
}

/* ── tools popover: database / answer from / model ────────────────── */
const pop = $('#tools-pop');
function optHtml(group, key, label, desc, on, disabled) {
  return `<button type="button" class="opt${on ? ' on' : ''}" role="menuitemradio" aria-checked="${on}" data-group="${group}" data-key="${esc(key)}"${disabled ? ' disabled' : ''}>
    <span class="opt-main"><span class="opt-label">${esc(label)}</span>${desc ? `<span class="opt-desc">${esc(desc)}</span>` : ''}</span>
    <span class="tickmark">${ico('check')}</span></button>`;
}
function renderTools() {
  pop.innerHTML = `
    <div class="pop-title">Database</div>
    ${state.dbs.map(d => optHtml('database', d.key, d.label, d.available ? d.description : 'Not configured on this server', d.key === state.database, !d.available)).join('')}
    <div class="pop-sep"></div>
    <div class="pop-title">Answer from</div>
    ${SOURCES.map(s => optHtml('source', s.key, s.label, s.desc, s.key === state.source)).join('')}
    <div class="pop-sep"></div>
    <div class="pop-title">Model</div>
    ${optHtml('model', '', 'Automatic', 'Fast model, stepping up for analysis', !state.model)}
    ${state.models.map(m => optHtml('model', m.key, m.label, '', m.key === state.model)).join('')}`;
}
function toggleTools(open) {
  const show = open === undefined ? pop.hidden : open;
  if (show) renderTools();
  pop.hidden = !show;
  $('#tools-btn').setAttribute('aria-expanded', String(show));
}
pop.addEventListener('click', e => {
  const b = e.target.closest('.opt');
  if (!b || b.disabled) return;
  const { group, key } = b.dataset;
  if (group === 'database' && key !== state.database) {
    state.database = key; store('chat-db', key);
    if ($('#welcome')) showWelcome(); else toast(`Next questions go to ${db().label}`);
  } else if (group === 'source') { state.source = key; store('chat-source', key); }
  else if (group === 'model') { state.model = key; store('chat-model-v2', key); }
  renderChips();
  toggleTools(false);
  input.focus();
});
function renderChips() {
  const chips = [`<button type="button" class="chip" title="Database for the next question">${ico('database')}<span>${esc(db().label)}</span></button>`];
  const src = SOURCES.find(s => s.key === state.source);
  if (src && src.key !== 'sql') chips.push(`<button type="button" class="chip" title="Answer from">${ico(src.icon)}<span>${esc(src.label)}</span></button>`);
  const m = state.models.find(x => x.key === state.model);
  if (m) chips.push(`<button type="button" class="chip" title="Model">${ico('cpu')}<span>${esc(m.label)}</span></button>`);
  $('#ctx-chips').innerHTML = chips.join('');
}
$('#ctx-chips').addEventListener('click', e => { if (e.target.closest('.chip')) { e.stopPropagation(); toggleTools(true); } });
$('#tools-btn').addEventListener('click', e => { e.stopPropagation(); toggleTools(); });

async function loadSettings() {
  const [dbs, models] = await Promise.all([
    fetch('/chat/databases').then(r => r.json()).catch(() => null),
    fetch('/chat/models').then(r => r.json()).catch(() => null),
  ]);
  if (dbs) {
    state.dbs = dbs.databases;
    if (!state.dbs.some(d => d.key === state.database && d.available)) state.database = dbs.default;
  }
  if (models) {
    state.models = models.models;
    models.models.forEach(m => { state.modelLabels[m.id] = m.label; });
    if (state.model && !state.models.some(m => m.key === state.model)) state.model = '';
  }
  if (!SOURCES.some(s => s.key === state.source)) state.source = 'sql';
  renderChips();
}

/* ── theme + sidebar ──────────────────────────────────────────────── */
function paintThemeBtn() { $('#theme-btn').innerHTML = ico(root.dataset.theme === 'dark' ? 'sun' : 'moon'); }
$('#theme-btn').addEventListener('click', () => {
  root.dataset.theme = root.dataset.theme === 'dark' ? 'light' : 'dark';
  store('chat-theme', root.dataset.theme);
  paintThemeBtn();
  state.views.forEach(v => { if (v.chartObj) renderResult(v); });     // charts read their colours from the theme
});
function closeDrawer() { root.classList.remove('sb-mobile-open'); }
function toggleSidebar() {
  if (isMobile()) { root.classList.toggle('sb-mobile-open'); return; }
  const closed = root.classList.toggle('sb-closed');
  store('chat-sidebar', closed ? 'closed' : 'open');
  setTimeout(() => state.views.forEach(v => v.chartObj && v.chartObj.resize()), 280);
}
$('#sb-open').addEventListener('click', toggleSidebar);
$('#sb-close').addEventListener('click', toggleSidebar);
$('#scrim').addEventListener('click', closeDrawer);
$('#new-chat').addEventListener('click', newChat);
$('.brand').addEventListener('click', e => { e.preventDefault(); newChat(); });

/* ── composer + keyboard ──────────────────────────────────────────── */
$('#composer').addEventListener('submit', e => { e.preventDefault(); stopOrSend(); });
$('#edit-cancel').addEventListener('click', () => cancelEdit());
input.addEventListener('input', () => { grow(); syncSend(); });
input.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    if (!state.busy) stopOrSend();
  } else if (e.key === 'ArrowUp' && !input.value && state.lastUser && !state.busy) {
    e.preventDefault();
    startEdit(state.lastUser);
  }
});
document.addEventListener('click', e => {
  if (!pop.hidden && !e.target.closest('#tools-pop')) toggleTools(false);
  if (!menu.hidden && !e.target.closest('#menu') && !e.target.closest('.sb-more')) closeMenu();
});
document.addEventListener('keydown', e => {
  const typing = /^(INPUT|TEXTAREA)$/.test(document.activeElement.tagName);
  const mod = e.ctrlKey || e.metaKey;
  if (e.key === 'Escape') {
    if (!menu.hidden) closeMenu();
    else if (!pop.hidden) toggleTools(false);
    else if (state.editing) cancelEdit();
    else if (state.busy) stopOrSend();
    else closeDrawer();
  } else if (mod && e.key.toLowerCase() === 'k') {
    e.preventDefault();
    if (root.classList.contains('sb-closed') || isMobile()) toggleSidebar();
    $('#chat-search').focus();
  } else if (mod && e.shiftKey && e.key.toLowerCase() === 'o') {
    e.preventDefault(); newChat();
  } else if (e.key === '/' && !typing && !mod) {
    e.preventDefault(); input.focus();
  }
});
window.addEventListener('resize', () => stick());

/* ── boot ─────────────────────────────────────────────────────────── */
(async function boot() {
  $$('[data-ico]').forEach(n => { n.innerHTML = ico(n.dataset.ico) + n.innerHTML; });
  paintThemeBtn();
  setBusy(false);
  showWelcome();
  const params = new URLSearchParams(location.search);
  await Promise.all([loadSettings(), loadSessions()]);
  if (params.get('db') && state.dbs.some(d => d.key === params.get('db') && d.available)) {
    state.database = params.get('db'); renderChips();
  }
  if (params.get('c')) await openSession(params.get('c'), true);
  else showWelcome();                       // again, now that the database's starter questions are known
  if (params.get('q')) send(params.get('q'));    // /chat?q=... asks straight away
  else if (!isMobile()) input.focus();
})();
