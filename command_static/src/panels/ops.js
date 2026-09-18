/* The operating rhythm, as feature windows.
 *
 * Harvesting, pruning, weeding and spraying, pest control and transport are
 * one definition parameterised by operation, because underneath they are the
 * same row: what was planned, what was done, by whom, on which block. Each
 * opens in its own window (shell/popup.js) with a section menu:
 *
 *   Tomorrow   The plan · Not reached · Every block
 *   Ledger     Summary · What drove the misses · Week by week · Slippage ·
 *              By crew · Orders
 *   Why        Objective and constraint · What contiguity costs ·
 *              Demand ranking · Assumptions used
 *   Forecast   What the forecasts say for this plan (panels/forecasts.js)
 *
 * The assumption register and Did-it-work open the same way.
 *
 * Every figure on screen came from the server. This module holds the
 * payloads, the ledger filters and the edits made to tomorrow's plan; it
 * never computes a tonne or a rupiah itself.
 */
import { esc, fmt, idr } from '../lib/fmt.js';
import { getJSON } from '../lib/api.js';
import { MAP_ICON } from '../shell/popup.js';
import { S } from '../state/store.js';
import { recordDecision } from './decisions.js';
import { opsForecastSection, planForecastStrip } from './forecasts.js';
import { openPanel } from './registry.js';

/* ── state ────────────────────────────────────────────────────────────── */

/* Per operation: the last ledger and plan payloads, the ledger filters, the
   edits made to the plan, and the edits the current plan was run with. Kept
   on the store so decisions.js can read the plan when an assignment is
   drafted, and so a window reopens exactly as it was left. */
function state(op) {
  S.ops = S.ops || {};
  if (!S.ops[op]) {
    S.ops[op] = { plan: null, ledger: null, filters: {}, edits: { crews: {} }, applied: null };
  }
  return S.ops[op];
}

const editsKey = e => JSON.stringify({
  crews: e.crews || {}, rain: e.rain_mm ?? null, date: e.date || null,
  exclude: e.exclude_blocks || [],
});

const n0 = v => (v === null || v === undefined) ? '—' : fmt(v, 0);
const n1 = v => (v === null || v === undefined) ? '—' : fmt(v, 1);
const n2 = v => (v === null || v === undefined) ? '—' : fmt(v, 2);
const pc = v => (v === null || v === undefined) ? '—' : `${fmt(v, 1)}%`;
const pc0 = v => (v === null || v === undefined) ? '' : `${fmt(v, 0)}%`;
const mIdr = v => (v === null || v === undefined) ? '—' : `${fmt(v / 1e6, 1)}M`;
const cap = s => s ? s[0].toUpperCase() + s.slice(1) : s;
const words = s => String(s || '').replace(/_/g, ' ');
const over = v => (v === null || v === undefined) ? '—'
  : v > 0 ? `<b class="neg">+${v}</b>` : String(v);

function bar(pct) {
  if (pct === null || pct === undefined) return '—';
  const w = Math.max(0, Math.min(100, pct));
  const cls = pct >= 85 ? 'ok' : pct >= 65 ? 'mid' : 'low';
  return `<span class="adh ${cls}"><i style="width:${w}%"></i><b>${fmt(pct, 0)}%</b></span>`;
}

function qs(obj) {
  const p = Object.entries(obj).filter(([, v]) => v !== null && v !== undefined && v !== '');
  return p.length ? '?' + p.map(([k, v]) => `${k}=${encodeURIComponent(v)}`).join('&') : '';
}

/* Rows and buttons that name blocks carry data-map; the popup shell wires
   them to Show on map. */
function mapAttr(labels, caption) {
  const ls = (labels || []).filter(Boolean);
  return ls.length ? ` data-map="${esc(ls.join(','))}" data-map-caption="${esc(caption || '')}"` : '';
}
function mapBtn(labels, caption) {
  const ls = (labels || []).filter(Boolean);
  return ls.length
    ? `<button class="fp-map-btn"${mapAttr(ls, caption)} title="Show on map" aria-label="Show on map">${MAP_ICON}</button>`
    : '';
}

const tbl = inner => `<div class="fp-tbl"><table class="tbl">${inner}</table></div>`;
const tonnesOp = op => op === 'harvest' || op === 'dispatch';

/* ── data ─────────────────────────────────────────────────────────────── */

async function loadLedger(op) {
  const st = state(op);
  st.ledger = await getJSON(`/gis/ops/${op}/ledger${qs({ ...st.filters, limit: 150 })}`);
  return st.ledger;
}

async function loadPlan(op) {
  const st = state(op);
  const e = st.edits;
  const body = {};
  if (e.crews && Object.keys(e.crews).length) body.crews = e.crews;
  if (e.rain_mm !== undefined && e.rain_mm !== null && e.rain_mm !== '') body.rain_mm = Number(e.rain_mm);
  if (e.exclude_blocks && e.exclude_blocks.length) body.exclude_blocks = e.exclude_blocks;
  if (e.date) body.date = e.date;
  st.plan = Object.keys(body).length
    ? await getJSON(`/gis/ops/${op}/plan`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
      })
    : await getJSON(`/gis/ops/${op}/plan`);
  st.applied = editsKey(e);
  return st.plan;
}

/* ── the operation windows ────────────────────────────────────────────── */

/* The weeding window also carries spraying: one upkeep menu in the
   manifest, two operations in the ledger, and a spray round that stops at
   15 mm of rain is worth its own view rather than a column. */
const VARIANTS = {
  ops_weed: [{ op: 'weed', label: 'Weeding' }, { op: 'spray', label: 'Spraying' }],
};

export function opsPopup(key) {
  S.opsVariant = S.opsVariant || {};
  S.opsSection = S.opsSection || {};
  const base = key.replace(/^ops_/, '');
  const cur = () => S.opsVariant[key] || base;

  return {
    key,
    initial: S.opsSection[key] || 'plan',
    async load() {
      const op = cur();
      await Promise.all([loadLedger(op), loadPlan(op)]);
    },
    header: () => opsHeader(key, cur()),
    wireHeader(host, api) {
      host.querySelectorAll('[data-variant]').forEach(b => {
        b.onclick = () => {
          if (b.dataset.variant === cur()) return;
          S.opsVariant[key] = b.dataset.variant;
          api.reload();
        };
      });
    },
    menu: () => opsMenu(cur()),
    section: id => opsSection(cur(), id),
    wire: (root, api) => wireOps(cur(), root, api),
    onSection: id => { S.opsSection[key] = id; },
  };
}

function opsHeader(key, op) {
  const st = state(op);
  const P = st.plan, L = st.ledger;
  const w = (L && L.window) || (P && P.window);
  const variants = VARIANTS[key];
  return `
    ${variants ? `<div class="fp-seg" role="group" aria-label="Operation">
      ${variants.map(v => `<button class="${v.op === op ? 'on' : ''}" data-variant="${esc(v.op)}"
        aria-pressed="${v.op === op}">${esc(v.label)}</button>`).join('')}</div>` : ''}
    ${w ? `<span class="prov synthetic" title="The ledger is generated, over exactly the window of your export">
        ledger ${esc(w.from)} to ${esc(w.to)}</span>` : ''}
    ${P && P.available ? `<span class="prov scheduled" title="Every figure on the plan is scheduled, never recorded">
        plan ${esc(P.date)}${P.is_tomorrow ? '' : ' · replay'}</span>` : ''}`;
}

