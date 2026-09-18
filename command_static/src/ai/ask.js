import maplibregl from 'maplibre-gl';
import { map } from '../map/instance.js';
import { AI, activeChat, auditChip, deleteChat, genBadge, saveChats, switchChat } from './store.js';
import { getJSON } from '../lib/api.js';
import { esc, row } from '../lib/fmt.js';
import { setMetric } from '../map/choropleth.js';
import { PANELS, openPanel } from '../panels/registry.js';
import { renderMonths } from '../shell/timeline.js';
import { S } from '../state/store.js';
import { replaceSelection } from '../state/selection.js';

/* ── ask the map ────────────────────────────────────────────────────── */

export function askEl() { return document.getElementById('ask'); }

export function toggleAsk(force) {
  AI.open = force === undefined ? !AI.open : force;
  askEl().hidden = !AI.open;
  document.getElementById('ask-launch').hidden = AI.open;
  if (AI.open) {
    renderAsk();
    const input = document.getElementById('ask-input');
    if (input) input.focus();
  }
}

export function renderAskTabs() {
  const tabs = document.getElementById('ask-tabs');
  tabs.innerHTML = AI.chats.map(c => `
    <div class="ask-tab ${c.id === AI.activeChat ? 'on' : ''}" data-chat="${esc(c.id)}">
      <span class="t">${esc(c.title)}</span>
      <span class="del" data-del="${esc(c.id)}" title="Delete chat" aria-label="Delete chat">&times;</span>
    </div>`).join('');
  tabs.querySelectorAll('[data-chat]').forEach(el => {
    el.onclick = (e) => {
      if (e.target.closest('[data-del]')) return;
      switchChat(el.dataset.chat);
    };
  });
  tabs.querySelectorAll('[data-del]').forEach(el => {
    el.onclick = (e) => { e.stopPropagation(); deleteChat(el.dataset.del); };
  });
  // A mouse wheel only scrolls vertically by default, which does nothing on a
  // row that only overflows horizontally - so older chats were unreachable
  // without a trackpad's shift-scroll. Redirect vertical wheel motion here.
  if (!tabs.dataset.wheelBound) {
    tabs.dataset.wheelBound = '1';
    tabs.addEventListener('wheel', (e) => {
      if (!e.deltaY || e.deltaX) return;
      tabs.scrollLeft += e.deltaY;
      e.preventDefault();
    }, { passive: false });
  }
}

export function renderAsk() {
  renderAskTabs();
  const chat = activeChat();
  const body = document.getElementById('ask-body');
  if (!chat || !chat.history.length) {
    const models = AI.status ? `${esc(AI.status.models.reasoning)} over ${esc(AI.status.provider)}` : 'the configured model';
    body.innerHTML = `
      <div class="sheet-note" style="margin:0 0 11px">
        Ask about this estate in plain language. Answers are computed from the
        same layers the map is painted from, by ${models}, and every figure is
        checked back against them. The map follows the answer.
      </div>
      <div class="eg">${AI.examples.map(q =>
        `<button data-eg="${esc(q)}">${esc(q)}</button>`).join('')}</div>`;
    body.querySelectorAll('[data-eg]').forEach(b => {
      b.onclick = () => submitAsk(b.dataset.eg);
    });
    return;
  }

  body.innerHTML = chat.history.map((h, i) => {
    if (h.pending) {
      return `<div class="qa"><div class="q">${esc(h.q)}</div>
        <div class="thinking">Reading the layers…</div></div>`;
    }
    if (h.error) {
      return `<div class="qa"><div class="q">${esc(h.q)}</div>
        <div class="a err">${esc(h.error)}</div></div>`;
    }
    return `<div class="qa">
      <div class="q">${esc(h.q)}</div>
      <div class="a">${esc(h.a)}</div>
      <div class="brief-meta" style="border:none;padding:7px 0 0">
        ${genBadge(h.model)} ${auditChip(h.audit)}
        <span>${h.ms} ms</span>
      </div>
      ${renderTrace(h.trace, i)}
    </div>`;
  }).join('');
  body.scrollTop = 0;
}

export function renderTrace(trace, i) {
  if (!trace || !trace.length) return '';
  return `<details class="trace">
    <summary>${trace.length} tool call${trace.length > 1 ? 's' : ''} behind this answer</summary>
    <div class="trace-b">
      ${trace.map(t => `<div class="trace-row ${t.ok ? '' : 'bad'}">
        <code>${esc(t.tool)}</code>
        <span class="args">${esc(JSON.stringify(t.args))}</span>
        <span class="ms">${t.elapsed_ms === null || t.elapsed_ms === undefined ? '' : t.elapsed_ms + ' ms'}</span>
      </div>`).join('')}
    </div>
  </details>`;
}

