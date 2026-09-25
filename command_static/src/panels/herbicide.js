/* Weeding and herbicide programme adherence.
 *
 * The spraying round against its target, the misses split into the days real
 * rain washed out and everything else, and the herbicide the store issued per
 * hectare actually sprayed against the register dose. One fetch; every figure
 * is computed server-side in gis/herbicide.py, which joins the spraying work
 * orders to the SAP MM goods issues on the order number. Charts are inline SVG.
 */
import { esc, firstSentence, fmt, idr } from '../lib/fmt.js';
import { blockList } from './_shared.js';

const MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const mon = m => MON[Number(String(m).slice(5, 7)) - 1] || String(m);
const has = v => v !== null && v !== undefined && !Number.isNaN(v);
const n0 = v => has(v) ? fmt(v, 0) : '—';
const n1 = v => has(v) ? fmt(v, 1) : '—';
const n2 = v => has(v) ? fmt(v, 2) : '—';
const p0 = v => has(v) ? `${Math.round(v)}%` : '—';
const p1 = v => has(v) ? `${Number(v).toFixed(1)}%` : '—';
const xf = v => has(v) ? `${Number(v).toFixed(2)}x` : '—';

/* Monthly bars with a line. Stacked segments per month, an optional series
   drawn as a line, and dashed horizontal rules for a target. */
function chart({ months, stacks, line = null, rules = [], unit = '', height = 190 }) {
  const n = months.length;
  if (!n) return '';
  const W = 680, H = height, L = 48, R = 14, T = 16, B = 30;
  let max = 0;
  months.forEach(m => {
    const tot = stacks.reduce((s, st) => s + (st.v(m) || 0), 0);
    max = Math.max(max, tot, line ? (line.v(m) || 0) : 0);
  });
  rules.forEach(r => { max = Math.max(max, r.v || 0); });
  max = max * 1.12 || 1;
  const dp = max < 10 ? 2 : 0;
  const iw = (W - L - R) / n, bw = iw * 0.56;
  const x = i => L + iw * (i + 0.5);
  const y = v => T + (H - T - B) * (1 - v / max);

  let grid = '';
  for (let t = 0; t <= 4; t++) {
    const v = max * t / 4;
    grid += `<line class="grid" x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}"/>`
      + `<text x="${L - 6}" y="${y(v) + 3.5}" text-anchor="end">${fmt(v, dp)}</text>`;
  }
  let bars = '';
  months.forEach((m, i) => {
    let base = 0;
    stacks.forEach(st => {
      const v = st.v(m) || 0;
      if (v <= 0) return;
      bars += `<rect x="${x(i) - bw / 2}" y="${y(base + v)}" width="${bw}"
        height="${Math.max(0.5, y(base) - y(base + v))}" style="fill:${st.fill}"><title>${
        esc(mon(m.month))}: ${esc(st.label)} ${fmt(v, dp === 2 ? 2 : 0)} ${esc(unit)}</title></rect>`;
      base += v;
    });
    bars += `<text x="${x(i)}" y="${H - B + 14}" text-anchor="middle">${esc(mon(m.month))}</text>`;
  });
  let ln = '';
  if (line) {
    const pts = months.map((m, i) => `${x(i)},${y(line.v(m) || 0)}`).join(' ');
    ln = `<polyline class="mid" points="${pts}"/>`
      + months.map((m, i) => `<circle cx="${x(i)}" cy="${y(line.v(m) || 0)}" r="3"
          style="fill:var(--accent)"><title>${esc(mon(m.month))}: ${esc(line.label)} ${
          fmt(line.v(m) || 0, dp)} ${esc(unit)}</title></circle>`).join('');
  }
  const rl = rules.map(r => `<line class="rop" x1="${L}" x2="${W - R}" y1="${y(r.v)}" y2="${y(r.v)}"/>
    <text class="lbl-rop" x="${W - R}" y="${y(r.v) - 4}" text-anchor="end">${esc(r.label)}</text>`).join('');

  const items = [...stacks.map(s => ({ label: s.label, fill: s.fill })),
                 ...(line ? [{ label: line.label, line: true }] : [])];
  let lx = L, legend = '';
  items.forEach(it => {
    legend += it.line
      ? `<line class="mid" x1="${lx}" x2="${lx + 12}" y1="${H + 1}" y2="${H + 1}"/>`
      : `<rect x="${lx}" y="${H - 4}" width="10" height="10" style="fill:${it.fill}"/>`;
    legend += `<text x="${lx + 15}" y="${H + 5}">${esc(it.label)}</text>`;
    lx += 15 + it.label.length * 6.3 + 14;
  });
  return `<svg class="st-chart" viewBox="0 0 ${W} ${H + 12}" role="img">${grid}${rl}${bars}${ln}${legend}</svg>`;
}