function opsMenu(op) {
  const st = state(op);
  const P = st.plan && st.plan.available ? st.plan : null;
  const L = st.ledger && st.ledger.available ? st.ledger : null;
  const crewLabel = (P && P.crew_label) || 'crew';
  const gap = P ? P.why.objective.gap_pct : null;
  const contig = P ? P.why.objective.contiguity : null;
  return [
    { label: P && !P.is_tomorrow ? `Plan · ${P.date}` : 'Tomorrow', items: [
      { id: 'plan', label: 'The plan', count: P ? `${P.totals.crews_with_work}/${P.totals.crews}` : '' },
      { id: 'unreached', label: 'Not reached', count: P ? P.not_reached.blocks : '',
        alert: !!(P && P.not_reached.blocks) },
      { id: 'blocks', label: 'Every block', count: P ? P.totals.blocks : '' },
    ] },
    { label: 'Ledger', items: [
      { id: 'summary', label: 'Summary', count: L ? pc0(L.totals.adherence_pct) : '',
        alert: !!(L && L.totals.adherence_pct !== null && L.totals.adherence_pct < 75) },
      { id: 'drivers', label: 'What drove the misses' },
      { id: 'weeks', label: 'Week by week', count: L ? L.by_week.length : '' },
      { id: 'slippage', label: 'Slippage', count: L ? n0(L.slippage.chained_orders) : '' },
      { id: 'crews', label: `By ${crewLabel}`, count: L ? L.by_crew.length : '' },
      { id: 'orders', label: 'Orders', count: L ? n0(L.matched) : '' },
    ] },
    { label: 'Why', items: [
      { id: 'objective', label: 'Objective and constraint',
        count: gap !== null && gap !== undefined ? `${fmt(gap, 1)}%` : '' },
      { id: 'contiguity', label: 'What contiguity costs',
        count: contig && contig.cost_pct !== undefined && contig.cost_pct !== null ? `${fmt(contig.cost_pct, 1)}%` : '' },
      { id: 'ranking', label: 'Demand ranking', count: P ? P.why.ranking.length : '' },
      { id: 'assumptions', label: 'Assumptions used', count: P ? P.assumptions_used.length : '' },
    ] },
    { label: 'Forecast', items: [
      { id: 'forecast', label: 'What the forecasts say', count: forecastCount(P),
        alert: !!(P && P.forecast && ((P.forecast.spray_call && !P.forecast.spray_call.go)
          || (P.forecast.work_done && P.forecast.work_done.in_use && P.forecast.work_done.expected_pct < 65))) },
    ] },
  ];
}

function forecastCount(P) {
  const f = P && P.forecast;
  if (!f) return '';
  if (f.spray_call) return f.spray_call.go ? 'spray' : 'hold';
  if (f.work_done && f.work_done.in_use) return `${f.work_done.expected_pct}%`;
  if (f.rain && f.rain.in_use) return `${f.rain.chances.heavy.pct}% rain`;
  return '';
}

function opsSection(op, id) {
  const st = state(op);
  const P = st.plan, L = st.ledger;
  const ledgerIds = ['summary', 'drivers', 'weeks', 'slippage', 'crews', 'orders'];
  if (ledgerIds.includes(id)) {
    if (!L || !L.available) return { title: 'Ledger', html: `<div class="empty">${esc((L && L.reason) || 'No ledger.')}</div>` };
    const f = filterBar(op, L);
    const s = { summary: secSummary, drivers: secDrivers, weeks: secWeeks, slippage: secSlippage,
                crews: secCrews, orders: secOrders }[id](op, L);
    return { ...s, html: f + s.html };
  }
  if (!P || !P.available) return { title: 'Plan', html: `<div class="empty">${esc((P && P.reason) || 'No plan.')}</div>` };
  if (id === 'forecast') return opsForecastSection(P);
  return ({ plan: secPlan, unreached: secUnreached, blocks: secBlocks, objective: secObjective,
            contiguity: secContiguity, ranking: secRanking, assumptions: secAssumptionsUsed }[id]
          || secPlan)(op, P);
}

/* ── tomorrow ─────────────────────────────────────────────────────────── */

