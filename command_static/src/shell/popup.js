/* The feature window: one large popup per operational feature.
 *
 * The workbench dock is the right shape for a panel that makes one point
 * about some blocks. It is the wrong shape for a feature a person works in:
 * the operation menus carry a plan, a ledger and the arithmetic behind both,
 * and stacked in a 480px column they became one long scroll where nothing
 * could be found twice.
 *
 * So these features open here instead. A menu down the left lists every
 * section, one section fills the window at a time, and the window remembers
 * where it was. The map is not lost: anything that names blocks carries a
 * Show-on-map action, which outlines them on the full map and shrinks the
 * window to a bar at the foot of the screen; one click puts it back exactly
 * as it was.
 *
 * This module is the shell only. A feature hands it a definition:
 *
 *   {
 *     key, title, subtitle,
 *     ml:       true to mark the title as machine learning (optional)
 *     initial:  section id to open on
 *     load():   fetch what the window needs; may throw
 *     loadingText: what to say while load() runs (optional)
 *     header(): html for the strip beside the title (optional)
 *     wireHeader(el, api)
 *     menu():   [{ label, items: [{ id, label, count, alert }] }]
 *     section(id): { title, html, blocks: [labels], blocksCaption }; may be async
 *     wire(contentEl, api, sectionId)
 *     onSection(id)
 *     needs():  html for the "What this needs" section (optional)
 *   }
 *
 * and gets back an api: rerender(), go(id), reload(), showOnMap(labels, caption).
 * Every figure still comes from the server; the shell never computes one.
 */
import { esc } from '../lib/fmt.js';
import { ML_ICON } from '../lib/ml.js';
import { blockFeature, blockIdsFor } from '../map/blocks.js';
import { currentRightPad } from '../map/camera.js';
import { clearPanelHighlight, highlightPanel } from '../map/highlight.js';
import { map } from '../map/instance.js';

const P = {
  def: null,
  section: null,
  state: 'closed',        // open | min | closed
  ownsHighlight: false,
  token: 0,
  returnFocus: null,
  mapCaption: '',
};

let onChange = () => {};
export function onPopupChange(fn) { onChange = fn; }

export function popupIsOpen() { return P.state === 'open'; }
export function popupKey() { return P.state === 'closed' || !P.def ? null : P.def.key; }

function el(id) { return document.getElementById(id); }

