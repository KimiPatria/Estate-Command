/* The workbench: where analysis happens next to the map rather than on top
 * of it.
 *
 * This replaces the centred modal. A modal was the wrong shape for this app:
 * every panel here talks about blocks, and a panel that says "13 blocks weak
 * on both" while covering the map is asking the reader to hold thirteen
 * labels in their head. Docked to the right, the same sentence is a caption
 * for something visible.
 *
 * The map is not reflowed to make room. It keeps its full canvas and takes
 * camera padding instead, which is the trick the block drawer already used:
 * imagery never jumps, and the subject slides into the clear half.
 */
import { map } from '../map/instance.js';
import { easeMapPadding } from '../map/camera.js';
import { esc } from '../lib/fmt.js';
import { clearBlockRefs, wireBlockRefs } from '../panels/_shared.js';

const WIDTH_KEY = 'ec.wb.width.v1';
const MIN = 380, MAX = 720, DEFAULT = 480;

export const WB = {
  tabs: [],        // [{ id, kind, key, label, question, render }]
  active: null,
  width: DEFAULT,
  open: false,
};

function el(id) { return document.getElementById(id); }

/* ── width, remembered ─────────────────────────────────────────────────── */

function loadWidth() {
  try {
    const n = parseInt(localStorage.getItem(WIDTH_KEY), 10);
    if (n >= MIN && n <= MAX) WB.width = n;
  } catch (e) { /* a private window must not cost us the dock */ }
}

function saveWidth() {
  try { localStorage.setItem(WIDTH_KEY, String(WB.width)); } catch (e) { /* as above */ }
}

function applyWidth() {
  document.documentElement.style.setProperty('--wb', WB.width + 'px');
  if (WB.open) easeMapPadding(WB.width);
}

/* ── open and close ────────────────────────────────────────────────────── */

export function openWorkbench() {
  if (WB.open) return;
  WB.open = true;
  const w = el('wb');
  w.hidden = false;
  void w.offsetWidth;           // settle the offscreen start before animating
  w.classList.add('open');
  easeMapPadding(WB.width);
}

export function closeWorkbench() {
  if (!WB.open) return;
  WB.open = false;
  clearBlockRefs();
  el('wb').classList.remove('open');
  easeMapPadding(0);
  // Let the slide finish before it leaves the accessibility tree.
  setTimeout(() => { if (!WB.open) el('wb').hidden = true; }, 440);
}

export function workbenchIsOpen() { return WB.open; }

/* ── tabs ──────────────────────────────────────────────────────────────── */

/* A tab is identified by kind+key so re-opening the same panel focuses the
   tab that already holds it instead of stacking duplicates. */
export function addTab({ kind, key, label, question, render, after }) {
  const id = `${kind}:${key}`;
  let tab = WB.tabs.find(t => t.id === id);
  if (tab) {
    Object.assign(tab, { label, question, render, after });
  } else {
    tab = { id, kind, key, label, question, render, after };
    WB.tabs.push(tab);
  }
  WB.active = id;
  openWorkbench();
  renderTabs();
  return paint(tab);
}

export function closeTab(id) {
  const i = WB.tabs.findIndex(t => t.id === id);
  if (i < 0) return;
  WB.tabs.splice(i, 1);
  if (WB.active === id) {
    const next = WB.tabs[i] || WB.tabs[i - 1];
    WB.active = next ? next.id : null;
  }
  renderTabs();
  if (!WB.tabs.length) { closeWorkbench(); return; }
  const tab = WB.tabs.find(t => t.id === WB.active);
  if (tab) paint(tab);
}

export function switchTab(id) {
  const tab = WB.tabs.find(t => t.id === id);
  if (!tab || WB.active === id) return;
  WB.active = id;
  renderTabs();
  paint(tab);
}

/* The rail mirrors what is open. Registered rather than imported, because
   workbench.js must not depend on the panel registry that depends on it. */
let onChange = () => {};
export function onWorkbenchChange(fn) { onChange = fn; }

function renderTabs() {
  const host = el('wb-tabs');
  host.innerHTML = WB.tabs.map(t => `
    <button class="wb-tab ${t.id === WB.active ? 'on' : ''}" data-tab="${esc(t.id)}"
            title="${esc(t.label)}">
      <span>${esc(t.label)}</span>
      <span class="x" data-close="${esc(t.id)}" role="button" aria-label="Close">&times;</span>
    </button>`).join('');
  host.querySelectorAll('[data-tab]').forEach(b => {
    b.onclick = e => {
      if (e.target.closest('[data-close]')) { closeTab(b.dataset.tab); return; }
      switchTab(b.dataset.tab);
    };
  });
  // The strip only earns its height once there is a choice to make.
  host.hidden = WB.tabs.length < 2;
  onChange();
}

/* ── painting the active tab ───────────────────────────────────────────── */

async function paint(tab) {
  el('wb-title').textContent = tab.label;
  el('wb-sub').textContent = tab.question || '';
  const body = el('wb-body');
  body.dataset.view = tab.key;
  body.scrollTop = 0;
  body.innerHTML = '<div class="empty">Loading…</div>';
  try {
    const html = await tab.render();
    // A slower tab must not overwrite whatever the user switched to.
    if (WB.active !== tab.id) return;
    body.innerHTML = html;
    // Every panel that names blocks gets them painted, hoverable and clickable
    // without opting in. This is the whole point of docking beside the map.
    wireBlockRefs(body);
    // Panels that own live controls wire them here, once their markup is in
    // the document. Doing it from inside the renderer races the assignment.
    if (tab.after) tab.after(body);
  } catch (err) {
    console.error('panel render failed', tab.id, err);
    if (WB.active !== tab.id) return;
    body.innerHTML = `<div class="sheet-note">This panel failed to render.
      The map and every other panel are unaffected.</div>`;
  }
  return body;
}

/* Re-run the active tab's renderer, for when the estate or month changes. */
export function refreshActiveTab() {
  const tab = WB.tabs.find(t => t.id === WB.active);
  if (tab && WB.open) return paint(tab);
}

/* ── the drag handle ───────────────────────────────────────────────────── */

function wireGrip() {
  const grip = el('wb-grip');
  let startX = 0, startW = 0, dragging = false;

  const move = e => {
    if (!dragging) return;
    // The dock is on the right, so dragging left widens it.
    WB.width = Math.max(MIN, Math.min(MAX, startW + (startX - e.clientX)));
    applyWidth();
  };
  const up = () => {
    if (!dragging) return;
    dragging = false;
    document.body.classList.remove('wb-dragging');
    saveWidth();
    if (map) map.resize();
    window.removeEventListener('pointermove', move);
    window.removeEventListener('pointerup', up);
  };

  grip.addEventListener('pointerdown', e => {
    dragging = true;
    startX = e.clientX;
    startW = WB.width;
    document.body.classList.add('wb-dragging');
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up);
    e.preventDefault();
  });

  // Keyboard resize, because a drag handle alone is not reachable.
  grip.addEventListener('keydown', e => {
    const step = e.shiftKey ? 80 : 20;
    if (e.key === 'ArrowLeft')  { WB.width = Math.min(MAX, WB.width + step); }
    else if (e.key === 'ArrowRight') { WB.width = Math.max(MIN, WB.width - step); }
    else return;
    e.preventDefault();
    applyWidth();
    saveWidth();
  });
}

export function initWorkbench() {
  loadWidth();
  applyWidth();
  wireGrip();
  el('wb-x').onclick = closeWorkbench;
}