function secPlan(op, P) {
  const st = state(op);
  const t = P.totals, wx = P.weather, unit = P.unit, isT = tonnesOp(op);
  const crewLabel = P.crew_label;
  const isVeh = op === 'dispatch';
  const dirty = editsKey(st.edits) !== st.applied;
  const hasEdits = editsKey(st.edits) !== editsKey({ crews: {} });

  const kpis = `<div class="kpis">
    <div class="kpi"><b>${t.crews_with_work} of ${t.crews}</b><span>${esc(crewLabel)}s on shift</span></div>
    ${isVeh ? '' : `<div class="kpi"><b>${n0(t.present)} of ${n0(t.on_roll)}</b><span>present</span></div>`}
    <div class="kpi"><b>${n0(t.blocks)}</b><span>blocks</span></div>
    <div class="kpi"><b>${n0(t.ha)}</b><span>ha</span></div>
    <div class="kpi"><b>${isT ? `${n1(t.tonnes)} t` : `${n0(t.qty)} ${esc(unit)}`}</b><span>planned</span></div>
    <div class="kpi"><b>${isT ? `~${n1(t.expected_tonnes)} t` : `~${n0(t.expected_qty)} ${esc(unit)}`}</b>
      <span>expected at ${pc(wx.expected_adherence_pct)}${t.expected_qty_low !== null && t.expected_qty_low !== undefined && t.qty
        ? `, likely ${fmt(100 * t.expected_qty_low / t.qty, 0)} to ${fmt(100 * t.expected_qty_high / t.qty, 0)}%` : ''}</span></div>
  </div>
  ${planForecastStrip(P)}`;

  const rainVal = st.edits.rain_mm !== undefined && st.edits.rain_mm !== null
    ? st.edits.rain_mm : (wx.rain_mm === null || wx.rain_mm === undefined ? '' : wx.rain_mm);
  const controls = `<div class="fp-controls">
    <label>Plan for
      <input type="date" data-plan-date value="${esc(st.edits.date || P.date)}"
             min="${esc(P.window.from)}" max="${esc(P.window.tomorrow)}"></label>
    <label>Rain on the day
      <span class="inl"><input type="number" min="0" max="200" step="1" data-rain value="${esc(rainVal)}" placeholder="mm"> mm</span></label>
    <div class="fp-dim">${wx.forecast
        ? `${esc(wx.forecast.headline)} Type an amount to plan for that rain instead.`
        : esc(wx.source)}${wx.stops_work ? ` · <b class="neg">${esc(wx.reason)}</b>` : ''}<br>
      Expected done ${pc(wx.expected_adherence_pct)}: ${esc(wx.basis)}.
      ${P.is_tomorrow ? '' : (wx.recorded_mm !== null && wx.recorded_mm !== undefined
        ? ` A replay decides on what was knowable the evening before; ${fmt(wx.recorded_mm, 0)} mm actually fell.`
        : ' A date inside the ledger replays the plan against what happened that day.')}</div>
  </div>`;

  const rows = P.crews.map(c => {
    const e = st.edits.crews[c.crew_code] || {};
    const present = e.present !== undefined ? e.present : c.present;
    const out = !!e.exclude;
    const tt = c.totals;
    return `<tr class="${out ? 'out' : ''} ${c.edited ? 'edited' : ''}" data-crew="${esc(c.crew_code)}">
      <td class="crew"><b>${esc(c.crew_code)}</b>
        <span class="fp-dim">${c.division_code ? `div ${esc(c.division_code)}` : esc(c.vehicle_class || '')}</span></td>
      <td class="num">${isVeh ? `${n0(c.capacity_md)} loads`
        : `<input type="number" class="present" min="0" max="${c.on_roll}" step="1" value="${present}"
             data-present="${esc(c.crew_code)}" ${out ? 'disabled' : ''} aria-label="${esc(c.crew_code)} present"> / ${c.on_roll}
           ${c.cutters !== null && c.cutters !== undefined ? `<span class="fp-dim">${c.cutters} cut</span>` : ''}
           ${c.present_low !== null && c.present_low !== undefined && !out
             ? `<br><span class="fp-dim" title="${esc((c.turnout_drivers || []).join(' '))}">likely ${c.present_low} to ${c.present_high}</span>` : ''}
           ${c.recorded_present !== null && c.recorded_present !== undefined && !P.is_tomorrow && !out
             ? `<br><span class="fp-dim">${c.recorded_present} came</span>` : ''}`}</td>
      <td class="num"><input type="checkbox" data-exclude="${esc(c.crew_code)}" ${out ? 'checked' : ''}
          title="Take this ${esc(crewLabel)} out of the plan" aria-label="Take ${esc(c.crew_code)} out"></td>
      <td class="blocks">${out ? '<span class="fp-dim">taken out</span>' : c.blocks.length
        ? `<b>${esc(c.range_label)}</b><br><span class="fp-dim">${c.block_labels.map(esc).join(', ')}</span>`
        : '<span class="fp-dim">nothing reached</span>'}</td>
      <td class="num">${n1(tt.ha)}</td>
      <td class="num">${tt.max_days_over_round > 0 ? `<b class="neg">+${tt.max_days_over_round} d</b>`
        : (tt.blocks ? '<span class="pos">on round</span>' : '—')}</td>
      <td class="num">${isT ? `${n1(tt.tonnes)} t` : `${n0(tt.qty)} ${esc(unit)}`}
        ${tt.expected_qty_low !== null && tt.expected_qty_low !== undefined && tt.qty
          ? `<br><span class="fp-dim">~${fmt(100 * tt.expected_qty / tt.qty, 0)}% likely done</span>` : ''}</td>
      <td class="num">${bar(c.utilisation_pct)}${c.speed_factor && Math.abs(c.speed_factor - 1) >= 0.03
        ? `<br><span class="fp-dim" title="Learned from this crew's records">${c.speed_factor > 1 ? 'faster' : 'slower'} ${fmt(Math.abs(100 * (c.speed_factor - 1)), 0)}%</span>` : ''}</td>
      <td class="num">${out ? '' : mapBtn(c.block_labels, `${c.crew_code}, ${c.range_label || 'plan'}`)}</td>
    </tr>`;
  }).join('');

  const crewTable = tbl(`
    <tr><th>${esc(cap(crewLabel))}</th><th class="num">${isVeh ? 'Capacity' : 'Present'}</th><th class="num">Out</th>
        <th>Blocks</th><th class="num">ha</th><th class="num">Over round</th>
        <th class="num">${isT ? 'Tonnes' : esc(cap(unit))}</th><th class="num">Used</th><th></th></tr>
    ${rows}`).replace('class="tbl"', 'class="tbl ops-plan"');

  const decide = {
    use_case: `Operations, ${P.label.toLowerCase()} assignment`,
    title: `${P.label} assignment for ${P.date}`,
    subject: `${t.blocks} blocks, ${t.ha} ha, ${t.crews_with_work} ${crewLabel}s`,
    action: 'accepted',
    artifact_kind: `${{ harvest: 'harvest', prune: 'upkeep', weed: 'upkeep', spray: 'upkeep', pest: 'pest', dispatch: 'dispatch' }[op]}_assignment`,
    ops_plan: op,
    evidence: [P.headline, P.why.binding_constraint.text],
  };
  const reject = {
    use_case: `Operations, ${P.label.toLowerCase()} assignment`,
    title: `Hold ${P.label.toLowerCase()} plan for ${P.date}`, subject: 'assignment', action: 'rejected',
    evidence: ["Manager keeps the mandor's own allocation"],
  };

  const actions = `<div class="fp-actbar">
      <button class="ops-btn primary" data-replan>${dirty ? 'Re-run with these edits' : 'Re-run plan'}</button>
      <button class="ops-btn" data-reset-edits ${hasEdits ? '' : 'disabled'}>Reset edits</button>
      <span class="fp-dim" data-replan-note>${dirty ? 'Edits not yet applied.' : ''}</span>
      <span class="grow"></span>
      <span class="acts" data-act-scope>
        <button class="accept" data-decide='${JSON.stringify(decide).replace(/'/g, '&#39;')}'>Draft the assignment</button>
        <button data-decide='${JSON.stringify(reject).replace(/'/g, '&#39;')}'>Reject</button>
      </span>
    </div>
    <div class="stub"></div>`;

  return {
    title: P.is_tomorrow ? `Tomorrow, ${P.date_label}` : `Replay, ${P.date_label}`,
    lead: P.headline,
    blocks: P.crews.flatMap(c => c.block_labels),
    blocksCaption: `${P.label} plan for ${P.date}`,
    html: `${kpis}${controls}${crewTable}${actions}
      ${P.not_reached.blocks ? `<div class="fp-callout">
        <b>${n0(P.not_reached.blocks)} blocks due are not reached</b>, deferring costs
        ${mIdr(P.not_reached.deferral_cost_idr_per_week)} IDR this week.
        ${esc(P.why.binding_constraint.text)}
        <button class="ops-btn" data-go="unreached">See what is left</button>
        <button class="ops-btn" data-go="ranking">See the ranking</button></div>` : ''}
      <div class="sheet-note"><span class="prov scheduled">scheduled</span> ${esc(P.provenance)}<br><br>${esc(P.note)}</div>`,
  };
}

function secUnreached(op, P) {
  const nr = P.not_reached, unit = P.unit;
  const qty = v => unit === 'ha' || unit === 'tonnes' ? n1(v) : n0(v);
  return {
    title: 'Due, and not reached',
    lead: `What the ${P.crew_label}s cannot get to on ${P.date}, and what waiting costs. The table shows the costliest ${nr.top.length}; the map button outlines all ${nr.blocks}.`,
    blocks: nr.block_labels || nr.top.map(b => b.block_label),
    blocksCaption: 'Not reached',
    html: `<div class="kpis">
        <div class="kpi ${nr.blocks ? 'warn' : ''}"><b>${n0(nr.blocks)}</b><span>blocks due, not reached</span></div>
        <div class="kpi"><b>${n0(nr.ha)}</b><span>ha</span></div>
        <div class="kpi"><b>${n1(nr.tonnes_at_risk)} t</b><span>${tonnesOp(op) ? 'ripe, waiting' : 'yield at risk'}</span></div>
        <div class="kpi warn"><b>${mIdr(nr.deferral_cost_idr_per_week)}</b><span>IDR deferring costs this week</span></div>
        <div class="kpi"><b>${n0(nr.overdue)}</b><span>already past the round</span></div>
      </div>
      ${nr.top.length ? tbl(`
        <tr><th>Block</th><th>Div</th><th class="num">Urgency</th><th class="num">Over round</th>
            <th class="num">${esc(cap(unit))}</th><th class="num">Man-days</th><th class="num">IDR per day</th></tr>
        ${nr.top.map(b => `<tr class="${b.urgency >= 1.5 ? 'hi' : ''}"${mapAttr([b.block_label], `Not reached, ${b.block_label}`)}>
          <td>${esc(b.block_label)}</td><td>${esc(b.division_code)}</td>
          <td class="num">${n2(b.urgency)}</td><td class="num">${over(b.days_over_round)}</td>
          <td class="num">${qty(b.qty)}</td><td class="num">${n1(b.man_days)}</td>
          <td class="num">${idr(b.deferral_cost_idr_per_day)}</td></tr>`).join('')}`)
        : '<div class="empty">Everything due is reached.</div>'}
      <div class="blk-cap">Click a row to see the block on the map. Urgency is days since the last round over the target round.</div>
      <div class="sheet-note">${esc(P.why.binding_constraint.text)}</div>`,
  };
}

