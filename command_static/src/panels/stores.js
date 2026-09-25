/* Stores: what to order, when, and why, for the people who run the store.
 *
 * Every section leads with the sentence a storekeeper acts on ("Order by 31
 * July: 250 t"), shows where stock is heading as a band rather than a table,
 * says why the buffer is the size it is, and keeps the arithmetic and the
 * checks one menu away.
 *
 *   Now        At a glance · To order
 *   Materials  Fertiliser · Agrochemicals · Fuel · Spare parts · (the open material)
 *   Suppliers  Lead times against quotes · Slow months
 *   Ledger     Movements · Purchase orders · Rush buys
 *   Trust      The replay · Backtests and checks · How it works
 *
 * Every figure and sentence comes from the server (gis/stores.py); this
 * module only lays them out.
 */
import { esc, fmt } from '../lib/fmt.js';
import { getJSON } from '../lib/api.js';
import { S } from '../state/store.js';
import { recordDecision } from './decisions.js';
import { checksList, chips, explainBlock, hero, predictedBadge, tbl, trainedOn, trustPill } from './forecasts.js';
import { openPanel } from './registry.js';

const GROUPS = [['FERT', 'Fertiliser'], ['AGCH', 'Agrochemicals'], ['FUEL', 'Fuel'], ['SPARE', 'Spare parts']];
const STATUS = { order_now: 'Order now', this_week: 'Order this week', covered: 'Covered', overstocked: 'Overstocked' };
const MOVE_FILTERS = [['', 'All'], ['101', 'Receipts'], ['201,261', 'Issues'], ['551,701,702', 'Counts and scrap']];

const n0 = v => (v === null || v === undefined) ? '—' : fmt(v, 0);

/* Quantities read in the unit a storekeeper uses: tonnes for fertiliser. */
export function qty(v, unit) {
  if (v === null || v === undefined) return '—';
  const u = String(unit || '').toLowerCase();
  if (u === 'kg' && Math.abs(v) >= 1000) return `${fmt(v / 1000, Math.abs(v) < 10000 ? 1 : 0)} t`;
  return `${fmt(v, u === 'ea' ? 0 : (Math.abs(v) < 10 ? 1 : 0))} ${u}`;
}

export function rp(v) {
  if (v === null || v === undefined) return '—';
  const a = Math.abs(v);
  if (a >= 1e9) return `Rp ${fmt(v / 1e9, 2)} bn`;
  if (a >= 1e7) return `Rp ${fmt(v / 1e6, 0)} m`;
  if (a >= 1e5) return `Rp ${fmt(v / 1e6, 1)} m`;
  return `Rp ${fmt(v, 0)}`;
}

const status = s => `<span class="st-status ${esc(s)}">${esc(STATUS[s] || s)}</span>`;

/* A supplier on one track, for an order placed today: the band from the
   most likely time to the one-in-ten time, a line at the most likely, and a
   dot where its quote sits. */
function leadBar(median, p90, quoted, max) {
  if (!max) return '';
  const x = v => Math.max(0, Math.min(100, 100 * v / max));
  return `<span class="fc-range" title="Most likely ${fmt(median, 0)} days, 1 in 10 takes ${fmt(p90, 0)} or more; quotes ${fmt(quoted, 0)}">
    <i class="band" style="left:${x(median)}%;width:${Math.max(1.5, x(p90) - x(median))}%"></i>
    <i class="most" style="left:${x(median)}%"></i>
    <i class="act" style="left:${x(quoted)}%"></i>
  </span>`;
}
const policy = p => p === 'learned'
  ? '<span class="chip pos" title="The twelve-month replay showed the learned reorder point costs less here">learned</span>'
  : '<span class="chip" title="SAP\'s own settings stay: the replay did not show the learned reorder point doing better here">SAP settings</span>';
const dateWords = iso => iso ? new Date(`${iso}T00:00:00`).toLocaleDateString('en-GB', { day: 'numeric', month: 'short' }) : '—';

