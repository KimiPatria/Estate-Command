/* The selection tray: what you can do with a set once you have one.
 *
 * This is the other half of the missing verb. Six approval documents already
 * exist server-side in gis/decisions.py - harvesting plan, work order, goods
 * issue, purchase requisition, fire mobilisation, field inspection - and each
 * one was reachable only as a button at the bottom of a single panel, against
 * a block list the panel chose. `/gis/artifact-kinds` was never called by the
 * UI at all.
 *
 * Drafting against a set the user assembled themselves is the difference
 * between reading a recommendation and making a decision.
 */
import { getJSON } from '../lib/api.js';
import { esc, fmt } from '../lib/fmt.js';
import { highlightSet } from '../map/highlight.js';
import {
  clearSelection, onSelectionChange, selectionArea, selectionIds,
  selectionRows, selectionSize,
} from '../state/selection.js';
import { S } from '../state/store.js';
import { closePop, showPop } from './pop.js';
import { addTab } from './workbench.js';

/* Which of the six documents a set of blocks can actually produce. The other
   three take vendors, materials or a fire, not a block list. */
const SET_KINDS = ['harvesting_plan', 'work_order', 'inspection_order'];

let KINDS = null;   // from /gis/artifact-kinds, fetched once

function el(id) { return document.getElementById(id); }

export function renderTray() {
  const n = selectionSize();
  const tray = el('tray');
  if (!n) { tray.hidden = true; return; }
  tray.hidden = false;
  el('tray-n').textContent = `${n} block${n === 1 ? '' : 's'}`;
  el('tray-ha').textContent = `${fmt(selectionArea(), 1)} ha`;
  // Inspection orders are per block; the rest take the whole set.
  el('tray-draft').textContent = n === 1 ? 'Draft' : 'Draft for all';
}

/* ── the draft menu ────────────────────────────────────────────────────── */

async function kinds() {
  if (KINDS) return KINDS;
  const d = await getJSON('/gis/artifact-kinds');
  KINDS = d.kinds || d;
  return KINDS;
}

function draftMenu(all, n) {
  const rows = SET_KINDS
    .filter(k => all[k] && (k !== 'inspection_order' || n === 1))
    .map(k => {
      const m = all[k];
      return `<button class="draft-row" data-kind="${esc(k)}">
        <span class="t">${esc(m.label)}</span>
        <span class="s">approved by ${esc(m.approver)}</span>
        <span class="r">${esc(m.epms_table)}</span>
      </button>`;
    }).join('');
  return `<div class="draft-menu">
    <div class="mm-h">Draft against ${n} block${n === 1 ? '' : 's'}</div>
    ${rows}
  </div>`;
}

/* Work orders need an activity; the rest are determined by the kind alone. */
const ACTIVITY = 'circle weeding';

async function draft(kind) {
  const rows = selectionRows();
  const meta = (await kinds())[kind] || {};
  const payload = kind === 'work_order'
    ? { activity: ACTIVITY, blocks: rows.map(r => ({ ...r, activity: ACTIVITY })) }
    : kind === 'inspection_order'
      ? { ...rows[0], reason: 'Selected on the map for inspection' }
      : { date: 'next round', blocks: rows };

  const body = {
    estate: S.estate,
    use_case: 'selection',
    title: `${meta.label || kind}, ${rows.length} block${rows.length === 1 ? '' : 's'}`,
    subject: rows.map(r => r.block_label).join(', ').slice(0, 180),
    action: 'accepted',
    // The evidence is the set itself: what was chosen, and how much of it.
    evidence: [
      `${rows.length} blocks selected on the map`,
      `${fmt(selectionArea(), 1)} ha planted`,
      ...rows.slice(0, 8).map(r => `Block ${r.block_label}, division ${r.division_code}`),
    ],
    artifact_kind: kind,
    artifact_payload: payload,
  };

  const res = await fetch('/gis/decisions', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`draft failed: ${res.status}`);
  return res.json();
}

/* ── actions ───────────────────────────────────────────────────────────── */

async function openDraft(kind) {
  const meta = (await kinds())[kind] || {};
  addTab({
    kind: 'draft', key: `${kind}:${Date.now()}`,
    label: meta.label || kind,
    question: `Drafted against ${selectionSize()} selected block(s)`,
    render: async () => {
      const d = await draft(kind);
      const { renderArtifact } = await import('../panels/decisions.js');
      return (d.artifact ? renderArtifact(d.artifact) : '')
        + `<div class="sheet-note"><b>Recorded</b> in the decision log.</div>`;
    },
  });
}

function openBrief() {
  const rows = selectionRows();
  addTab({
    kind: 'set', key: 'brief',
    label: `${rows.length} selected`,
    question: 'What is in the working set',
    render: async () => `
      <div class="kpis">
        <div class="kpi"><b>${rows.length}</b><span>blocks</span></div>
        <div class="kpi"><b>${fmt(selectionArea(), 1)}</b><span>ha planted</span></div>
        <div class="kpi"><b>${fmt(rows.reduce((s, r) => s + (r.palms || 0), 0))}</b>
          <span>palms</span></div>
        <div class="kpi"><b>${new Set(rows.map(r => r.division_code)).size}</b>
          <span>divisions</span></div>
      </div>
      <table class="tbl">
        <tr><th>Block</th><th>Div</th><th class="num">Planted ha</th>
            <th class="num">Palms</th><th class="num">Year</th></tr>
        ${rows.map(r => `<tr>
          <td>${esc(r.block_label)}</td><td>${esc(r.division_code)}</td>
          <td class="num">${fmt(r.planted_ha, 2)}</td>
          <td class="num">${fmt(r.palms)}</td>
          <td class="num">${r.planted_year ?? '—'}</td></tr>`).join('')}
      </table>`,
  });
}

/* ── wiring ────────────────────────────────────────────────────────────── */

export function initTray() {
  const pop = document.createElement('div');
  pop.className = 'pop';
  pop.hidden = true;
  document.body.appendChild(pop);

  el('tray-clear').onclick = clearSelection;
  el('tray-brief').onclick = openBrief;
  el('tray-draft').onclick = async () => {
    const all = await kinds();
    showPop(pop, el('tray-draft'), draftMenu(all, selectionSize()), root => {
      root.querySelectorAll('[data-kind]').forEach(b => {
        b.onclick = () => { closePop(); openDraft(b.dataset.kind); };
      });
    });
  };

  // The map follows the set, and the tray follows its size.
  onSelectionChange(ids => { highlightSet(ids); renderTray(); });
  renderTray();
}

export { selectionIds };