function secBlocks(op, P) {
  const unit = P.unit;
  const blocks = P.crews.flatMap(c => c.blocks.map(b => ({ ...b, crew: c.crew_code })));
  return {
    title: 'Every block on the plan',
    lead: 'In the order each crew is meant to work them. A share marks a block the crew starts but does not finish; the remainder carries to the next order.',
    blocks: blocks.map(b => b.block_label),
    blocksCaption: `All ${blocks.length} planned blocks`,
    html: blocks.length ? tbl(`
      <tr><th>Crew</th><th class="num">#</th><th>Block</th><th>Div</th><th>Activity</th><th class="num">ha</th>
          <th class="num">${esc(cap(unit))}</th><th class="num">Man-days</th><th class="num">Over round</th>
          <th class="num">Next to</th><th class="num">km</th><th class="num">Likely done</th><th>Order</th></tr>
      ${blocks.map(b => `<tr class="${b.partial ? 'partial' : ''}"${mapAttr([b.block_label], `${b.crew}, ${b.block_label}`)}>
        <td>${esc(b.crew)}</td><td class="num">${b.seq}</td><td><b>${esc(b.block_label)}</b></td>
        <td>${esc(b.division_code)}</td><td>${esc(words(b.pest || b.activity))}</td>
        <td class="num">${n1(b.planted_ha)}</td>
        <td class="num">${unit === 'ha' || unit === 'tonnes' ? n1(b.qty) : n0(b.qty)}${b.partial
          ? ` <span class="fp-dim">${fmt(b.share * 100, 0)}%</span>` : ''}</td>
        <td class="num">${n1(b.man_days)}</td><td class="num">${over(b.days_over_round)}</td>
        <td class="num">${b.contiguous ? '<span class="pos">✓</span>' : '—'}</td>
        <td class="num">${n1(b.travel_km)}</td>
        <td class="num" title="${esc((b.risk_drivers || []).join(' '))}">${b.expected_share === null || b.expected_share === undefined ? '—'
          : `${fmt(100 * b.expected_share, 0)}%${b.p_carried >= 0.7 ? ' <span class="chip neg">may need another day</span>' : ''}`}</td>
        <td class="fp-dim">${esc(b.order_ref)}</td></tr>`).join('')}`)
      : '<div class="empty">Nothing is assigned on this plan.</div>',
  };
}

/* ── the ledger ───────────────────────────────────────────────────────── */

function filterBar(op, L) {
  const f = L.filters;
  const crews = [...new Set(L.by_crew.map(c => c.crew_code))].sort();
  const divs = [...new Set(L.by_crew.map(c => c.division_code).filter(Boolean))].sort((a, b) => a - b);
  const opt = (v, curv, label) => `<option value="${esc(v)}" ${String(curv || '') === String(v) ? 'selected' : ''}>${esc(label || v)}</option>`;
  const active = Object.values(f).some(Boolean);
  // Crews and divisions come from the filtered result, so a narrowed ledger
  // still offers the value that narrowed it.
  if (f.crew && !crews.includes(f.crew)) crews.push(f.crew);
  if (f.division && !divs.includes(f.division)) divs.push(f.division);
  return `<div class="fp-filters">
    <label>Crew <select data-filter="crew"><option value="">all</option>${crews.map(c => opt(c, f.crew)).join('')}</select></label>
    <label>Division <select data-filter="division"><option value="">all</option>${divs.map(d => opt(d, f.division)).join('')}</select></label>
    <label>Status <select data-filter="status"><option value="">all</option>${L.statuses.map(s => opt(s, f.status, words(s))).join('')}</select></label>
    ${L.by_activity.length || f.activity ? `<label>Activity <select data-filter="activity"><option value="">all</option>
      ${(L.by_activity.length ? L.by_activity.map(a => a.activity) : [f.activity]).map(a => opt(a, f.activity, words(a))).join('')}</select></label>` : ''}
    <label>From <input type="date" data-filter="date_from" value="${esc(f.date_from || '')}" min="${esc(L.window.from)}" max="${esc(L.window.to)}"></label>
    <label>To <input type="date" data-filter="date_to" value="${esc(f.date_to || '')}" min="${esc(L.window.from)}" max="${esc(L.window.to)}"></label>
    <button class="ops-btn" data-clear-filters ${active ? '' : 'disabled'}>Clear filters</button>
    <span class="fp-filter-n">${active ? `<b>${n0(L.matched)}</b> orders match` : `${n0(L.matched)} orders`} · filters apply to every ledger section</span>
  </div>`;
}

function secSummary(op, L) {
  const t = L.totals, rd = L.slippage.round;
  return {
    title: `${L.label} ledger`,
    lead: `What was worked, when, by whom, planned against actual. Stands in for ${L.stands_in_for}.`,
    html: `<div class="kpis">
        <div class="kpi"><b>${n0(t.days)}</b><span>days</span></div>
        <div class="kpi"><b>${n0(t.orders)}</b><span>orders</span></div>
        <div class="kpi ${t.adherence_pct !== null && t.adherence_pct < 75 ? 'warn' : ''}"><b>${pc(t.adherence_pct)}</b><span>adherence</span></div>
        <div class="kpi"><b>${n0(t.carried_forward)}</b><span>carried forward</span></div>
        <div class="kpi"><b>${n0(t.weathered_off)}</b><span>weathered off</span></div>
        <div class="kpi"><b>${n0(t.not_started)}</b><span>not started</span></div>
      </div>
      <div class="kpis">
        <div class="kpi"><b>${n0(t.planned_qty)}</b><span>${esc(L.unit)} planned</span></div>
        <div class="kpi"><b>${n0(t.actual_qty)}</b><span>${esc(L.unit)} done</span></div>
        <div class="kpi"><b>${n1(t.output_per_man_day)}</b><span>${esc(L.unit)} per man-day</span></div>
        <div class="kpi"><b>${n0(t.man_days_actual)}</b><span>man-days worked</span></div>
        ${t.tonnes_short !== undefined ? `<div class="kpi warn"><b>${n0(t.tonnes_short)} t</b><span>short of plan</span></div>` : ''}
      </div>
      <div class="ops-footer"><b>${esc(L.footer)}</b></div>
      ${rd ? `<div class="fp-callout"><b>The round.</b> ${esc(rd.reading)}<br>
        ${rd.by_month.map(m => `<span class="chip">${esc(m.month)}: ${m.median_interval_days} d</span>`).join(' ')}
        <button class="ops-btn" data-go="slippage">See the chains</button></div>` : ''}
      ${L.by_activity.length ? `<div class="sub-t">By activity</div>${tbl(`
        <tr><th>Activity</th><th class="num">Orders</th><th class="num">Planned</th><th class="num">Done</th>
            <th class="num">Adherence</th><th class="num">Carried</th><th class="num">Rained off</th></tr>
        ${L.by_activity.map(a => `<tr><td>${esc(words(a.activity))}</td><td class="num">${n0(a.orders)}</td>
          <td class="num">${n1(a.planned_qty)}</td><td class="num">${n1(a.actual_qty)}</td>
          <td class="num">${bar(a.adherence_pct)}</td><td class="num">${n0(a.carried_forward)}</td>
          <td class="num">${n0(a.weathered_off)}</td></tr>`).join('')}`)}` : ''}
      <div class="sheet-note"><b>Provenance.</b> ${esc(L.provenance)}<br><br>${esc(L.note)}</div>`,
  };
}

function secDrivers(op, L) {
  const tables = ['rain', 'road', 'attendance'].map(k => {
    const rows = L.drivers[k] || [];
    if (!rows.length) return '';
    const title = { rain: 'Rain on the day', road: 'Road condition', attendance: 'Crew attendance' }[k];
    return `<div class="fp-card"><div class="fp-card-t">${title}</div><table class="tbl">
      <tr><th>Bucket</th><th class="num">Orders</th><th class="num">Adherence</th><th class="num">Rained off</th></tr>
      ${rows.map(r => `<tr><td>${esc(r.bucket)}</td><td class="num">${n0(r.orders)}</td>
        <td class="num">${bar(r.adherence_pct)}</td><td class="num">${n0(r.weathered_off)}</td></tr>`).join('')}
    </table></div>`;
  }).join('');
  return {
    title: 'What drove the misses',
    lead: 'Adherence grouped by the thing that drove the miss. If it were flat across all three, a plan could ignore them.',
    html: `<div class="fp-grid3">${tables}</div><div class="sheet-note">${esc(L.drivers.note)}</div>`,
  };
}