export async function panelHerbicide() {
  const r = await fetch('/gis/herbicide?estate=EC&top=12');
  if (!r.ok) return `<div class="empty">The herbicide join could not load (${r.status}).</div>`;
  const d = await r.json();
  if (!d.available) return `<div class="empty">${esc(d.reason)}</div>`;

  const t = d.totals, ro = d.round, pr = ro.past_round, mi = d.misses;
  const ra = d.rate, h = ra.handling, gly = ra.materials.glyphosate, met = ra.materials.metsulfuron;
  const late = has(ro.implied_days_at_pace) ? ro.implied_days_at_pace - ro.target_days : null;
  const months = d.by_month || [];

  const sprayChart = chart({
    months, unit: 'ha',
    stacks: [
      { label: 'sprayed', fill: 'var(--accent)', v: m => m.sprayed_ha },
      { label: 'rained off', fill: 'var(--gold)', v: m => m.rained_off_ha },
      { label: 'short on dry days', fill: 'var(--muted)', v: m => m.other_short_ha },
    ],
    line: { label: 'due this month', v: m => m.due_ha },
  });
  const rateChart = chart({
    months, unit: 'L/ha', height: 150,
    stacks: [{ label: 'glyphosate issued per ha sprayed', fill: 'var(--accent)',
               v: m => m.glyphosate_l_per_ha }],
    rules: [{ v: gly.dose_per_ha, label: `register dose ${n2(gly.dose_per_ha)} L/ha` }],
  });

  const pastCols = [
    { key: 'days_overdue', label: 'Days past round', num: true, fmt: n0 },
    { key: 'last_sprayed', label: 'Last sprayed', fmt: v => v || '—' },
    { key: 'orders', label: 'Orders since', num: true, fmt: n0 },
    { key: 'rained_off_attempts', label: 'Rained off', num: true, fmt: n0 },
  ];
  const doseCols = [
    { key: 'sprayed_ha', label: 'ha sprayed', num: true, fmt: n1 },
    { key: 'glyphosate_l', label: 'L issued', num: true, fmt: n1 },
    { key: 'l_per_ha', label: 'L/ha', num: true, fmt: n2 },
    { key: 'dose_factor', label: 'x dose', num: true, fmt: xf },
    { key: 'issues', label: 'Issues', num: true, fmt: n0 },
  ];

  return `
  <div class="sheet-note" style="font-size:12.5px;color:var(--text)">${esc(firstSentence(d.summary))}</div>

  <div class="kpis">
    <div class="kpi ${pr.pct > 33 ? 'alert' : ''}"><b>${p0(pr.pct)}</b>
      <span>${pr.blocks} of ${pr.of} blocks past the ${ro.target_days}-day round</span></div>
    <div class="kpi ${late > 15 ? 'alert' : ''}"><b>${n0(ro.implied_days_at_pace)} d</b>
      <span>round at current pace, target ${ro.target_days}</span></div>
    <div class="kpi"><b>${p0(mi.adherence_pct)}</b><span>of planned ha sprayed</span></div>
    <div class="kpi"><b>${p0(mi.rained_off_pct_of_plan)}</b><span>of plan rained off, real rain</span></div>
    <div class="kpi"><b>${n2(gly.per_ha)} L/ha</b><span>glyphosate issued, dose ${n2(gly.dose_per_ha)}</span></div>
    <div class="kpi ${h.found ? '' : 'warn'}"><b>${xf(h.recovered)}</b><span>issued over dose</span></div>
  </div>

  <div class="sub-t">Hectares sprayed each month against what was due</div>
  ${sprayChart}
  <table class="tbl">
    <tr><th>Month</th><th class="num">Due ha</th><th class="num">Planned</th><th class="num">Sprayed</th>
        <th class="num">Rained off</th><th class="num">Short, dry</th>
        <th class="num">Rain days &ge;${n0(mi.rain_threshold_mm)} mm</th><th class="num">Of due</th></tr>
    ${months.map(m => `<tr class="${has(m.cover_pct_of_due) && m.cover_pct_of_due < 60 ? 'hi' : ''}">
      <td>${esc(mon(m.month))} ${esc(String(m.month).slice(0, 4))}</td>
      <td class="num">${n0(m.due_ha)}</td><td class="num">${n0(m.planned_ha)}</td>
      <td class="num"><b>${n0(m.sprayed_ha)}</b></td><td class="num">${n0(m.rained_off_ha)}</td>
      <td class="num">${n0(m.other_short_ha)}</td><td class="num">${n0(m.rain_days)}</td>
      <td class="num">${p0(m.cover_pct_of_due)}</td></tr>`).join('')}
    <tr class="good"><td>Window</td><td class="num">${n0(t.due_ha)}</td>
      <td class="num">${n0(t.planned_ha)}</td><td class="num"><b>${n0(t.sprayed_ha)}</b></td>
      <td class="num">${n0(mi.rained_off_ha)}</td><td class="num">${n0(mi.other_short_ha)}</td>
      <td class="num">${n0(mi.rain_days_over_threshold)}</td><td class="num">${p0(t.cover_pct_of_due)}</td></tr>
  </table>

  <div class="sub-t">Herbicide issued per hectare sprayed</div>
  ${rateChart}
  <table class="tbl">
    <tr><th>Month</th><th class="num">Glyphosate L/ha</th><th class="num">x dose</th>
        <th class="num">Metsulfuron g/ha</th><th class="num">x dose</th></tr>
    ${months.map(m => `<tr>
      <td>${esc(mon(m.month))} ${esc(String(m.month).slice(0, 4))}</td>
      <td class="num">${n2(m.glyphosate_l_per_ha)}</td><td class="num">${xf(m.glyphosate_factor)}</td>
      <td class="num">${n1(m.metsulfuron_g_per_ha)}</td><td class="num">${xf(m.metsulfuron_factor)}</td></tr>`).join('')}
    <tr class="good"><td>Window</td>
      <td class="num"><b>${n2(gly.per_ha)}</b></td><td class="num"><b>${xf(gly.factor)}</b></td>
      <td class="num"><b>${n1(met.per_ha)}</b></td><td class="num"><b>${xf(met.factor)}</b></td></tr>
  </table>

  <div class="sub-t">Where the misses come from</div>
  <table class="tbl">
    <tr><th>Rain on the day</th><th class="num">Orders</th><th class="num">Planned ha</th>
        <th class="num">Sprayed</th><th class="num">Adherence</th><th class="num">Rained off</th></tr>
    ${(d.drivers_rain || []).map(b => `<tr class="${b.adherence_pct !== null && b.adherence_pct < 20 ? 'hi' : ''}">
      <td>${esc(b.bucket)}</td><td class="num">${n0(b.orders)}</td>
      <td class="num">${n0(b.planned_qty)}</td><td class="num">${n0(b.actual_qty)}</td>
      <td class="num">${p1(b.adherence_pct)}</td><td class="num">${n0(b.weathered_off)}</td></tr>`).join('')}
  </table>

  <div class="sub-t">By division</div>
  <table class="tbl">
    <tr><th>Div</th><th class="num">Blocks</th><th class="num">Past round</th>
        <th class="num">Adherence</th><th class="num">Rained off</th>
        <th class="num">L/ha</th><th class="num">x dose</th></tr>
    ${(d.by_division || []).map(v => `<tr class="${v.past_round_pct > 50 ? 'hi' : ''}">
      <td>${esc(v.division_code)}</td><td class="num">${n0(v.blocks)}</td>
      <td class="num">${n0(v.past_round)} (${p0(v.past_round_pct)})</td>
      <td class="num">${p0(v.adherence_pct)}</td><td class="num">${p0(v.rained_off_pct_of_plan)}</td>
      <td class="num">${n2(v.glyphosate_l_per_ha)}</td><td class="num">${xf(v.dose_factor)}</td></tr>`).join('')}
  </table>

  <div class="sub-t">Blocks most past their round</div>
  ${blockList(d.blocks_past_round, { cols: pastCols })
    || '<div class="empty">No block is past its round.</div>'}

  <div class="sub-t">Most over the dose</div>
  ${blockList(d.blocks_over_dose, { cols: doseCols }) || '<div class="empty">No rated issues.</div>'}
  <div class="sub-t">Most under the dose</div>
  ${blockList(d.blocks_under_dose, { cols: doseCols }) || '<div class="empty">No rated issues.</div>'}

  <div class="fp-actbar">
    <button class="ops-btn small" data-open-panel="ops_weed">Open the weeding plan</button>
  </div>`;
}
