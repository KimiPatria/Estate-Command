import { AI, aiFailure, auditChip, genBadge } from './store.js';
import { getJSON } from '../lib/api.js';
import { esc, fmt, row } from '../lib/fmt.js';
import { selectBlockById } from '../map/select.js';
import { sheetBg } from '../shell/sheet.js';
import { S } from '../state/store.js';

/* ── the block brief, in the drawer ─────────────────────────────────── */

export function blockBriefSection(blockId) {
  const b = AI.briefs[blockId];
  if (!b) {
    return `<div class="dr-sec">
      <div class="dr-sec-t">Agronomist's read</div>
      <button class="gen-btn" data-brief="${esc(blockId)}">
        <svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"
          stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <path d="M12 3v3m0 12v3M3 12h3m12 0h3M5.6 5.6l2.1 2.1m8.6 8.6l2.1 2.1m0-12.8l-2.1 2.1m-8.6 8.6l-2.1 2.1"/>
        </svg>
        Brief me on this block
      </button>
      <div class="src">Reads this block's own figures, its planting cohort and its
        satellite canopy value, and writes the judgement. Nothing is computed by
        the model: every number it quotes is checked back against the payload.</div>
    </div>`;
  }
  if (b.pending) {
    return `<div class="dr-sec"><div class="dr-sec-t">Agronomist's read</div>
      <div class="thinking">Reading the block…</div></div>`;
  }
  if (b.available === false) {
    return `<div class="dr-sec"><div class="dr-sec-t">Agronomist's read</div>
      ${aiFailure(b, 'The brief')}</div>`;
  }
  return `<div class="dr-sec">
    <div class="dr-sec-t">Agronomist's read</div>
    <div class="brief">
      <div class="brief-h">
        <span class="posture ${esc(b.severity)}">${esc(b.severity)}</span>
        <h4>${esc(b.headline)}</h4>
      </div>
      <div class="brief-b">
        ${b.narrative.split(/\n\n+/).map(p => `<p>${esc(p)}</p>`).join('')}
        ${b.evidence && b.evidence.length ? `<ul class="ev" style="margin-top:9px">
          ${b.evidence.map(e => `<li>${esc(e)}</li>`).join('')}</ul>` : ''}
      </div>
      ${b.action ? `<div class="brief-act"><b>Next:</b> ${esc(b.action)}</div>` : ''}
      ${b.not_recorded && b.not_recorded.length ? `<div class="brief-act"
        style="background:none;color:var(--muted)"><b>Would need:</b>
        ${b.not_recorded.map(esc).join('; ')}</div>` : ''}
      <div class="brief-meta">
        ${genBadge(b.model)} ${auditChip(b.figure_audit)}
        <span>${b.cached ? 'cached' : b.latency_ms + ' ms'}</span>
      </div>
    </div>
  </div>`;
}

export async function loadBlockBrief(blockId) {
  AI.briefs[blockId] = { pending: true };
  if (S.selected === blockId) selectBlockById(blockId);
  try {
    const qs = new URLSearchParams({ estate: S.estate, block_id: blockId });
    if (S.month) qs.set('month', S.month);
    AI.briefs[blockId] = await getJSON('/gis/blocks/brief?' + qs.toString());
  } catch (e) {
    AI.briefs[blockId] = { available: false, reason: e.message };
  }
  if (S.selected === blockId) selectBlockById(blockId);
}

/* Canopy vigour, real, from the satellite layer. Sits in the drawer beside the
   client's own harvest figures precisely so the two can be compared. */
export function canopySection(f) {
  // Three states, kept apart on purpose. No table yet is not the same as no
  // value, and saying "under cloud" while a fetch is still in flight would be
  // the page inventing a reason - the one thing it exists not to do.
  if (!S.rows) return '';
  const r = S.rows[f.id];
  if (!r || r.ndre === null || r.ndre === undefined) {
    if (!S.satellite) return '';
    return `<div class="dr-sec">
      <div class="dr-sec-t">Canopy vigour</div>
      <div class="empty">Under cloud on ${esc(S.satellite.date)}. No value, rather
        than an interpolated one.</div>
    </div>`;
  }
  const real = r.ndre_source === 'real:sentinel-2';
  const anom = r.ndre_anomaly;
  return `<div class="dr-sec">
    <div class="dr-sec-t">Canopy vigour
      <span class="prov ${real ? 'real' : 'synthetic'}">${real ? 'satellite' : 'synthetic'}</span>
    </div>
    <dl class="kv">
      ${row('NDRE (red edge)', fmt(r.ndre, 3))}
      ${row('Against its cohort', anom === null || anom === undefined ? null
            : (anom > 0 ? '+' : '') + fmt(anom, 3))}
      ${row('NDVI', fmt(r.ndvi, 3))}
      ${row('Cloud-free pixels', fmt(r.ndre_valid_pct, 1), '%')}
    </dl>
    ${real ? `<div class="sat">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"
        stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <path d="M12 2a10 10 0 0 1 10 10M12 6a6 6 0 0 1 6 6"/><circle cx="7" cy="17" r="3"/>
      </svg>
      <div><b>Measured, not supplied.</b> Sentinel-2 scene of ${esc(S.satellite.date)},
        computed over this block's own polygon at 20 m and cloud-masked. The client
        does not hold this figure; it costs nothing to produce.</div>
    </div>` : `<div class="src">Generated series. No Sentinel-2 scene has been pulled
      for this estate.</div>`}
  </div>`;
}