function secWeeks(op, L) {
  return {
    title: 'Week by week',
    lead: 'Planned against actual per week, beside the real rainfall that fell in it.',
    html: tbl(`
      <tr><th>Week of</th><th class="num">Orders</th><th class="num">Planned</th><th class="num">Actual</th>
          <th class="num">Adherence</th><th class="num">Rain mm</th><th class="num">Rained off</th><th class="num">Carried</th></tr>
      ${L.by_week.map(wk => `<tr class="${wk.adherence_pct !== null && wk.adherence_pct < 60 ? 'hi' : ''}">
        <td>${esc(wk.week)}</td><td class="num">${n0(wk.orders)}</td>
        <td class="num">${n0(wk.planned_qty)}</td><td class="num">${n0(wk.actual_qty)}</td>
        <td class="num">${bar(wk.adherence_pct)}</td><td class="num">${n0(wk.rain_mm)}</td>
        <td class="num">${n0(wk.weathered_off)}</td><td class="num">${n0(wk.carried_forward)}</td></tr>`).join('')}`),
  };
}

function secSlippage(op, L) {
  const sl = L.slippage, rd = sl.round;
  const chains = sl.longest || [];
  return {
    title: 'Slippage',
    lead: sl.note,
    blocks: chains.map(c => c.block_label),
    blocksCaption: 'Longest carry-forward chains',
    html: `<div class="kpis">
        <div class="kpi"><b>${n0(sl.chained_orders)}</b><span>orders in a chain</span></div>
        <div class="kpi"><b>${n0(sl.chains)}</b><span>chains</span></div>
        <div class="kpi"><b>${n1(sl.mean_chain_days)} d</b><span>mean chain</span></div>
        ${rd ? `<div class="kpi ${rd.stretch_days > 0 ? 'warn' : ''}"><b>${n0(rd.by_month[rd.by_month.length - 1].median_interval_days)} d</b>
          <span>round in ${esc(rd.by_month[rd.by_month.length - 1].month)}, target ${n0(rd.target_days)}</span></div>` : ''}
      </div>
      ${rd ? `<div class="sub-t">The round, month by month</div>${tbl(`
        <tr><th>Month</th><th class="num">Median interval</th><th class="num">Target</th><th class="num">Intervals</th></tr>
        ${rd.by_month.map(m => `<tr class="${m.median_interval_days > rd.target_days ? 'hi' : ''}"><td>${esc(m.month)}</td>
          <td class="num">${m.median_interval_days} d</td><td class="num">${rd.target_days} d</td>
          <td class="num">${n0(m.intervals)}</td></tr>`).join('')}`)}` : ''}
      <div class="sub-t">The longest chains</div>
      ${chains.length ? tbl(`
        <tr><th>Block</th><th>Crew</th><th>Activity</th><th class="num">Orders</th><th class="num">Days</th>
            <th>From</th><th>To</th><th>First order</th><th>Last order</th></tr>
        ${chains.map(c => `<tr class="${c.days >= 7 ? 'hi' : ''}"${mapAttr([c.block_label], `Chain on ${c.block_label}`)}>
          <td>${esc(c.block_label)}</td><td>${esc(c.crew_code)}</td><td>${esc(words(c.activity))}</td>
          <td class="num">${c.orders}</td><td class="num">${c.days}</td><td>${esc(c.from)}</td><td>${esc(c.to)}</td>
          <td class="fp-dim">${esc(c.first_order)}</td><td class="fp-dim">${esc(c.last_order)}</td></tr>`).join('')}`)
        : '<div class="empty">No carry-forward chains in this slice.</div>'}`,
  };
}

function secCrews(op, L) {
  return {
    title: 'By crew',
    lead: 'Worst adherence first. Pick a crew in the filter above to see its orders.',
    html: tbl(`
      <tr><th>Crew</th><th>Div</th><th class="num">Days</th><th class="num">Blocks</th><th class="num">Orders</th>
          <th class="num">Adherence</th><th class="num">${esc(L.unit)} per man-day</th><th class="num">Carried</th>
          <th class="num">Rained off</th><th class="num">Not started</th><th></th></tr>
      ${L.by_crew.map(c => `<tr class="${c.adherence_pct !== null && c.adherence_pct < 65 ? 'hi' : ''}">
        <td><b>${esc(c.crew_code)}</b></td><td>${esc(c.division_code || '')}</td>
        <td class="num">${n0(c.days)}</td><td class="num">${n0(c.blocks)}</td><td class="num">${n0(c.orders)}</td>
        <td class="num">${bar(c.adherence_pct)}</td><td class="num">${n1(c.output_per_man_day)}</td>
        <td class="num">${n0(c.carried_forward)}</td><td class="num">${n0(c.weathered_off)}</td>
        <td class="num">${n0(c.not_started)}</td>
        <td><button class="ops-btn small" data-crew-filter="${esc(c.crew_code)}">Orders</button></td></tr>`).join('')}`),
  };
}

function secOrders(op, L) {
  const labels = [...new Set(L.rows.map(r => r.block_label).filter(Boolean))];
  const multi = L.by_activity.length > 0;
  return {
    title: 'Orders',
    lead: `${L.matched > L.rows.length ? `The most recent ${L.rows.length} of ${n0(L.matched)} matching orders. ` : ''}Click a row to see its block on the map.`,
    blocks: labels,
    blocksCaption: 'Blocks in the orders shown',
    html: tbl(`
      <tr><th>Date</th><th>Crew</th><th>Block</th>${multi ? '<th>Activity</th>' : ''}
          <th class="num">Planned</th><th class="num">Actual</th><th class="num">Adherence</th>
          <th class="num">Men</th><th class="num">Man-days</th><th class="num">Out per md</th>
          <th>Status</th><th class="num">Rain</th><th>Road</th><th>Carried to</th></tr>
      ${L.rows.map(r => `<tr class="${r.status === 'weathered_off' || r.status === 'not_started' ? 'hi' : ''}"${mapAttr([r.block_label], `Order ${r.order_id}`)}>
        <td>${esc(r.date)}</td><td>${esc(r.crew_code)}</td><td>${esc(r.block_label)}</td>
        ${multi ? `<td>${esc(words(r.activity))}</td>` : ''}
        <td class="num">${n1(r.planned_qty)}</td><td class="num">${n1(r.actual_qty)}</td>
        <td class="num">${r.adherence === null ? '—' : bar(r.adherence * 100)}</td>
        <td class="num">${n0(r.headcount_actual)} / ${n0(r.headcount_plan)}</td>
        <td class="num">${n1(r.man_days_actual)}</td><td class="num">${n1(r.output_per_man_day)}</td>
        <td>${esc(words(r.status))}</td><td class="num">${r.rain_mm === null ? '—' : n0(r.rain_mm)}</td>
        <td>${esc(r.road_condition || '')}</td>
        <td class="fp-dim">${r.carried_to ? esc(r.carried_to) : ''}</td></tr>`).join('')}`),
  };
}

/* ── why ──────────────────────────────────────────────────────────────── */

function secObjective(op, P) {
  const W = P.why, O = W.objective;
  return {
    title: 'Objective and binding constraint',
    lead: 'Value recovered per man-day, subject to capacity. Each term is in rupiah and traceable to a figure the app already computes.',
    html: `<div class="kpis">
        <div class="kpi"><b>${mIdr(O.terms.deferral_idr)}</b><span>IDR deferral value taken</span></div>
        <div class="kpi"><b>${mIdr(O.terms.contiguity_idr)}</b><span>IDR contiguity bonus</span></div>
        <div class="kpi"><b>${mIdr(O.terms.travel_idr)}</b><span>IDR travel penalty</span></div>
        <div class="kpi"><b>${mIdr(O.upper_bound_idr)}</b><span>IDR relaxed bound</span></div>
        <div class="kpi ${O.gap_pct !== null && O.gap_pct > 5 ? 'warn' : ''}"><b>${pc(O.gap_pct)}</b><span>gap to bound</span></div>
      </div>
      <div class="fp-callout"><b>Binding constraint.</b> ${esc(W.binding_constraint.text)}</div>
      <div class="sub-t">Method</div>
      <div class="sheet-note" style="margin-top:0">${esc(O.method)}.${O.gap_reading ? ` The gap: ${esc(O.gap_reading)}` : ''}</div>
      <pre class="fp-formula">${esc(O.formula)}</pre>
      <div class="sub-t">Demand and rate</div>
      <div class="sheet-note" style="margin-top:0"><b>Demand signal.</b> ${esc(W.demand_signal)}.<br>
        <b>Rate.</b> ${esc(W.rate)}.</div>`,
  };
}

