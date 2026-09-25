/* The context bar: where you are, who is looking, and what this app runs on.
 *
 * These were three separate rail accordions plus a footer button. None of
 * them is a thing you fiddle with while working - estate and role are set
 * once, coverage and readiness are read - so they belong in the chrome rather
 * than in the column that also held the panel menu.
 */
import { getJSON } from '../lib/api.js';
import { fmt, esc } from '../lib/fmt.js';
import { fitAllEstates } from '../map/camera.js';
import { map } from '../map/instance.js';
import { setMetric } from '../map/choropleth.js';
import { selectEstate } from '../map/estate.js';
import { ROLES } from '../state/constants.js';
import { S } from '../state/store.js';
import { showPop, closePop } from './pop.js';
import { sheetBg } from './sheet.js';

function el(id) { return document.getElementById(id); }

/* ── estate ────────────────────────────────────────────────────────────── */

export function renderEstates() {
  const e = S.estates.find(x => x.estate_code === S.estate);
  el('ctx-estate-v').textContent = S.estate || '—';
  el('ctx-estate').title = e
    ? (e.has_block_geometry ? `${e.blocks} blocks · ${fmt(e.planted_ha)} ha`
                            : `outline only · ${fmt(e.planted_ha)} ha`)
    : 'Choose an estate';
}

function estateMenu() {
  return `<div class="est">` + S.estates.map(e => {
    const real = e.has_block_geometry;
    const info = real ? `${e.blocks} blocks · ${fmt(e.planted_ha)} ha`
                      : `outline only · ${fmt(e.planted_ha)} ha`;
    return `<button class="est-row ${e.estate_code === S.estate ? 'on' : ''}"
                    data-estate="${esc(e.estate_code)}">
      <span class="code">${esc(e.estate_code)}</span>
      <span class="info">${esc(info)}</span>
      <span class="pill ${real ? 'real' : 'hull'}">${real ? 'ArcGIS' : 'GPS hull'}</span>
    </button>`;
  }).join('') + `</div>`;
}

/* ── role ──────────────────────────────────────────────────────────────── */

export function renderRoles() {
  const r = ROLES[S.role];
  el('ctx-role-v').textContent = r ? r.short : '—';
  el('ctx-role').title = r ? r.hint : '';
}

function roleMenu() {
  return `<div class="roles">` + Object.entries(ROLES).map(([k, r]) => `
    <button class="role ${k === S.role ? 'on' : ''}" data-role="${esc(k)}">
      ${esc(r.label)}<small>${esc(r.hint)}</small>
    </button>`).join('') + `</div>`;
}

export function applyRole(role) {
  S.role = role;
  const r = ROLES[role];
  renderRoles();
  const target = S.blocks ? r.metric : 'palm_age_years';
  if (S.blocks) setMetric(target);
  if (r.fitAll) fitAllEstates();
  else map.easeTo({ zoom: r.zoom, duration: 700 });
}

/* ── coverage, and the data request it opens ───────────────────────────── */

export function renderCoverage() {
  const s = S.features && S.features.summary;
  if (!s) return;
  el('coverage-live').innerHTML =
    `<b>${s.features_live}</b> of ${s.features_total} live`;
  // The waiting half is the ask, and the ask is the product. Gold because it
  // is what the meeting is for, not a warning about the demo.
  el('coverage-want').textContent = s.feeds_requested
    ? `${s.feeds_requested} extracts wanted`
    : 'all on your data';
  el('coverage-btn').title = s.headline || 'The data request';
}

async function openDataRequest() {
  const body = el('sheet-body');
  document.querySelector('.sheet-h h2').textContent = 'The data request';
  body.dataset.view = 'data-request';
  body.innerHTML = '<div class="empty">Loading…</div>';
  sheetBg.hidden = false;
  try {
    const d = await getJSON('/gis/data-request');
    body.innerHTML = renderDataRequest(d);
  } catch (err) {
    body.innerHTML = `<div class="sheet-note">The data request could not be loaded.</div>`;
  }
}

function renderDataRequest(d) {
  const head = `
    <div class="kpis">
      <div class="kpi"><b>${d.total_requirements}</b><span>extracts wanted</span></div>
      <div class="kpi"><b>${d.systems}</b><span>source systems</span></div>
      ${d.highest_value ? `<div class="kpi alert"><b>${d.highest_value.unblocks_count}</b>
        <span>unblocked by the single best ask</span></div>` : ''}
    </div>`;

  const groups = (d.groups || []).map(g => `
    <div class="cap">
      <div class="cap-h">
        <h4>${esc(g.label)}</h4>
        <span class="status partial">${g.unblocks_count} features</span>
      </div>
      <table class="tbl">
        <thead><tr><th>Extract</th><th>Where it lives</th><th class="num">Unblocks</th></tr></thead>
        <tbody>${(g.requirements || []).map(r => `
          <tr>
            <td>${esc(r.entity)}</td>
            <td style="color:var(--muted)">${esc(r.table || '—')}</td>
            <td class="num">${(r.unblocks || []).length}</td>
          </tr>`).join('')}</tbody>
      </table>
    </div>`).join('');

  return head + groups;
}

/* ── readiness ─────────────────────────────────────────────────────────── */

export function renderReadinessButton() {
  const c = (S.readiness && S.readiness.summary) || {};
  el('readiness-counts').textContent =
    `${c.ready || 0}/${(c.ready || 0) + (c.synthetic || 0) + (c.partial || 0) + (c.unavailable || 0)}`;
  el('readiness-btn').title =
    `${c.ready || 0} ready · ${c.synthetic || 0} synthetic · `
    + `${c.partial || 0} partial · ${c.unavailable || 0} missing`;
}

/* ── wiring ────────────────────────────────────────────────────────────── */

export function initContextBar() {
  const pop = document.createElement('div');
  pop.className = 'pop';
  pop.hidden = true;
  document.body.appendChild(pop);

  el('ctx-estate').onclick = () => showPop(pop, el('ctx-estate'), estateMenu(), p => {
    p.querySelectorAll('[data-estate]').forEach(b => {
      b.onclick = () => { closePop(); selectEstate(b.dataset.estate); };
    });
  });

  el('ctx-role').onclick = () => showPop(pop, el('ctx-role'), roleMenu(), p => {
    p.querySelectorAll('[data-role]').forEach(b => {
      b.onclick = () => { closePop(); applyRole(b.dataset.role); };
    });
  });

  el('coverage-btn').onclick = openDataRequest;
}