export async function submitAsk(question) {
  const q = (question || '').trim();
  const chat = activeChat();
  if (!q || AI.busy || !chat) return;
  AI.busy = true;
  const input = document.getElementById('ask-input');
  const send = document.getElementById('ask-send');
  if (input) input.value = '';
  if (send) send.disabled = true;
  if (chat.title === 'New chat') chat.title = q.slice(0, 40) + (q.length > 40 ? '…' : '');
  // Prior turns from this chat, oldest first, so a follow-up like "what about
  // last month" resolves against what was already asked here.
  const priorHistory = chat.history
    .filter(h => h.a && !h.error && !h.pending)
    .slice().reverse()
    .map(h => ({ question: h.q, answer: h.a }));
  chat.history.unshift({ q, pending: true });
  renderAsk();

  try {
    const out = await getJSON('/gis/ask', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        question: q,
        estate: S.estate || 'EC',
        history: priorHistory,
        // What the manager is looking at, so "this block" has a referent.
        view: {
          metric: S.metric,
          month: S.month || 'full recorded window',
          selected_block: S.selected && S.blocks
            ? (S.blocks.features.find(f => f.id === S.selected) || {}).properties?.block_label
            : null,
          fire_scenario: S.scenario,
        },
      }),
    });
    chat.history[0] = out.available === false
      ? { q, error: out.reason || 'The model did not answer.' }
      : { q, a: out.answer, model: out.model, ms: out.latency_ms,
          audit: out.figure_audit, trace: out.trace };
    if (out.focus) applyFocus(out.focus);
  } catch (e) {
    chat.history[0] = { q, error: e.message };
  } finally {
    AI.busy = false;
    if (send) send.disabled = false;
    saveChats();
    renderAsk();
  }
}

/* The agent's one side effect. Everything it can do to the map, it does
   through here, and every id was resolved on the server against the real
   feature list - so a block the model invented highlights nothing. */
export async function applyFocus(focus) {
  if (!focus) return;
  if (focus.month && (S.months.includes(focus.month) || S.forwardMonths.includes(focus.month))) {
    S.month = focus.month;
    renderMonths();
  }
  if (focus.metric && focus.metric !== S.metric) {
    await setMetric(focus.metric);
  } else if (focus.month) {
    await setMetric(S.metric);
  }
  highlightBlocks(focus.block_ids || []);
  offerFocusAsSet(focus.block_ids || []);
  if (focus.panel && PANELS[focus.panel]) openPanel(focus.panel);
}

/* An answer that named blocks is one gesture away from being a working set.
   The ids were resolved server-side against the real feature list, so what
   the copilot pointed at and what gets selected cannot diverge. */
function offerFocusAsSet(ids) {
  const host = document.getElementById('ask-body');
  if (!host) return;
  host.querySelectorAll('.ask-take').forEach(b => b.remove());
  if (!ids.length) return;
  const last = host.querySelector('.msg.a:last-of-type') || host.lastElementChild;
  if (!last) return;
  const btn = document.createElement('button');
  btn.className = 'ask-take';
  btn.type = 'button';
  btn.textContent = `Select these ${ids.length}`;
  btn.onclick = () => replaceSelection(ids);
  last.appendChild(btn);
}

export function highlightBlocks(ids) {
  if (!map || !map.getLayer('blocks-ai')) return;
  map.setFilter('blocks-ai', ids.length
    ? ['in', ['get', '_id'], ['literal', ids]]
    : ['==', ['get', '_id'], '__none__']);
  if (!ids.length || !S.blocks) return;
  // Frame what was just highlighted, but only when it is off screen: a manager
  // who asked a follow-up about blocks already in view should not have the
  // camera yanked from under them.
  const feats = S.blocks.features.filter(f => ids.includes(f.id));
  if (!feats.length) return;
  const b = new maplibregl.LngLatBounds();
  feats.forEach(f => f.geometry.coordinates[0].forEach(c => b.extend(c)));
  const view = map.getBounds();
  const inside = view.contains(b.getNorthEast()) && view.contains(b.getSouthWest());
  if (!inside) map.fitBounds(b, { padding: 120, duration: 900, maxZoom: 14.5 });
}