function secContiguity(op, P) {
  const C = P.why.objective.contiguity;
  if (C.cost_idr === undefined) {
    return { title: 'What contiguity costs', html: `<div class="empty">Contiguity is switched off or nothing was assigned,
      so there is no comparison to make.</div>` };
  }
  return {
    title: 'What contiguity costs',
    lead: 'The same plan run twice: once keeping crews on adjacent blocks, once as a pure ranking. The difference is the price of a plan a mandor will actually follow.',
    html: `<div class="kpis">
        <div class="kpi"><b>${n0(C.pairs_with)}</b><span>adjacent pairs with</span></div>
        <div class="kpi"><b>${n0(C.pairs_without)}</b><span>adjacent pairs without</span></div>
        <div class="kpi warn"><b>${mIdr(C.cost_idr)}</b><span>IDR given up</span></div>
        <div class="kpi"><b>${pc(C.cost_pct)}</b><span>of deferral value</span></div>
      </div>
      ${tbl(`
        <tr><th>Plan</th><th class="num">Adjacent pairs</th><th class="num">Deferral value IDR</th></tr>
        <tr class="good"><td>With contiguity at ${C.weight_pct}% of block value</td><td class="num">${n0(C.pairs_with)}</td>
          <td class="num">${idr(C.deferral_with_idr)}</td></tr>
        <tr><td>Without: pure ranking</td><td class="num">${n0(C.pairs_without)}</td><td class="num">${idr(C.deferral_without_idr)}</td></tr>`)}
      <div class="sheet-note">${esc(C.reading)} The weight is an assumption; set it to zero in the register to see the scattered plan.</div>
      <div class="fp-actbar"><button class="ops-btn" data-open-panel="assumptions">Open the assumption register</button></div>`,
  };
}

function secRanking(op, P) {
  const W = P.why, unit = P.unit;
  return {
    title: 'The demand ranking',
    lead: P.why.objective.discounted_by_work_done
      ? 'Sorted by deferral value per man-day, discounted by how much of each block is likely to get done: the order the greedy pass reads. Rows not taken by any crew are highlighted.'
      : 'Sorted by deferral value per man-day, which is the order the greedy pass reads. Rows not taken by any crew are highlighted.',
    blocks: W.ranking.map(r => r.block_label),
    blocksCaption: `Top ${W.ranking.length} by value per man-day`,
    html: tbl(`
      <tr><th class="num">#</th><th>Block</th><th>Div</th><th>Activity</th><th class="num">Urgency</th><th class="num">Over round</th>
          <th class="num">${esc(cap(unit))}</th><th class="num">Man-days</th><th class="num">IDR per day</th>
          <th class="num">Horizon d</th><th class="num">Likely done</th><th class="num">IDR per man-day</th><th>Taken by</th></tr>
      ${W.ranking.map(r => `<tr class="${r.assigned_to ? '' : 'hi'}"${mapAttr([r.block_label], `Rank ${r.rank}, ${r.block_label}`)}>
        <td class="num">${r.rank}</td><td>${esc(r.block_label)}</td><td>${esc(r.division_code)}</td>
        <td>${esc(words(r.activity))}</td><td class="num">${n2(r.urgency)}</td><td class="num">${over(r.days_over_round)}</td>
        <td class="num">${unit === 'ha' || unit === 'tonnes' ? n1(r.qty) : n0(r.qty)}</td>
        <td class="num">${n1(r.man_days)}</td><td class="num">${idr(r.deferral_cost_idr_per_day)}</td>
        <td class="num">${r.horizon_days}</td>
        <td class="num">${r.expected_share === null || r.expected_share === undefined ? '—' : `${fmt(100 * r.expected_share, 0)}%`}</td>
        <td class="num">${idr(r.value_per_man_day_idr)}</td>
        <td>${r.assigned_to ? esc(r.assigned_to) : '<span class="neg">not reached</span>'}</td></tr>`).join('')}`)
      + `<div class="blk-cap">Rate source for each block is on the plan payload; harvest rates come from the productivity model where it has a fit.</div>`,
  };
}

function secAssumptionsUsed(op, P) {
  return {
    title: 'Assumptions this plan rests on',
    lead: 'Every rupiah and tonne on the plan is computed from these values. Change one in the register and re-run.',
    html: tbl(`
      <tr><th>Assumption</th><th class="num">Value</th><th>Unit</th><th>Source</th></tr>
      ${P.assumptions_used.map(a => `<tr><td>${esc(a.label)}</td><td class="num">${fmt(a.value, a.value % 1 ? 2 : 0)}</td>
        <td>${esc(a.unit)}</td><td>${srcBadge(a.source)}</td></tr>`).join('')}`)
      + `<div class="fp-actbar"><button class="ops-btn" data-open-panel="assumptions">Open the assumption register</button></div>`,
  };
}

/* ── wiring ───────────────────────────────────────────────────────────── */

function wireOps(op, root, api) {
  const st = state(op);

  const markDirty = () => {
    const dirty = editsKey(st.edits) !== st.applied;
    const btn = root.querySelector('[data-replan]');
    const note = root.querySelector('[data-replan-note]');
    const reset = root.querySelector('[data-reset-edits]');
    if (btn) btn.textContent = dirty ? 'Re-run with these edits' : 'Re-run plan';
    if (note) note.textContent = dirty ? 'Edits not yet applied.' : '';
    if (reset) reset.disabled = editsKey(st.edits) === editsKey({ crews: {} });
  };

  const replan = async () => {
    const btn = root.querySelector('[data-replan]');
    if (btn) { btn.disabled = true; btn.textContent = 'Re-running…'; }
    try {
      await loadPlan(op);
      await api.rerender();
    } catch (e) {
      const note = root.querySelector('[data-replan-note]');
      if (note) note.textContent = `Re-run failed: ${e.message}`;
      if (btn) btn.disabled = false;
    }
  };

  const refilter = async () => {
    await loadLedger(op);
    await api.rerender();
  };

  root.querySelectorAll('[data-filter]').forEach(el => {
    el.onchange = () => { st.filters[el.dataset.filter] = el.value || undefined; refilter(); };
  });
  const clear = root.querySelector('[data-clear-filters]');
  if (clear) clear.onclick = () => { st.filters = {}; refilter(); };
  root.querySelectorAll('[data-crew-filter]').forEach(b => {
    b.onclick = async () => {
      st.filters = { ...st.filters, crew: b.dataset.crewFilter };
      await loadLedger(op);
      api.go('orders');
    };
  });

  root.querySelectorAll('[data-present]').forEach(el => {
    el.onchange = () => {
      const code = el.dataset.present;
      st.edits.crews[code] = { ...(st.edits.crews[code] || {}), present: Number(el.value) };
      el.closest('tr').classList.add('edited');
      markDirty();
    };
  });
  root.querySelectorAll('[data-exclude]').forEach(el => {
    el.onchange = () => {
      const code = el.dataset.exclude;
      const next = { ...(st.edits.crews[code] || {}) };
      if (el.checked) next.exclude = true; else delete next.exclude;
      if (Object.keys(next).length) st.edits.crews[code] = next; else delete st.edits.crews[code];
      const row = el.closest('tr');
      if (row) row.classList.toggle('out', el.checked);
      const inp = root.querySelector(`[data-present="${CSS.escape(code)}"]`);
      if (inp) inp.disabled = el.checked;
      markDirty();
    };
  });
  const rain = root.querySelector('[data-rain]');
  if (rain) rain.onchange = () => {
    st.edits.rain_mm = rain.value === '' ? undefined : Number(rain.value);
    markDirty();
  };
  // A different day is a different plan: last day's headcount edits do not
  // carry, so the date resets them and re-runs straight away.
  const day = root.querySelector('[data-plan-date]');
  if (day) day.onchange = () => {
    if (!day.value) return;
    const tomorrow = st.plan && st.plan.window ? st.plan.window.tomorrow : null;
    st.edits = { crews: {}, date: day.value === tomorrow ? undefined : day.value };
    replan();
  };

  const rerun = root.querySelector('[data-replan]');
  if (rerun) rerun.onclick = replan;
  const reset = root.querySelector('[data-reset-edits]');
  if (reset) reset.onclick = () => { st.edits = { crews: {} }; replan(); };

  root.querySelectorAll('[data-decide]').forEach(b => {
    b.onclick = () => recordDecision(JSON.parse(b.dataset.decide), b);
  });
  wireCommon(root, api);
}