const PIN = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"
  stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
  <path d="M12 21s-7-6.2-7-11.5a7 7 0 0 1 14 0C19 14.8 12 21 12 21z"/><circle cx="12" cy="9.5" r="2.4"/></svg>`;
export const MAP_ICON = PIN;

/* ── the DOM, built once ───────────────────────────────────────────────── */

function ensureDom() {
  if (el('fp-bg')) return;
  const host = document.createElement('div');
  host.innerHTML = `
  <div class="fp-bg" id="fp-bg" hidden>
    <div class="fp" role="dialog" aria-modal="true" aria-labelledby="fp-title">
      <header class="fp-h">
        <div class="fp-h-t">
          <h2 id="fp-title"></h2>
          <p id="fp-sub"></p>
        </div>
        <div class="fp-extra" id="fp-extra"></div>
        <button class="fp-hbtn" id="fp-min" title="Show the map (the window waits at the foot of the screen)">
          ${PIN}<span>Map</span></button>
        <button class="x" id="fp-x" aria-label="Close" title="Close (Esc)">&times;</button>
      </header>
      <div class="fp-main">
        <nav class="fp-nav" id="fp-nav" aria-label="Sections"></nav>
        <section class="fp-content" id="fp-content" tabindex="-1"></section>
      </div>
    </div>
  </div>
  <div class="fp-bar" id="fp-bar" hidden role="region" aria-label="Minimised window">
    <span class="fp-bar-dot"></span>
    <span class="fp-bar-t"><b id="fp-bar-title"></b><span id="fp-bar-sec"></span></span>
    <span class="fp-bar-cap" id="fp-bar-cap"></span>
    <button class="fp-bar-back" id="fp-restore">Back to the window</button>
    <button class="x" id="fp-bar-x" aria-label="Close window">&times;</button>
  </div>`;
  document.body.append(...host.children);

  el('fp-x').onclick = closePopup;
  el('fp-bar-x').onclick = closePopup;
  el('fp-restore').onclick = restorePopup;
  el('fp-min').onclick = () => minimisePopup('Map view');

  // A backdrop click closes, but only when the press started there too: a
  // text selection dragged out of a table must not throw the window away.
  let downOnBg = false;
  el('fp-bg').addEventListener('mousedown', e => { downOnBg = e.target === el('fp-bg'); });
  el('fp-bg').addEventListener('click', e => {
    if (downOnBg && e.target === el('fp-bg')) closePopup();
    downOnBg = false;
  });

  el('fp-nav').addEventListener('click', e => {
    const b = e.target.closest('[data-sec]');
    if (b) go(b.dataset.sec);
  });
  el('fp-nav').addEventListener('keydown', e => {
    if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
    const items = [...el('fp-nav').querySelectorAll('[data-sec]')];
    const i = items.indexOf(document.activeElement);
    if (i < 0) return;
    e.preventDefault();
    const next = items[(i + (e.key === 'ArrowDown' ? 1 : items.length - 1)) % items.length];
    next.focus();
    go(next.dataset.sec);
  });
}

/* ── open, minimise, restore, close ────────────────────────────────────── */

export async function openPopup(def) {
  ensureDom();
  // The same feature again: bring it back rather than rebuilding it.
  if (P.def && P.def.key === def.key && P.state !== 'closed') {
    if (P.state === 'min') restorePopup();
    return;
  }
  if (P.state !== 'closed') closePopup({ quiet: true });

  P.def = def;
  P.section = def.initial;
  P.state = 'open';
  P.returnFocus = document.activeElement;
  el('fp-bar').hidden = true;
  el('fp-bg').hidden = false;
  document.body.classList.add('fp-open');
  renderHeader();
  el('fp-nav').innerHTML = '';
  el('fp-content').innerHTML = `<div class="empty">${esc(def.loadingText || 'Loading…')}</div>`;
  onChange();

  const token = ++P.token;
  try {
    await def.load();
  } catch (err) {
    if (token !== P.token || P.def !== def) return;
    console.error('popup load failed', def.key, err);
    el('fp-content').innerHTML = `<div class="sheet-note">This window failed to load: ${esc(err.message)}.
      The map and every other panel are unaffected.</div>`;
    return;
  }
  if (P.def !== def || P.state === 'closed') return;
  renderHeader();
  await renderSection();
  el('fp-x').focus({ preventScroll: true });
}

export function minimisePopup(caption) {
  if (P.state !== 'open') return;
  P.state = 'min';
  P.mapCaption = caption || '';
  el('fp-bg').hidden = true;
  document.body.classList.remove('fp-open');
  el('fp-bar-title').textContent = P.def.title || '';
  el('fp-bar-sec').textContent = sectionLabel() ? ` · ${sectionLabel()}` : '';
  el('fp-bar-cap').textContent = P.mapCaption;
  el('fp-bar').hidden = false;
  el('fp-restore').focus({ preventScroll: true });
  onChange();
}

export function restorePopup() {
  if (P.state !== 'min') return;
  P.state = 'open';
  el('fp-bar').hidden = true;
  el('fp-bg').hidden = false;
  document.body.classList.add('fp-open');
  el('fp-content').focus({ preventScroll: true });
  onChange();
}

export function closePopup(opts = {}) {
  if (P.state === 'closed') return;
  const def = P.def;
  P.state = 'closed';
  P.token++;
  el('fp-bg').hidden = true;
  el('fp-bar').hidden = true;
  document.body.classList.remove('fp-open');
  if (P.ownsHighlight) {
    clearPanelHighlight();
    restoreDockHighlight();
    P.ownsHighlight = false;
  }
  if (def && def.onClose) def.onClose();
  P.def = null;
  if (!opts.quiet) {
    const f = P.returnFocus;
    if (f && document.contains(f) && f.focus) f.focus({ preventScroll: true });
    onChange();
  }
}

/* The dock may be showing a panel whose blocks were outlined before the
   window borrowed the highlight layer. Give them back. */
function restoreDockHighlight() {
  const wb = el('wb'), body = el('wb-body');
  if (!wb || wb.hidden || !body) return;
  const ids = [...new Set([...body.querySelectorAll('[data-block]')].map(n => n.dataset.block))];
  if (ids.length) highlightPanel(ids);
}

/* ── the map ───────────────────────────────────────────────────────────── */

function frame(ids) {
  if (!map) return;
  let [w, s, e, n] = [180, 90, -180, -90];
  const walk = c => {
    if (Array.isArray(c[0])) { c.forEach(walk); return; }
    const [x, y] = c;
    if (x < w) w = x; if (x > e) e = x;
    if (y < s) s = y; if (y > n) n = y;
  };
  ids.forEach(id => { const f = blockFeature(id); if (f) walk(f.geometry.coordinates); });
  if (w > e || s > n) return;
  const fly = parseInt(getComputedStyle(document.documentElement).getPropertyValue('--fly'), 10) || 0;
  try {
    map.fitBounds([[w, s], [e, n]], {
      padding: { top: 80, bottom: 150, left: 80 + fly, right: 80 + currentRightPad() },
      maxZoom: 15.4, duration: 800,
    });
  } catch (err) { /* framing is a nicety; the outline is already drawn */ }
}

export function showOnMap(labels, caption) {
  const ids = blockIdsFor(labels || []);
  if (!ids.length) {
    flashNote('None of these blocks are on the map for this estate.');
    return;
  }
  highlightPanel(ids);
  P.ownsHighlight = true;
  frame(ids);
  const what = `${ids.length} block${ids.length === 1 ? '' : 's'} outlined`;
  minimisePopup(caption ? `${caption}: ${what}` : what);
}

function flashNote(text) {
  const c = el('fp-content');
  let n = c.querySelector('.fp-flash');
  if (!n) {
    n = document.createElement('div');
    n.className = 'fp-flash';
    c.prepend(n);
  }
  n.textContent = text;
  setTimeout(() => n.remove(), 3200);
}

/* ── rendering ─────────────────────────────────────────────────────────── */

const api = {
  rerender: () => { renderHeader(); return renderSection({ keepScroll: true }); },
  // Header and menu only, leaving the section's DOM (and whatever has focus
  // in it) untouched.
  refreshMenu: () => { renderHeader(); renderNav(); },
  go: id => go(id),
  reload: () => reload(),
  showOnMap: (labels, caption) => showOnMap(labels, caption),
  section: () => P.section,
};

function go(id) {
  if (!P.def || id === P.section) return;
  P.section = id;
  if (P.def.onSection) P.def.onSection(id);
  renderSection();
}

async function reload() {
  const def = P.def;
  if (!def) return;
  el('fp-content').innerHTML = '<div class="empty">Loading…</div>';
  const token = ++P.token;
  try {
    await def.load();
  } catch (err) {
    if (token === P.token) {
      el('fp-content').innerHTML = `<div class="sheet-note">Reload failed: ${esc(err.message)}</div>`;
    }
    return;
  }
  if (P.def !== def) return;
  renderHeader();
  await renderSection();
}

function menu() {
  let groups = [];
  try { groups = P.def.menu() || []; } catch (e) { console.error('popup menu', e); }
  if (P.def.needs) {
    groups = [...groups, { label: 'About', items: [{ id: '__needs', label: 'What this needs' }] }];
  }
  return groups;
}

function sectionLabel() {
  for (const g of menu()) {
    const hit = g.items.find(i => i.id === P.section);
    if (hit) return hit.label;
  }
  return '';
}

function renderHeader() {
  const def = P.def;
  if (!def) return;
  el('fp-title').innerHTML = esc(typeof def.title === 'function' ? def.title() : (def.title || ''))
    + (def.ml ? ML_ICON : '');
  el('fp-sub').textContent = typeof def.subtitle === 'function' ? def.subtitle() : (def.subtitle || '');
  const extra = el('fp-extra');
  let html = '';
  try { html = def.header ? def.header() : ''; } catch (e) { html = ''; }
  extra.innerHTML = html;
  if (def.wireHeader) def.wireHeader(extra, api);
}

function renderNav() {
  const groups = menu();
  // A section that no longer exists (a variant switched) falls back to the
  // first one rather than rendering nothing.
  const all = groups.flatMap(g => g.items.map(i => i.id));
  if (all.length && !all.includes(P.section)) P.section = all[0];
  el('fp-nav').innerHTML = groups.map(g => `
    <div class="fp-nav-g">
      <div class="fp-nav-l">${esc(g.label)}</div>
      ${g.items.map(i => `<button class="fp-nav-i ${i.id === P.section ? 'on' : ''}" data-sec="${esc(i.id)}"
          ${i.id === P.section ? 'aria-current="true"' : ''}>
        <span class="t">${esc(i.label)}</span>
        ${i.count !== undefined && i.count !== null && i.count !== ''
          ? `<span class="n ${i.alert ? 'warn' : ''}">${esc(i.count)}</span>` : ''}
      </button>`).join('')}
    </div>`).join('');
}

async function renderSection({ keepScroll = false } = {}) {
  const def = P.def;
  if (!def) return;
  renderNav();
  const content = el('fp-content');
  const top = keepScroll ? content.scrollTop : 0;
  const token = ++P.token;
  const id = P.section;

  let sec;
  // A section that fetches its own data says so if it takes a moment,
  // rather than leaving the last section on screen under the new menu item.
  const slow = setTimeout(() => {
    if (token === P.token && P.def === def && P.section === id) {
      content.innerHTML = '<div class="empty">Loading…</div>';
    }
  }, 160);
  try {
    sec = id === '__needs'
      ? { title: 'What this needs',
          html: await def.needs() || '<div class="empty">Every input here is already yours.</div>' }
      : await def.section(id);
  } catch (err) {
    console.error('popup section failed', def.key, id, err);
    sec = { title: sectionLabel(), html: `<div class="sheet-note">This section failed to render.</div>` };
  }
  clearTimeout(slow);
  if (token !== P.token || P.def !== def || P.section !== id) return;

  const blocks = (sec.blocks || []).filter(Boolean);
  content.innerHTML = `
    <div class="fp-sec-h">
      <div class="fp-sec-t">
        <h3>${esc(sec.title || sectionLabel())}</h3>
      </div>
      ${blocks.length ? `<button class="fp-map-all" data-map="${esc(blocks.join(','))}"
          data-map-caption="${esc(sec.blocksCaption || sec.title || '')}">
          ${PIN}<span>Show ${blocks.length} on map</span></button>` : ''}
    </div>
    <div class="fp-sec-b">${sec.html || ''}</div>`;
  content.scrollTop = top;
  wireMapRefs(content);
  if (def.wire) def.wire(content, api, id);
}

/* Anything carrying data-map names blocks. A button shows them; a table row
   shows them when the row itself is clicked, but not when the click was on a
   control inside it. */
function wireMapRefs(root) {
  root.querySelectorAll('[data-map]').forEach(node => {
    const labels = node.dataset.map.split(',').filter(Boolean);
    const caption = node.dataset.mapCaption || '';
    if (node.tagName === 'TR') {
      node.classList.add('fp-row-map');
      node.title = 'Click to show on the map';
      node.addEventListener('click', ev => {
        if (ev.target.closest('button, a, input, select, label, textarea')) return;
        showOnMap(labels, caption);
      });
    } else {
      node.addEventListener('click', ev => { ev.stopPropagation(); showOnMap(labels, caption); });
    }
  });
}