function decideSpec(v, kind) {
  const req = kind === 'material_requisition';
  return JSON.stringify({
    use_case: req ? 'Stores: reorder' : 'Stores: MRP settings',
    title: req ? `Order ${v.maktx}` : `Correct the SAP settings for ${v.maktx}`,
    subject: v.matnr, action: 'accepted', artifact_kind: kind,
    stores: { matnr: v.matnr, kind },
    evidence: [v.plain.headline, v.plain.supplier, v.plain.buffer],
  }).replace(/'/g, '&#39;');
}

function actions(v) {
  const buttons = [];
  if (v.order_qty > 0) {
    buttons.push(`<button class="accept" data-decide='${decideSpec(v, 'material_requisition')}'>Raise requisition: ${esc(qty(v.order_qty, v.unit))}</button>`);
  }
  buttons.push(`<button data-decide='${decideSpec(v, 'mrp_settings_change')}'>Propose SAP settings change</button>`);
  return `<div><div class="fp-actbar"><span class="grow"></span>
      <span class="acts" data-act-scope>${buttons.join('')}</span></div>
    <div class="stub"></div></div>`;
}

/* ── the projection ───────────────────────────────────────────────────── */

/* Stock over the next 90 days if nothing more is ordered: the band is where
   it lands 8 times in 10, the line the middle, the dashed line the reorder
   point on the order-by day, and a marker on that day if it falls inside. */
export function projectionChart(p, unit, orderByDays, orderByLabel) {
  if (!p || !p.p50 || !p.p50.length) return '';
  const W = 660, H = 230, L = 64, R = 14, T = 12, B = 28;
  const n = p.p50.length;
  const all = [...p.p10, ...p.p90, p.reorder_point, 0];
  const floor = Math.min(...all);
  let lo = floor;
  let hi = Math.max(...all);
  if (hi - lo < 1e-9) hi = lo + 1;
  const span = hi - lo;
  // Pad below only when stock can really go below zero; otherwise zero is the floor.
  if (floor < 0) lo -= span * 0.04;
  hi += span * 0.06;
  const x = i => L + (W - L - R) * i / (n - 1);
  const y = v => T + (H - T - B) * (1 - (v - lo) / (hi - lo));
  const pts = arr => arr.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`);
  const band = [...pts(p.p90), ...pts(p.p10).reverse()].join(' ');
  const ticks = [0, 1, 2, 3].map(k => lo + (hi - lo) * k / 3);
  const days = Array.from({ length: Math.floor((n - 1) / 30) + 1 }, (_, k) => k * 30);
  const by = orderByDays !== null && orderByDays !== undefined && orderByDays < n ? orderByDays : null;
  return `<svg class="st-chart" viewBox="0 0 ${W} ${H}" role="img"
      aria-label="Stock over the next ${n - 1} days if nothing more is ordered">
    ${ticks.map(t => `<line class="grid" x1="${L}" x2="${W - R}" y1="${y(t).toFixed(1)}" y2="${y(t).toFixed(1)}"/>
      <text x="${L - 6}" y="${(y(t) + 3).toFixed(1)}" text-anchor="end">${esc(qty(t, unit))}</text>`).join('')}
    ${days.map(d => `<text x="${x(d).toFixed(1)}" y="${H - 8}" text-anchor="${d === 0 ? 'start' : d === n - 1 ? 'end' : 'middle'}">${d === 0 ? 'today' : `+${d} d`}</text>`).join('')}
    ${floor < 0 ? `<line class="zero" x1="${L}" x2="${W - R}" y1="${y(0).toFixed(1)}" y2="${y(0).toFixed(1)}"/>` : ''}
    <polygon class="band" points="${band}"/>
    <polyline class="mid" points="${pts(p.p50).join(' ')}"/>
    <line class="rop" x1="${L}" x2="${W - R}" y1="${y(p.reorder_point).toFixed(1)}" y2="${y(p.reorder_point).toFixed(1)}"/>
    <text class="lbl-rop" x="${W - R - 4}" y="${(y(p.reorder_point) - 5).toFixed(1)}" text-anchor="end">reorder point${orderByLabel ? ` on ${esc(orderByLabel)}` : ''}</text>
    ${by !== null ? `<line class="by" x1="${x(by).toFixed(1)}" x2="${x(by).toFixed(1)}" y1="${T}" y2="${H - B}"/>
      <text class="lbl-by" x="${(x(by) + 4).toFixed(1)}" y="${T + 10}">order by</text>` : ''}
  </svg>
  <div class="st-legend"><span><i class="lg-band"></i>8 times in 10</span><span><i class="lg-mid"></i>most likely</span>
    <span><i class="lg-rop"></i>reorder point</span>${floor < 0 ? '<span><i class="lg-zero"></i>empty</span>' : ''}</div>`;
}

/* ── the window ───────────────────────────────────────────────────────── */

export function storesPopup() {
  S.stores = S.stores || { section: 'overview', matnr: null, moveType: '', moveMine: false };
  const F = S.stores;
  let O = null;
  const cache = {};
  const fetchView = async (key, url) => {
    if (!cache[key]) cache[key] = await getJSON(url);
    return cache[key];
  };

  return {
    key: 'stores',
    initial: F.section,
    loadingText: 'Reading the store… the first time after a restart this can take half a minute while the replay runs.',
    async load() {
      Object.keys(cache).forEach(k => delete cache[k]);
      O = await getJSON('/gis/stores');
    },
    header: () => O && O.available ? `
      ${predictedBadge('Reorder points and lead times are learned, not recorded')}
      <span class="prov synthetic" title="Generated SAP MM records, shaped as the extract that would replace them">generated MM records</span>
      <span class="prov scheduled">as of ${esc(O.date_label)}</span>` : '',
    menu: () => {
      if (!O || !O.available) return [];
      const g = key => O.groups.find(x => x.group === key) || { to_order: 0, materials: 0 };
      const open = F.matnr && O.materials.find(m => m.matnr === F.matnr);
      return [
        { label: 'Now', items: [
          { id: 'overview', label: 'At a glance' },
          { id: 'order', label: 'To order', count: O.to_order || '', alert: O.to_order > 0 },
        ] },
        { label: 'Materials', items: [
          ...GROUPS.map(([k, l]) => ({ id: `g:${k}`, label: l, count: g(k).to_order || g(k).materials, alert: g(k).to_order > 0 })),
          ...(open ? [{ id: 'material', label: `▸ ${open.maktx}` }] : []),
        ] },
        { label: 'Suppliers', items: [
          { id: 'leadtimes', label: 'Lead times against quotes' },
          { id: 'season', label: 'Slow months' },
        ] },
        { label: 'Ledger', items: [
          { id: 'movements', label: 'Movements' },
          { id: 'pos', label: 'Purchase orders' },
          { id: 'rush', label: 'Rush buys', count: O.rush_90d.orders || '' },
        ] },
        { label: 'Trust', items: [
          { id: 'replay', label: 'The replay' },
          { id: 'checks', label: 'Backtests and checks',
            count: `${O.trust.filter(t => t.grade.passes).length}/${O.trust.length}` },
          { id: 'how', label: 'How it works' },
        ] },
      ];
    },
    onSection: id => { F.section = id; },
    async section(id) {
      if (!O.available) return { title: 'Stores', html: `<div class="empty">${esc(O.reason)}</div>` };
      if (id === 'order') return secOrder(O);
      if (id.startsWith('g:')) return secGroup(O, id.slice(2));
      if (id === 'material' && F.matnr) {
        return secMaterial(await fetchView(`m:${F.matnr}`, `/gis/stores/material?matnr=${encodeURIComponent(F.matnr)}`));
      }
      if (id === 'leadtimes') return secLeadTimes(await fetchView('lt', '/gis/stores/lead-times'));
      if (id === 'season') return secSeason(await fetchView('lt', '/gis/stores/lead-times'));
      if (id === 'movements') {
        const q = new URLSearchParams({ limit: '150' });
        if (F.moveType) q.set('bwart', F.moveType);
        if (F.moveMine && F.matnr) q.set('matnr', F.matnr);
        return secMovements(await fetchView(`mv:${q}`, `/gis/stores/ledger?${q}`), F);
      }
      if (id === 'pos') return secPOs(await fetchView('pos', '/gis/stores/purchase-orders?kind=normal&limit=200'), false);
      if (id === 'rush') return secPOs(await fetchView('rush', '/gis/stores/purchase-orders?kind=rush&limit=200'), true);
      if (id === 'replay') return secReplay(await fetchView('acc', '/gis/stores/accuracy'));
      if (id === 'checks') return secChecks(await fetchView('acc', '/gis/stores/accuracy'));
      if (id === 'how') return secHow(await fetchView('acc', '/gis/stores/accuracy'), O);
      return secOverview(O);
    },
    wire(root, api) {
      root.querySelectorAll('[data-go]').forEach(b => {
        b.onclick = ev => { ev.stopPropagation(); api.go(b.dataset.go); };
        if (b.tagName !== 'BUTTON') {
          b.onkeydown = ev => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); api.go(b.dataset.go); } };
        }
      });
      root.querySelectorAll('[data-mat]').forEach(b => {
        b.onclick = ev => {
          if (ev.target.closest('button[data-decide]')) return;
          F.matnr = b.dataset.mat;
          if (api.section() === 'material') api.rerender(); else api.go('material');
        };
      });
      root.querySelectorAll('[data-open-panel]').forEach(b => { b.onclick = () => openPanel(b.dataset.openPanel); });
      root.querySelectorAll('[data-mvtype]').forEach(b => {
        b.onclick = () => { F.moveType = b.dataset.mvtype; api.rerender(); };
      });
      root.querySelectorAll('[data-mvmine]').forEach(b => {
        b.onclick = () => { F.moveMine = b.dataset.mvmine === '1'; api.rerender(); };
      });
      root.querySelectorAll('[data-decide]').forEach(b => {
        b.onclick = ev => { ev.stopPropagation(); recordDecision(JSON.parse(b.dataset.decide), b); };
      });
    },
  };
}

/* ── sections ─────────────────────────────────────────────────────────── */

function materialRows(rows, { showGroup = false, showSupplier = true } = {}) {
  return tbl(`<tr><th>Status</th><th>Material</th>${showGroup ? '<th>Group</th>' : ''}<th class="num">On hand</th>
      <th class="num">Days of cover</th><th class="num">Reorder point</th><th>Order by</th><th class="num">Order</th>
      ${showSupplier ? '<th>Supplier</th>' : ''}<th>Using</th><th>In words</th></tr>
    ${rows.map(r => `<tr class="fp-row-map" data-mat="${esc(r.matnr)}" title="Open ${esc(r.maktx)}">
      <td>${status(r.status)}</td><td><b>${esc(r.maktx)}</b> <span class="fp-dim">${esc(r.matnr)}</span></td>
      ${showGroup ? `<td>${esc(r.group_label)}</td>` : ''}
      <td class="num">${esc(qty(r.on_hand, r.unit))}${r.on_order ? `<br><span class="fp-dim">+${esc(qty(r.on_order, r.unit))} on order</span>` : ''}</td>
      <td class="num">${n0(r.days_of_cover)}</td>
      <td class="num">${esc(qty(r.reorder_point, r.unit))}</td>
      <td>${r.order_by ? esc(dateWords(r.order_by)) : '<span class="fp-dim">none due</span>'}</td>
      <td class="num">${r.order_qty ? esc(qty(r.order_qty, r.unit)) : '—'}</td>
      ${showSupplier ? `<td>${esc(r.supplier)}</td>` : ''}
      <td>${policy(r.policy_in_force)}</td>
      <td class="fc-why">${esc(r.plain.headline)}</td></tr>`).join('')}`);
}

function secOverview(O) {
  const inUse = O.materials.filter(m => m.policy_in_force === 'learned').length;
  const top = O.materials.filter(m => m.status === 'order_now' || m.status === 'this_week');
  const rop = O.reorder_point_stock || {};
  return {
    title: `The store at a glance: ${O.date_label}`,
    html: `
      <div class="fc-summary-box"><div class="fp-card-t">In short</div>
        <ul>${O.summary.map(l => `<li>${esc(l)}</li>`).join('')}</ul></div>
      <div class="kpis">
        <div class="kpi ${O.to_order ? 'warn' : ''}"><b>${O.to_order}</b><span>to order this week</span></div>
        <div class="kpi"><b>${rp(O.stock_value_idr)}</b><span>stock held, all materials</span></div>
        <div class="kpi"><b>${rp(rop.held_idr)}</b><span>held in reorder-point materials, against ${rp(rop.recommended_idr)} the learned points would carry on average</span></div>
        <div class="kpi ${O.rush_90d.orders ? 'warn' : ''}"><b>${O.rush_90d.orders}</b><span>rush buys in 90 days, ${rp(O.rush_90d.premium_idr)} over the normal price</span></div>
        <div class="kpi"><b>${inUse} of ${O.materials.length}</b><span>materials on learned reorder points${O.switch ? '' : ' (switched off)'}</span></div>
      </div>
      ${top.length ? `<div class="sub-t">Order this week</div>${materialRows(top)}` : '<div class="fp-callout">Nothing needs ordering this week.</div>'}
      <div class="sub-t">By group</div>
      <div class="fc-cards">${O.groups.map(g => {
        const r = g.replay || {};
        return `<div class="fc-card" data-go="g:${esc(g.group)}" role="button" tabindex="0">
          <div class="fc-card-h"><span>${esc(g.label)}</span>${g.to_order ? `<span class="st-status this_week">${g.to_order} to order</span>` : ''}</div>
          <div class="fc-card-big">${rp(g.value_idr)}</div>
          <div class="fc-card-sub">held across ${g.materials} material${g.materials === 1 ? '' : 's'}, service level ${g.service_level_pct}%</div>
          <span class="fc-more">Every material →</span></div>`;
      }).join('')}</div>
      <div class="sub-t">How far to trust it</div>
      <div class="fc-trustrow">${O.trust.map(t => `<div class="fc-trustcard">
          <div class="fc-card-h"><span>${esc(t.title)}</span>${trustPill(t.grade)}</div>
          <p>${esc(t.headline)}</p>${trainedOn(t.trained_on)}</div>`).join('')}</div>`,
  };
}

function secOrder(O) {
  const rows = O.materials.filter(m => m.status === 'order_now' || m.status === 'this_week');
  const soon = O.materials.filter(m => m.status === 'covered' && m.order_by_days !== null && m.order_by_days <= 30);
  return {
    title: 'To order',
    html: `
      ${rows.length ? materialRows(rows) : '<div class="fp-callout">Nothing reaches its reorder point this week.</div>'}
      ${soon.length ? `<div class="sub-t">Due in the next 30 days</div>${materialRows(soon)}` : ''}
      <div class="fp-actbar"><button class="ops-btn" data-open-panel="assumptions">Open the assumption register</button></div>`,
  };
}

function secGroup(O, key) {
  const g = O.groups.find(x => x.group === key);
  const rows = O.materials.filter(m => m.group === key);
  const label = (GROUPS.find(x => x[0] === key) || [key, key])[1];
  const r = (g && g.replay) || {};
  return {
    title: label,
    html: `
      ${materialRows(rows, { showSupplier: true })}
      ${r.label ? `<div class="fp-grid3">
        <div class="fp-card"><div class="fp-card-t">Service level</div><div class="fp-big">${r.target_service_pct}%</div></div>
        <div class="fp-card"><div class="fp-card-t">SAP's settings, last 12 months</div>
          <div class="fp-big">${r.old_service_pct}%</div><p class="fp-dim">cycles clear, ${r.old_rush_orders} rush buys, ${rp(r.old_cost_idr)}</p></div>
        <div class="fp-card"><div class="fp-card-t">Learned reorder points, same months</div>
          <div class="fp-big">${r.new_service_pct}%</div><p class="fp-dim">cycles clear, ${r.new_rush_orders} rush buys, ${rp(r.new_cost_idr)}</p></div>
      </div>` : ''}
      <div class="fp-actbar"><button class="ops-btn" data-go="replay">The replay, material by material</button>
        ${key === 'FERT' ? '<button class="ops-btn" data-open-panel="nutrient">Open the nutrition panel</button>' : ''}</div>`,
  };
}

function secMaterial(V) {
  if (!V.available) return { title: 'Material', html: `<div class="empty">${esc(V.reason || 'Not found.')}</div>` };
  const u = V.unit;
  const sup = V.supplier;
  const rp_ = V.replay || {};
  const cc = V.cost_curve;
  const split = V.safety_stock > 0 ? Math.round(100 * V.safety_from_supplier / V.safety_stock) : 0;
  const big = V.status === 'covered' && V.order_by ? dateWords(V.order_by)
    : V.status === 'covered' ? 'Covered' : STATUS[V.status];
  const sapRows = V.sap.dismm === 'VB'
    ? `<tr><td>Reorder point</td><td class="num">${esc(qty(V.sap.minbe, u))} <span class="fp-dim">MINBE</span></td><td class="num"><b>${esc(qty(V.reorder_point, u))}</b></td></tr>
       <tr><td>Safety stock</td><td class="num">${esc(qty(V.sap.eisbe, u))} <span class="fp-dim">EISBE</span></td><td class="num"><b>${esc(qty(V.safety_stock, u))}</b></td></tr>`
    : `<tr><td>Next round ordered</td><td class="num">${esc(dateWords(V.sap.order_date))}</td><td class="num"><b>${esc(dateWords(V.order_by))}</b></td></tr>
       <tr><td>Safety stock</td><td class="num">${esc(qty(V.sap.eisbe, u))} <span class="fp-dim">EISBE</span></td><td class="num"><b>${esc(qty(V.safety_stock, u))}</b></td></tr>`;
  return {
    title: `${V.maktx} (${V.matnr})`,
    html: `
      ${hero(esc(big), `${status(V.status)} ${policy(V.policy_in_force)}`, esc(V.plain.headline),
        `<p class="fp-dim">${esc(V.plain.risk)}</p>`)}
      <div class="fp-card"><div class="fp-card-t">Where stock goes if nothing more is ordered</div>
        ${projectionChart(V.projection, u, V.order_by_days, V.order_by ? dateWords(V.order_by) : '')}
        <p class="fp-dim">On hand ${esc(qty(V.on_hand, u))}${V.on_order ? `, ${esc(qty(V.on_order, u))} on order` : ''}.
          ${V.round ? `Next round ${esc(dateWords(V.round.date))}: about ${esc(qty(V.round.expected, u))} of a ${esc(qty(V.round.programme, u))} programme.`
            : `Use over the next 30 days: ${esc(qty(V.use_30d, u))}${V.days_of_cover !== null ? `, ${n0(V.days_of_cover)} days of cover` : ''}.`}</p>
      </div>
      ${actions(V)}
      <div class="fp-grid3">
        <div class="fp-card"><div class="fp-card-t">Why the buffer is that size</div>
          <p>${esc(V.plain.buffer)}</p>
          ${V.safety_stock > 0 && !V.round ? `<div class="st-split" role="img" aria-label="${split}% of the safety stock is there because deliveries vary">
            <i class="sup" style="width:${split}%"></i><i class="use" style="width:${100 - split}%"></i></div>
            <div class="st-legend"><span><i class="lg-sup"></i>deliveries vary ${split}%</span><span><i class="lg-use"></i>use varies ${100 - split}%</span></div>` : ''}
        </div>
        <div class="fp-card"><div class="fp-card-t">The supplier</div>
          <p>${esc(V.plain.supplier)}</p>
          <div class="fc-band">${leadBar(sup.median, sup.p90, sup.quoted, Math.max(sup.p97, sup.quoted) * 1.1)}</div>
          ${sup.very_late_chance >= 0.05 ? `<p class="fp-dim">${Math.round(100 * sup.very_late_chance)}% chance of arriving very late.</p>` : ''}
          <div class="fp-actbar"><button class="ops-btn small" data-go="leadtimes">Every supplier</button></div>
        </div>
        <div class="fp-card"><div class="fp-card-t">SAP's settings against the learned ones</div>
          ${tbl(`<tr><th></th><th class="num">SAP</th><th class="num">Learned</th></tr>${sapRows}
            <tr><td>Lead time</td><td class="num">${sup.quoted} d <span class="fp-dim">PLIFZ</span></td><td class="num"><b>${n0(sup.usual)} d</b></td></tr>`)}
          <p class="fp-dim">${esc(V.plain.sap)}</p>
        </div>
      </div>
      ${rp_.old ? `<div class="sub-t">The last twelve months, replayed</div>
        <p>${esc(rp_.plain)}</p>
        ${tbl(`<tr><th></th><th class="num">Rush buys</th><th class="num">Cycles clear</th><th class="num">Average stock</th>
            <th class="num">Holding</th><th class="num">Storage loss</th><th class="num">Rush premium</th><th class="num">Total</th></tr>
          ${[['SAP\'s settings', rp_.old], ['Learned reorder point', rp_.new]].map(([l, r]) => `<tr><td><b>${l}</b></td>
            <td class="num">${r.rush_orders}</td><td class="num">${r.service_pct}%</td><td class="num">${rp(r.avg_stock_value_idr)}</td>
            <td class="num">${rp(r.holding_cost_idr)}</td><td class="num">${rp(r.storage_loss_idr)}</td><td class="num">${rp(r.rush_cost_idr)}</td>
            <td class="num"><b>${rp(r.total_cost_idr)}</b></td></tr>`).join('')}`)}` : ''}
      <div class="sub-t">What the service level costs</div>
      <p>${esc(cc.plain)}</p>
      ${tbl(`<tr><th class="num">Service level</th><th class="num">Safety stock</th><th class="num">Holding a year</th><th class="num">Rush buys a year</th><th class="num">Total</th><th></th></tr>
        ${cc.rows.map(r => `<tr class="${r.service_pct === cc.cheapest_pct ? 'good' : ''}">
          <td class="num">${r.service_pct}%</td><td class="num">${esc(qty(r.safety_stock, u))}</td>
          <td class="num">${rp(r.holding_idr)}</td><td class="num">${rp(r.rush_idr)}</td><td class="num"><b>${rp(r.total_idr)}</b></td>
          <td>${r.service_pct === cc.cheapest_pct ? '<span class="chip pos">cheapest</span>' : ''}
            ${Math.abs(r.service_pct - cc.chosen_pct) < 1 ? '<span class="chip">the register</span>' : ''}</td></tr>`).join('')}`)}
      ${V.consumption ? `<div class="sub-t">Use: how well it is forecast</div>
        <p>${esc(V.consumption.plain)} ${trustPill(V.consumption.grade)}</p>` : ''}
      ${V.use_per_unit ? `<p class="fp-dim">${esc(V.use_per_unit.plain)}</p>` : ''}
      ${V.diesel_check ? `<p class="fp-dim">${esc(V.diesel_check.plain)}</p>` : ''}
      ${V.open_orders.length ? `<div class="sub-t">On order</div>
        ${tbl(`<tr><th>PO</th><th>Placed</th><th>Due, as quoted</th><th class="num">Still to come</th></tr>
          ${V.open_orders.map(o => `<tr><td>${esc(o.ebeln)}</td><td>${esc(o.bedat)}</td><td>${esc(o.eindt)}</td><td class="num">${esc(qty(o.remaining, u))}</td></tr>`).join('')}`)}` : ''}
      <div class="sub-t">Latest movements</div>
      ${tbl(`<tr><th>Date</th><th>Type</th><th class="num">Quantity</th><th>Cost centre</th><th>Order</th><th>Block</th><th>PO</th></tr>
        ${V.movements.map(m => `<tr><td>${esc(m.budat)}</td><td>${esc(m.bwart)}</td>
          <td class="num ${m.shkzg === 'S' ? 'pos' : ''}">${m.shkzg === 'S' ? '+' : '−'}${esc(qty(m.menge, u))}</td>
          <td>${esc(m.kostl)}</td><td>${esc(m.aufnr)}</td><td>${esc(m.block_code)}</td><td>${esc(m.ebeln)}</td></tr>`).join('')}`)}
      <div class="fp-actbar"><button class="ops-btn" data-go="movements">Every movement</button>
        <button class="ops-btn" data-open-panel="assumptions">Open the assumption register</button></div>`,
  };
}

function secLeadTimes(T) {
  if (!T.available) return { title: 'Lead times', html: `<div class="empty">${esc(T.reason)}</div>` };
  const bt = T.backtest;
  const max = Math.max(...T.suppliers.map(s => Math.max(s.p90_now, s.quoted_days))) * 1.1;
  return {
    title: 'Lead times against quotes',
    html: `
      ${tbl(`<tr><th>Supplier</th><th>Route</th><th>Supplies</th><th class="num">Quotes</th><th class="num">Usually</th>
          <th>Quote against an order placed today</th><th class="num">Very late</th><th class="num">Orders</th><th>Slow months</th></tr>
        ${T.suppliers.map(s => `<tr><td><b>${esc(s.name)}</b><br><span class="fp-dim">${esc(s.city)}</span></td>
          <td>${esc(s.route)}</td><td class="fc-why">${esc(s.materials.join(', '))}</td>
          <td class="num">${s.quoted_days} d</td><td class="num"><b>${n0(s.usual_days)} d</b><br><span class="fp-dim">×${fmt(s.factor, 2)}</span></td>
          <td>${leadBar(s.median_now, s.p90_now, s.quoted_days, max)}</td>
          <td class="num ${s.very_late_chance >= 0.1 ? 'neg' : ''}">${Math.round(100 * s.very_late_chance)}%</td>
          <td class="num">${s.orders}${s.open ? `<br><span class="fp-dim">${s.open} open</span>` : ''}</td>
          <td>${esc(s.slow_months.join(' ') || '—')}</td></tr>`).join('')}`)}
      <ul class="fc-read">${T.suppliers.map(s => `<li>${esc(s.plain)}</li>`).join('')}</ul>
      <div class="sub-t">How accurate is it? ${trustPill(bt.grade)} ${trainedOn('synthetic')}</div>
      <ul class="fc-read"><li>${esc(bt.plain.headline)}</li><li>${esc(bt.plain.coverage)}</li><li>${esc(bt.plain.open)}</li></ul>
      <div class="sub-t">Does it find what is really there?</div>
      ${checksList(T.recovery.rows)}`,
  };
}

function secSeason(T) {
  if (!T.available) return { title: 'Slow months', html: `<div class="empty">${esc(T.reason)}</div>` };
  const shade = f => {
    const d = Math.max(-0.3, Math.min(0.5, f - 1));
    return d >= 0 ? `background: rgba(212,168,75,${(0.1 + d * 1.4).toFixed(2)})` : `background: rgba(63,176,196,${(0.08 - d).toFixed(2)})`;
  };
  const months = (T.suppliers[0] || { months: [] }).months.map(m => m.label);
  const routes = [...new Set(T.suppliers.map(s => s.route))].map(r => ({
    route: r, months: T.suppliers.find(s => s.route === r).months,
    names: T.suppliers.filter(s => s.route === r).map(s => s.name),
  }));
  return {
    title: 'Slow months',
    html: `
      ${tbl(`<tr><th>Route</th>${months.map(m => `<th class="num">${esc(m)}</th>`).join('')}</tr>
        ${routes.map(r => `<tr><td><b>By ${esc(r.route)}</b><br><span class="fp-dim">${esc(r.names.join(', '))}</span></td>
          ${r.months.map(m => `<td class="num"><span class="st-month" style="${shade(m.factor)}" title="${esc(m.label)}: ×${fmt(m.factor, 2)}">${fmt(m.factor, 2)}</span></td>`).join('')}</tr>`).join('')}`)}
      <div class="fp-callout">A quote is planned the same all year. Where a route is slow for a season, the reorder point for an order placed ahead of it rises with it, so the store orders earlier before the wet months instead of rush-buying in them.</div>`,
  };
}

function secMovements(M, F) {
  return {
    title: 'Movements',
    html: `
      ${chips('mvtype', MOVE_FILTERS, F.moveType)}
      ${F.matnr ? chips('mvmine', [['0', 'Every material'], ['1', `Only ${F.matnr}`]], F.moveMine ? '1' : '0') : ''}
      ${tbl(`<tr><th>Document</th><th>Date</th><th>Type</th><th>Material</th><th class="num">Quantity</th><th>Cost centre</th><th>Order</th><th>Block</th><th>PO</th></tr>
        ${M.rows.map(m => `<tr class="fp-row-map" data-mat="${esc(m.matnr)}"><td>${esc(m.mblnr)}</td><td>${esc(m.budat)}</td>
          <td title="${esc(M.movement_types[m.bwart] || '')}">${esc(m.bwart)}</td><td>${esc(m.maktx)}</td>
          <td class="num ${m.shkzg === 'S' ? 'pos' : ''}">${m.shkzg === 'S' ? '+' : '−'}${esc(qty(m.menge, m.meins))}</td>
          <td>${esc(m.kostl)}</td><td>${esc(m.aufnr)}</td><td>${esc(m.block_code)}</td><td>${esc(m.ebeln)}</td></tr>`).join('')}`)}
      <p class="fp-dim">Showing ${M.rows.length} of ${n0(M.total)}. ${Object.entries(M.movement_types).map(([k, v]) => `${k} ${v}`).join(' · ')}.</p>`,
  };
}

function secPOs(P, rush) {
  return {
    title: rush ? 'Rush buys' : 'Purchase orders',
    html: `
      ${rush ? `<div class="kpis"><div class="kpi warn"><b>${P.total}</b><span>rush buys in 24 months</span></div>
        <div class="kpi"><b>${rp(P.rows.reduce((a, r) => a + r.menge * r.netpr * r.premium_pct / (100 + r.premium_pct), 0))}</b><span>paid over the normal price, on those shown</span></div></div>` : ''}
      ${tbl(`<tr><th>PO</th><th>Placed</th><th>Material</th><th>Supplier</th><th class="num">Quantity</th>
          ${rush ? '<th class="num">Premium</th>' : '<th class="num">Quoted</th><th class="num">Took</th><th class="num">Late by</th><th>Status</th>'}</tr>
        ${P.rows.map(p => `<tr class="fp-row-map" data-mat="${esc(p.matnr)}"><td>${esc(p.ebeln)}</td><td>${esc(p.bedat)}</td><td>${esc(p.maktx)}</td>
          <td>${esc(p.supplier)}</td><td class="num">${esc(qty(p.menge, p.meins))}</td>
          ${rush ? `<td class="num neg">+${fmt(p.premium_pct, 0)}%</td>`
            : `<td class="num">${p.quoted_days} d</td><td class="num">${p.lead_days === null ? '<span class="fp-dim">at sea</span>' : `${p.lead_days} d`}</td>
               <td class="num ${p.late_days > 14 ? 'neg' : ''}">${p.late_days === null ? '—' : `${p.late_days > 0 ? '+' : ''}${p.late_days} d`}</td>
               <td>${esc(p.status)}</td>`}</tr>`).join('')}`)}
      <p class="fp-dim">Showing ${P.rows.length} of ${n0(P.total)}.</p>`,
  };
}

function secReplay(A) {
  const R = A.replay;
  if (!R.available) return { title: 'The replay', html: `<div class="empty">${esc(R.reason)}</div>` };
  const mats = Object.values(R.materials);
  return {
    title: 'The replay',
    html: `
      ${hero(`${R.improvement_pct === null ? '—' : `${fmt(R.improvement_pct, 1)}%`}`, 'less, where the learned points are used',
        esc(R.plain.headline), `<p class="fp-dim">${esc(R.plain.check)} ${trustPill(R.grade)}</p>`)}
      <div class="sub-t">By group</div>
      ${tbl(`<tr><th>Group</th><th class="num">Target</th><th class="num">SAP: cycles clear</th><th class="num">Learned: cycles clear</th>
          <th class="num">SAP: rush buys</th><th class="num">Learned: rush buys</th><th class="num">SAP: cost</th><th class="num">Learned: cost</th><th class="num">Using learned</th></tr>
        ${Object.values(R.groups).map(g => `<tr><td><b>${esc(g.label)}</b></td><td class="num">${g.target_service_pct}%</td>
          <td class="num">${g.old_service_pct}%</td><td class="num"><b>${g.new_service_pct}%</b></td>
          <td class="num">${g.old_rush_orders}</td><td class="num"><b>${g.new_rush_orders}</b></td>
          <td class="num">${rp(g.old_cost_idr)}</td><td class="num"><b>${rp(g.new_cost_idr)}</b></td>
          <td class="num">${g.materials_better} of ${g.materials}</td></tr>`).join('')}`)}
      <div class="sub-t">Material by material</div>
      ${tbl(`<tr><th>Material</th><th class="num">SAP: rush</th><th class="num">Learned: rush</th><th class="num">SAP: clear</th><th class="num">Learned: clear</th>
          <th class="num">SAP: cost</th><th class="num">Learned: cost</th><th>Result</th></tr>
        ${mats.map(m => `<tr class="fp-row-map" data-mat="${esc(m.matnr)}"><td><b>${esc(m.maktx)}</b></td>
          <td class="num">${m.old.rush_orders}</td><td class="num">${m.new.rush_orders}</td>
          <td class="num">${m.old.service_pct}%</td><td class="num">${m.new.service_pct}%</td>
          <td class="num">${rp(m.old.total_cost_idr)}</td><td class="num">${rp(m.new.total_cost_idr)}</td>
          <td>${m.better ? '<span class="chip pos">learned used</span>' : '<span class="chip">SAP settings stay</span>'}</td></tr>`).join('')}`)}
      <ul class="fc-read">${mats.map(m => `<li>${esc(m.plain)}</li>`).join('')}</ul>`,
  };
}

function secChecks(A) {
  const lb = A.leadtime.backtest, cb = A.consumption.backtest;
  return {
    title: 'Backtests and checks',
    html: `
      <div class="fc-trustrow">${A.trust.map(t => `<div class="fc-trustcard wide">
          <div class="fc-card-h"><span>${esc(t.title)}</span>${trustPill(t.grade)}</div>
          <p class="fc-lead">${esc(t.headline)}</p><p>${esc(t.detail)}</p>${trainedOn(t.trained_on)}</div>`).join('')}</div>
      <div class="sub-t">Lead times, month by month</div>
      ${tbl(`<tr><th>Cutoff</th><th class="num">Orders after it</th><th class="num">Off by, learned</th><th class="num">Off by, the quote</th></tr>
        ${lb.folds.map(f => `<tr><td>${esc(f.origin)}</td><td class="num">${f.orders}</td><td class="num"><b>${f.model_error_days} d</b></td><td class="num">${f.quote_error_days} d</td></tr>`).join('')}`)}
      <div class="sub-t">Use over the lead time, material by material</div>
      ${tbl(`<tr><th>Material</th><th>Kind</th><th class="num">Window</th><th class="num">Off by, forecast</th><th class="num">Off by, SAP's average</th><th>Grade</th></tr>
        ${Object.values(cb.materials).map(m => `<tr><td><b>${esc(m.maktx)}</b></td><td>${esc(m.kind)}</td><td class="num">${m.horizon_days} d</td>
          <td class="num"><b>${m.model_error_pct}%</b>${m.croston_error_pct !== undefined ? `<br><span class="fp-dim">Croston ${m.croston_error_pct}%</span>` : ''}</td>
          <td class="num">${m.old_error_pct}%</td><td>${trustPill(m.grade)}</td></tr>`).join('')}`)}
      <p class="fp-dim">${esc(cb.plain.by_kind)}</p>
      <div class="sub-t">Does the record add up?</div>
      ${A.ledger && A.ledger.available ? `<ul class="fc-checks">${Object.entries(A.ledger.checks).map(([k, ok]) => `<li>${ok ? '<span class="fc-mark ok">✓</span>' : '<span class="fc-mark no">✗</span>'}<span>${esc(k.replace(/_/g, ' '))}</span></li>`).join('')}</ul>
        <p class="fp-dim">${esc(A.ledger.plain)}</p>` : ''}
      <div class="sub-t">Does it find what is really there?</div>
      <div class="fc-checkgroup"><div class="fp-card-t">Lead times</div>${checksList(A.leadtime.recovery.rows)}</div>
      <div class="fc-checkgroup"><div class="fp-card-t">Handling and storage losses</div>${checksList(A.consumption.recovery.rows)}</div>
      <div class="sub-t">The rules</div>
      <ol class="fc-steps">${A.rules.map(r => `<li>${esc(r)}</li>`).join('')}</ol>`,
  };
}

function secHow(A, O) {
  return {
    title: 'How it works',
    html: `
      ${['leadtime', 'consumption', 'safety_stock'].map(k => `<div class="fc-howblock"><h4>${esc(A.explain[k].title)} <span class="fp-dim">${esc(A.explain[k].question)}</span></h4>
        ${explainBlock(A.explain[k])}</div>`).join('')}
      <div class="sub-t">What makes it real: five standard SAP reports</div>
      ${tbl(`<tr><th>Report</th><th>Gives</th><th>Replaces</th></tr>
        ${A.real_data.map(r => `<tr><td><b>${esc(r.report)}</b></td><td>${esc(r.gives)}</td><td class="fp-dim">${esc(r.replaces)}</td></tr>`).join('')}`)}
      <div class="sheet-note">${esc(O.note)}</div>`,
  };
}