function wireCommon(root, api) {
  root.querySelectorAll('[data-go]').forEach(b => { b.onclick = () => api.go(b.dataset.go); });
  root.querySelectorAll('[data-open-panel]').forEach(b => { b.onclick = () => openPanel(b.dataset.openPanel); });
}

/* ── the assumption register ──────────────────────────────────────────── */

function srcBadge(s) {
  const cls = s === 'client' ? 'real'
    : (s === 'literature' || s === 'calibrated' || s === 'derived') ? 'derived' : 'synthetic';
  return `<span class="prov ${cls}">${esc(s)}</span>`;
}

export function assumptionsPopup() {
  let D = null;
  S.asmSection = S.asmSection || 'overview';
  const load = async () => { D = await getJSON('/gis/assumptions'); };

  return {
    key: 'assumptions',
    initial: S.asmSection,
    load,
    header: () => D ? `<span class="prov ${D.overridden ? 'real' : 'synthetic'}">
        ${D.overridden ? `${D.overridden} set by the client` : 'all at their defaults'}</span>` : '',
    menu: () => D ? [
      { label: 'Register', items: [{ id: 'overview', label: 'Overview', count: D.total }] },
      { label: 'Groups', items: D.groups.map(g => ({
        id: g.group, label: cap(g.group),
        count: g.assumptions.some(a => a.overridden)
          ? `${g.assumptions.filter(a => a.overridden).length} edited` : g.assumptions.length,
        alert: g.assumptions.some(a => a.overridden),
      })) },
    ] : [],
    onSection: id => { S.asmSection = id; },
    section(id) {
      if (id === 'overview') {
        const edited = D.assumptions.filter(a => a.overridden);
        return {
          title: 'The assumption register',
          lead: 'Every number a plan is priced with, in one place: value, unit, source, and what depends on it.',
          html: `<div class="kpis">
              <div class="kpi"><b>${D.total}</b><span>assumptions</span></div>
              <div class="kpi ${D.overridden ? 'warn' : ''}"><b>${D.overridden}</b><span>set by the client</span></div>
              ${Object.entries(D.by_source).map(([k, v]) => `<div class="kpi"><b>${v}</b><span>${esc(k)}</span></div>`).join('')}
            </div>
            <div class="sheet-note" style="margin-top:0">${esc(D.note)}</div>
            <div class="sub-t">Groups</div>
            <div class="fp-grid3">${D.groups.map(g => `<button class="fp-tile" data-go="${esc(g.group)}">
              <b>${esc(cap(g.group))}</b>
              <span>${g.assumptions.length} assumptions${g.assumptions.some(a => a.overridden)
                ? ` · <em>${g.assumptions.filter(a => a.overridden).length} edited</em>` : ''}</span>
              <span class="fp-dim">${g.assumptions.slice(0, 3).map(a => esc(a.label)).join(' · ')}${g.assumptions.length > 3 ? ' …' : ''}</span>
            </button>`).join('')}</div>
            <div class="sub-t">Changed from the default</div>
            ${edited.length ? tbl(`<tr><th>Assumption</th><th class="num">Default</th><th class="num">Now</th><th>Unit</th><th>Set</th></tr>
              ${edited.map(a => `<tr class="edited"><td><b>${esc(a.label)}</b></td>
                <td class="num">${fmt(a.default, a.default % 1 ? 2 : 0)}</td><td class="num"><b>${fmt(a.value, a.value % 1 ? 2 : 0)}</b></td>
                <td>${esc(a.unit)}</td><td class="fp-dim">${esc((a.set_at || '').slice(0, 16).replace('T', ' '))} · ${esc(a.set_by || '')}</td></tr>`).join('')}`)
              : '<div class="empty">Nothing changed yet. Open a group and edit a value; it saves as you leave the field.</div>'}
            <div class="sub-t">Sources</div>
            <div class="sheet-note" style="margin-top:0">
              ${srcBadge('literature')} an agronomic or industry figure, cited in its basis ·
              ${srcBadge('calibrated')} back-solved from your own data ·
              ${srcBadge('derived')} computed by a model in this app ·
              ${srcBadge('assumed')} a planning figure with no better source yet ·
              ${srcBadge('client')} set by you here, overriding the default</div>
            <div class="fp-actbar"><button class="ops-btn" data-asm-reset ${D.overridden ? '' : 'disabled'}>Back to defaults</button>
              <span class="fp-dim" data-asm-note></span></div>`,
        };
      }
      const g = D.groups.find(x => x.group === id) || D.groups[0];
      return {
        title: `${cap(g.group)} assumptions`,
        lead: 'Edit a value and it saves as you leave the field. The next plan is priced at it.',
        html: `<div class="fp-tbl"><table class="tbl asm">
            <tr><th>Assumption</th><th class="num">Value</th><th>Unit</th><th>Source</th><th>Basis</th></tr>
            ${g.assumptions.map(a => `<tr class="${a.overridden ? 'edited' : ''}">
              <td class="blk"><b>${esc(a.label)}</b><br><span class="fp-dim">Used by: ${a.used_by.map(esc).join(' · ')}</span></td>
              <td class="num"><input type="number" data-asm="${esc(a.key)}" value="${a.value}" step="${a.value % 1 ? '0.01' : '1'}"
                   ${a.min !== null ? `min="${a.min}"` : ''} ${a.max !== null ? `max="${a.max}"` : ''} ${a.editable ? '' : 'disabled'}
                   aria-label="${esc(a.label)}">
                <br><span class="fp-dim">${a.overridden ? `default ${fmt(a.default, a.default % 1 ? 2 : 0)}` : ''}
                  ${a.min !== null && a.max !== null ? `${a.overridden ? ' · ' : ''}range ${fmt(a.min, a.min % 1 ? 2 : 0)} to ${fmt(a.max, a.max % 1 ? 2 : 0)}` : ''}</span></td>
              <td>${esc(a.unit)}</td>
              <td>${srcBadge(a.source_in_force)}${a.overridden ? `<br><span class="fp-dim">was ${esc(a.source)}</span>` : ''}</td>
              <td class="basis">${esc(a.basis)}</td>
            </tr>`).join('')}
          </table></div>
          <div class="fp-actbar"><span class="fp-dim" data-asm-note></span></div>`,
      };
    },
    wire(root, api) {
      const note = root.querySelector('[data-asm-note]');
      root.querySelectorAll('[data-asm]').forEach(el => {
        el.onchange = async () => {
          el.disabled = true;
          try {
            const r = await fetch('/gis/assumptions', {
              method: 'POST', headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ key: el.dataset.asm, value: Number(el.value) }),
            });
            const out = await r.json();
            if (!r.ok) throw new Error(out.detail || r.statusText);
            el.closest('tr').classList.add('edited');
            await load();
            // Counts in the menu and the header change; the table the user is
            // typing in is left alone so focus survives a tab to the next field.
            api.refreshMenu();
            S.panelHint.assumptions = D.overridden ? `${D.overridden} edited` : `${D.total} to agree`;
            invalidatePlans();
            if (note) note.textContent = `${out.assumption.label} set to ${out.assumption.value} ${out.assumption.unit}. The next plan is priced at it.`;
          } catch (e) {
            if (note) note.textContent = `Not saved: ${e.message}`;
          } finally {
            el.disabled = false;
          }
        };
      });
      const reset = root.querySelector('[data-asm-reset]');
      if (reset) reset.onclick = async () => {
        await fetch('/gis/assumptions', { method: 'DELETE' });
        await load();
        S.panelHint.assumptions = `${D.total} to agree`;
        invalidatePlans();
        api.rerender();
      };
      wireCommon(root, api);
    },
  };
}