/* The merged per-block table, which the drawer needs for anything beyond the
   feature properties. One fetch per estate and month, cached. */
export async function loadBlockRows(force) {
  const key = `${S.estate}|${S.month || ''}`;
  if (!force && S.rowsKey === key) return;
  try {
    const qs = new URLSearchParams({ estate: S.estate });
    if (S.month) qs.set('month', S.month);
    const d = await getJSON('/gis/blocks/table?' + qs.toString());
    S.rows = Object.fromEntries((d.rows || []).map(r => [r.block_id, r]));
    S.rowsKey = key;
  } catch (e) {
    S.rows = null; S.rowsKey = null;
  }
}

/* ── the fire brief ─────────────────────────────────────────────────── */

export async function openFireBrief() {
  const body = document.getElementById('sheet-body');
  document.querySelector('.sheet-h h2').textContent = 'Duty officer brief';
  body.innerHTML = '<div class="thinking">Assessing…</div>';
  sheetBg.hidden = false;
  let b;
  try {
    b = await getJSON(`/gis/fire/brief?estate=${encodeURIComponent(S.estate)}&scenario=${S.scenario}`);
  } catch (e) {
    body.innerHTML = aiFailure({ reason: e.message }, 'The brief');
    return;
  }
  if (b.available === false) { body.innerHTML = aiFailure(b, 'The brief'); return; }
  if (b.generated === false) {
    body.innerHTML = `<div class="empty">${esc(b.headline)}</div>`;
    return;
  }
  body.innerHTML = `
    <div class="brief">
      <div class="brief-h">
        <span class="posture ${esc(b.posture)}">${esc(b.posture)}</span>
        <h4>${esc(b.headline)}</h4>
      </div>
      <div class="brief-b">
        ${b.assessment.split(/\n\n+/).map(p => `<p>${esc(p)}</p>`).join('')}
        ${b.priority && b.priority.length ? `<ul class="pri" style="margin-top:9px">
          ${b.priority.map((p, i) => `<li><span class="n">${i + 1}</span>
            <span><b>Block ${esc(p.block)}</b> — ${esc(p.why)}</span></li>`).join('')}
        </ul>` : ''}
      </div>
      ${b.orders && b.orders.length ? `<div class="brief-act">
        <b>Orders</b><ul class="ev" style="margin-top:6px">
        ${b.orders.map(o => `<li>${esc(o)}</li>`).join('')}</ul></div>` : ''}
      ${b.blocked_by_missing_data && b.blocked_by_missing_data.length ? `<div class="brief-act"
        style="background:none"><b>The response half needs</b>
        <ul class="ev" style="margin-top:6px">
        ${b.blocked_by_missing_data.map(o => `<li>${esc(o)}</li>`).join('')}</ul></div>` : ''}
      <div class="brief-meta">
        ${genBadge(b.model)} ${auditChip(b.figure_audit)}
        <span>${b.cached ? 'cached' : b.latency_ms + ' ms'}</span>
      </div>
    </div>`;
}

/* ── the shift handover, in the decision log ────────────────────────── */

export async function loadHandover(btn) {
  const holder = document.getElementById('handover');
  if (!holder) return;
  if (btn) btn.disabled = true;
  holder.innerHTML = '<div class="thinking">Reading the log and the open positions…</div>';
  let h;
  try {
    h = await getJSON(`/gis/handover?estate=${encodeURIComponent(S.estate)}`);
  } catch (e) {
    holder.innerHTML = aiFailure({ reason: e.message }, 'The handover note');
    return;
  }
  if (h.available === false) { holder.innerHTML = aiFailure(h, 'The handover note'); return; }
  const list = (title, items) => items && items.length
    ? `<div class="brief-act" style="background:none"><b>${title}</b>
       <ul class="ev" style="margin-top:6px">${items.map(x => `<li>${esc(x)}</li>`).join('')}</ul></div>`
    : '';
  holder.innerHTML = `
    <div class="brief">
      <div class="brief-h"><h4>${esc(h.headline)}</h4></div>
      ${list('Decided', h.decided)}
      ${list('Still open', h.open)}
      ${list('Watch', h.watch)}
      ${h.note ? `<div class="brief-b"><p>${esc(h.note)}</p></div>` : ''}
      <div class="brief-meta">
        ${genBadge(h.model)} ${auditChip(h.figure_audit)}
        <span>${h.cached ? 'cached' : h.latency_ms + ' ms'}</span>
      </div>
    </div>`;
}