/* A changed assumption reprices every plan; drop the held ones so the next
   window opened fetches fresh rather than showing yesterday's rupiah. */
function invalidatePlans() {
  Object.values(S.ops || {}).forEach(st => { st.plan = null; st.applied = null; });
}

/* ── did it work ──────────────────────────────────────────────────────── */

export function outcomesPopup() {
  let D = null;
  S.outSection = S.outSection || 'overview';
  return {
    key: 'outcomes',
    initial: S.outSection,
    async load() { D = await getJSON('/gis/ops/outcomes?estate=EC'); },
    header: () => D ? `<span class="prov synthetic" title="Observed figures are read from the generated ledger">observed side is synthetic</span>` : '',
    menu: () => D ? [{ label: 'Did it work', items: [
      { id: 'overview', label: 'Overview', count: D.realisation_pct === null ? '' : pc0(D.realisation_pct) },
      { id: 'plans', label: 'Accepted plans', count: D.plans.length, alert: D.counts.not_executed > 0 },
      { id: 'baseline', label: 'Baseline by operation', count: D.baseline.length },
      { id: 'forecasts', label: 'Forecasts against the day',
        count: D.forecast_summary && D.forecast_summary.checked ? `${D.forecast_summary.right}/${D.forecast_summary.checked}` : '' },
    ] }] : [],
    onSection: id => { S.outSection = id; },
    section(id) {
      const c = D.counts;
      if (id === 'plans') {
        return {
          title: 'Accepted plans',
          lead: 'Each accepted assignment beside what the ledger recorded on its planned blocks that day. Click a row to see the planned blocks on the map.',
          html: D.plans.length ? tbl(`
            <tr><th>Accepted</th><th>Plan</th><th>Due</th><th class="num">Expected</th><th class="num">Observed</th>
                <th class="num">Realised</th><th>Status</th><th>Blocks worked</th></tr>
            ${D.plans.map(p => {
              const e = p.expected || {}, o = p.observed;
              const ex = e.tonnes ? `${n1(e.tonnes)} t` : `${n0(e.qty)} ${esc(e.unit || '')}`;
              const ob = o ? (o.tonnes !== undefined ? `${n1(o.tonnes)} t` : `${n0(o.qty)} ${esc(e.unit || '')}`) : '—';
              return `<tr class="${p.status === 'not_executed' ? 'hi' : ''}"${mapAttr(e.block_keys || [], p.title)}>
                <td>${esc(p.created_at.slice(0, 16).replace('T', ' '))}</td><td>${esc(p.title)}</td>
                <td>${esc(p.due_date)}</td><td class="num">${ex}</td><td class="num">${ob}</td>
                <td class="num">${p.realisation_pct === null ? '—' : bar(p.realisation_pct)}</td>
                <td>${esc(words(p.status))}</td>
                <td>${o ? `${o.blocks_worked} of ${o.blocks_planned}` : '—'}</td></tr>`;
            }).join('')}`)
            + `<div class="sheet-note">Observed is the ledger's actual on the planned blocks that day; a planned block the estate did not work counts as zero.
                A plan for a date beyond ${esc(D.window.to)} stays pending until a ledger extract covers it.</div>`
            : `<div class="empty">No plan has been accepted yet. ${esc(D.how_to_test)}</div>
               <div class="fp-actbar"><button class="ops-btn" data-open-panel="ops_harvest">Open the harvest plan</button></div>`,
        };
      }
      if (id === 'forecasts') {
        const withChecks = D.plans.filter(p => p.forecast_check && p.forecast_check.checks.length);
        return {
          title: 'Forecasts against the day',
          lead: 'For each accepted plan whose day is in the ledger: what the forecasts said when it was accepted, and what happened.',
          html: `<div class="fc-summary">${esc(D.forecast_summary ? D.forecast_summary.plain : '')}</div>
            ${withChecks.length ? withChecks.map(p => `<div class="fp-card fc-block">
                <div class="fc-block-h"><div class="fp-card-t">${esc(p.title)}</div><span class="fp-dim">due ${esc(p.due_date)}</span></div>
                <ul class="fc-checks">${p.forecast_check.checks.map(c => `<li>${c.right
                  ? '<span class="fc-mark ok">✓</span>' : '<span class="fc-mark no">✗</span>'}<span>${esc(c.plain)}</span></li>`).join('')}</ul>
              </div>`).join('')
              : `<div class="empty">Nothing to check yet. ${esc(D.how_to_test)}</div>`}
            <div class="sheet-note">A single day cannot prove a chance or a range right. The forecasts' full track record, over
              hundreds of past days, is in tomorrow's outlook.</div>
            <div class="fp-actbar"><button class="ops-btn" data-open-panel="forecasts">Open tomorrow's outlook</button></div>`,
        };
      }
      if (id === 'baseline') {
        return {
          title: 'The loop the ledger already closes',
          lead: "Every day's plan against every day's actual across the generated history. This is what Did it work reads once a season of real orders exists.",
          html: tbl(`
            <tr><th>Operation</th><th class="num">Orders</th><th class="num">Planned</th><th class="num">Actual</th>
                <th class="num">Realised</th>${(D.baseline[0] ? D.baseline[0].by_month : []).map(m => `<th class="num">${esc(m.month.slice(5))}</th>`).join('')}</tr>
            ${D.baseline.map(b => `<tr class="${b.realisation_pct !== null && b.realisation_pct < 65 ? 'hi' : ''}">
              <td><b>${esc(b.label)}</b></td><td class="num">${n0(b.orders)}</td>
              <td class="num">${n0(b.planned_qty)} ${esc(b.unit)}</td><td class="num">${n0(b.actual_qty)}</td>
              <td class="num">${bar(b.realisation_pct)}</td>
              ${b.by_month.map(m => `<td class="num">${m.realisation_pct === null ? '—' : fmt(m.realisation_pct, 0) + '%'}</td>`).join('')}</tr>`).join('')}`),
        };
      }
      return {
        title: 'Did it work',
        lead: D.headline,
        html: `<div class="kpis">
            <div class="kpi"><b>${c.accepted}</b><span>plans accepted</span></div>
            <div class="kpi"><b>${c.executed}</b><span>executed</span></div>
            <div class="kpi"><b>${c.pending}</b><span>pending</span></div>
            <div class="kpi ${c.not_executed ? 'warn' : ''}"><b>${c.not_executed}</b><span>not executed</span></div>
            <div class="kpi ${D.realisation_pct !== null && D.realisation_pct < 70 ? 'warn' : ''}"><b>${pc(D.realisation_pct)}</b><span>realisation</span></div>
          </div>
          <div class="fp-grid3">
            <div class="fp-card"><div class="fp-card-t">Expected</div><div class="fp-big">${n1(D.expected_tonnes)} t</div>
              <div class="fp-dim">across executed plans</div></div>
            <div class="fp-card"><div class="fp-card-t">Observed</div><div class="fp-big">${n1(D.observed_tonnes)} t</div>
              <div class="fp-dim">read back from the ledger</div></div>
            <div class="fp-card"><div class="fp-card-t">How to test it now</div>
              <div class="sheet-note" style="margin:0;border:none;padding:0">${esc(D.how_to_test)}</div>
              <div class="fp-actbar"><button class="ops-btn" data-open-panel="ops_harvest">Open the harvest plan</button></div></div>
          </div>
          <div class="sheet-note"><b>Provenance.</b> ${esc(D.provenance)}<br><br>${esc(D.note)}</div>`,
      };
    },
    wire: (root, api) => wireCommon(root, api),
  };
}
