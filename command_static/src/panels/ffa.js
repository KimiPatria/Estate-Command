/* Free fatty acid against dispatch delay (feature ffa_decay, panel `ffa`).
 *
 * The one quality loss the estate controls directly is the hours between the
 * knife and the weighbridge. The panel leads with what to do, then shows the
 * fit, and says beside every fitted figure that the slope was planted by the
 * generator: recovery is not discovery.
 */
import { esc, idr, fmt } from '../lib/fmt.js';
import { blockList } from './_shared.js';

const num = (v, d = 2) => v === null || v === undefined ? '—' : fmt(v, d);
const hrs = (v, d = 2) => v === null || v === undefined ? '—' : `${fmt(v, d)} h`;
const pc = (v, d = 2) => v === null || v === undefined ? '—' : `${fmt(v, d)}%`;

/* Bucket label from the edges, so the dash is typographic rather than ASCII. */
const bucketLabel = b => b.lo_h === 0 ? `< ${b.hi_h} h`
  : b.hi_h === null || b.hi_h === undefined ? `> ${b.lo_h} h`
  : `${b.lo_h}–${b.hi_h} h`;

/* ── the scatter: one dot per trip, the fitted line, the planted line ──── */

function scatter(sc, thr, fit, planted) {
  if (!sc || !sc.points || !sc.points.length) return '';
  const W = 660, H = 240, L = 46, R = 14, T = 16, B = 32;
  const xs = sc.points.map(q => q[0]);
  const ys = sc.points.map(q => q[1]);
  const x0 = Math.floor(Math.min(...xs) * 2) / 2;
  const x1 = Math.ceil(Math.max(...xs) * 2) / 2;
  // The penalty line is part of the picture even when nothing reaches it:
  // the gap between the dots and the line is the finding.
  const yLo = Math.floor(Math.min(...ys) * 10) / 10 - 0.05;
  const yHi = Math.max(Math.ceil(Math.max(...ys) * 10) / 10, thr || 0) + 0.08;
  const x = v => L + (W - L - R) * (v - x0) / (x1 - x0);
  const y = v => T + (H - T - B) * (1 - (v - yLo) / (yHi - yLo));
  const seg = (pts, cls) => pts && pts.length === 2
    ? `<line class="${cls}" x1="${x(pts[0][0]).toFixed(1)}" y1="${y(pts[0][1]).toFixed(1)}"
        x2="${x(pts[1][0]).toFixed(1)}" y2="${y(pts[1][1]).toFixed(1)}"/>` : '';
  const yTicks = [0, 1, 2, 3, 4].map(k => yLo + (yHi - yLo) * k / 4);
  const xTicks = [];
  for (let v = Math.ceil(x0); v <= x1; v += 1) xTicks.push(v);
  const dots = sc.points.map(q =>
    `<circle cx="${x(q[0]).toFixed(1)}" cy="${y(q[1]).toFixed(1)}" r="1.9"
       style="fill:var(--accent);opacity:.42"/>`).join('');
  const thrLine = thr ? `<line class="zero" x1="${L}" x2="${W - R}"
      y1="${y(thr).toFixed(1)}" y2="${y(thr).toFixed(1)}"/>
    <text x="${W - R - 4}" y="${(y(thr) - 5).toFixed(1)}" text-anchor="end"
      style="fill:var(--error)">${esc(thr)}% penalty line (placeholder)</text>` : '';
  return `<svg class="st-chart" viewBox="0 0 ${W} ${H}" role="img"
      aria-label="FFA at the mill against the ticket turnaround, one dot per trip">
    ${yTicks.map(t => `<line class="grid" x1="${L}" x2="${W - R}" y1="${y(t).toFixed(1)}" y2="${y(t).toFixed(1)}"/>
      <text x="${L - 6}" y="${(y(t) + 3).toFixed(1)}" text-anchor="end">${t.toFixed(1)}%</text>`).join('')}
    ${xTicks.map(v => `<text x="${x(v).toFixed(1)}" y="${H - 14}" text-anchor="middle">${v} h</text>`).join('')}
    <text x="${W - R}" y="${H - 2}" text-anchor="end">turnaround on the ticket</text>
    ${dots}
    ${thrLine}
    ${seg(sc.planted, 'rop')}
    ${seg(sc.fitted, 'mid')}
  </svg>
  <div class="st-legend">
    <span><i style="width:8px;height:8px;border-radius:50%;background:var(--accent);opacity:.6"></i>one trip (${idr(sc.drawn)} of ${idr(sc.of)} drawn; all are in the fit)</span>
    <span><i class="lg-mid"></i>fitted ${num(fit.slope_per_h, 4)}/h</span>
    <span><i class="lg-rop"></i>planted ${num(planted.per_hour, 2)}/h</span>
    ${thr ? '<span><i class="lg-zero"></i>penalty line</span>' : ''}
  </div>`;
}

/* ── a bucket table with an inline bar for mean FFA ─────────────────────── */

function bucketTable(title, rows, colLabel) {
  if (!rows || !rows.length) return '';
  const vals = rows.map(r => r.mean_ffa_pct);
  const hi = Math.max(...vals);
  const floor = Math.min(...vals) - 0.1;
  const bar = v => {
    const w = Math.max(2, Math.round(72 * (v - floor) / Math.max(1e-9, hi - floor)));
    return `<svg width="76" height="8" viewBox="0 0 76 8" aria-hidden="true"
      style="vertical-align:middle;margin-left:6px"><rect x="0" y="1" width="${w}" height="6" rx="1"
      style="fill:var(--accent);opacity:.7"/></svg>`;
  };
  return `<div class="sub-t">${esc(title)}</div>
    <table class="tbl">
      <tr><th>${esc(colLabel)}</th><th class="num">Trips</th><th class="num">Share</th>
          <th class="num">Tonnes</th><th class="num">Queue h</th>
          <th class="num">Mean FFA</th><th class="num">p90</th><th class="num">Over line</th></tr>
      ${rows.map(r => `<tr class="${r.over_threshold_pct > 0 ? 'hi' : ''}">
        <td>${esc(bucketLabel(r))}</td>
        <td class="num">${idr(r.trips)}</td><td class="num">${pc(r.share_pct, 1)}</td>
        <td class="num">${idr(Math.round(r.tonnes))}</td>
        <td class="num">${num(r.mean_queue_h)}</td>
        <td class="num">${pc(r.mean_ffa_pct)}${bar(r.mean_ffa_pct)}</td>
        <td class="num">${pc(r.p90_ffa_pct)}</td>
        <td class="num">${pc(r.over_threshold_pct)}</td>
      </tr>`).join('')}
    </table>`;
}

/* ── the panel ──────────────────────────────────────────────────────────── */

export async function panelFfa() {
  const r = await fetch('/gis/ffa?estate=EC&top=12');
  if (!r.ok) return `<div class="empty">The FFA endpoint answered ${r.status}.</div>`;
  const d = await r.json();
  if (!d.available) return `<div class="empty">${esc(d.reason || 'No trip ledger.')}</div>`;

  const t = d.totals, s = d.delay_split, rec = d.recovery, fit = d.fit || {};
  const thr = (d.threshold || {}).ffa_pct;
  const ft = fit.turnaround || {}, fc = fit.cut_to_mill || {}, fq = fit.queue || {}, fw = fit.field_wait || {};
  const overAny = (t.over_threshold_pct || 0) > 0;

  const kpis = `<div class="kpis">
    <div class="kpi"><b>${num(ft.slope_per_h, 2)} pts/h</b><span>FFA per hour on the ticket</span></div>
    <div class="kpi"><b>${pc(t.mean_ffa_pct)}</b><span>mean FFA at the gate</span></div>
    <div class="kpi ${overAny ? 'alert' : ''}"><b>${pc(t.over_threshold_pct)}</b><span>of tonnes over ${esc(thr)}%</span></div>
    <div class="kpi alert"><b>${pc(s.queue_share_of_turnaround_pct, 1)}</b><span>of the ticket is queueing</span></div>
    <div class="kpi"><b>${hrs(t.mean_cut_to_mill_h, 1)}</b><span>mean cut to mill</span></div>
    <div class="kpi"><b>${pc(rec.recovered_pct, 1)}</b><span>of the planted slope recovered</span></div>
  </div>`;

  const lead = `<div class="sheet-note">${esc(d.summary)}</div>`;

  const chart = scatter(d.scatter, thr, ft, d.planted || {});

  const split = `<div class="sub-t">Where the hours go</div>
    <table class="tbl">
      <tr><th>Leg</th><th class="num">Hours</th><th class="num">Share of cut to mill</th><th>What drives it</th></tr>
      <tr><td>Roadside wait, cut → departure</td><td class="num">${num(s.field_wait_h)}</td>
        <td class="num">${pc(s.field_wait_share_pct, 1)}</td><td>when the truck reaches the platform</td></tr>
      <tr><td>Road and ramp</td><td class="num">${num(s.road_h)}</td>
        <td class="num">${pc(s.road_share_pct, 1)}</td><td>distance to mill, loading, tipping</td></tr>
      <tr class="hi"><td>Mill queue</td><td class="num">${num(s.queue_h)}</td>
        <td class="num">${pc(s.queue_share_pct, 1)}</td>
        <td>${pc(s.queue_share_of_turnaround_pct, 1)} of the ticket; ${hrs(s.queue_h_after_noon, 1)} after noon against ${hrs(s.queue_h_before_noon, 1)} before</td></tr>
      <tr class="good"><td>Cut to mill, total</td><td class="num">${num(s.cut_to_mill_h)}</td>
        <td class="num">100%</td><td>of which the ticket is ${hrs(s.turnaround_h)}</td></tr>
    </table>`;

  const hours = (d.by_depart_hour || []).length ? `<div class="sub-t">By departure hour</div>
    <table class="tbl">
      <tr><th>Leaves</th><th class="num">Trips</th><th class="num">Queue h</th>
          <th class="num">Cut to mill h</th><th class="num">Mean FFA</th></tr>
      ${d.by_depart_hour.map(x => `<tr class="${parseInt(x.hour, 10) >= 12 ? 'hi' : ''}">
        <td>${esc(x.hour)}</td><td class="num">${idr(x.trips)}</td>
        <td class="num">${num(x.queue_h)}</td><td class="num">${num(x.cut_to_mill_h, 1)}</td>
        <td class="num">${pc(x.mean_ffa_pct)}</td></tr>`).join('')}
    </table>` : '';

  const recovery = `<div class="sub-t">Recovered against planted</div>
    <table class="tbl">
      <tr><th>Term</th><th class="num">Planted</th><th class="num">Recovered</th>
          <th class="num">Read back</th><th class="num">R²</th></tr>
      <tr class="good"><td>FFA per hour of turnaround</td><td class="num">${num(rec.slope_planted, 2)}</td>
        <td class="num">${num(rec.slope_recovered, 4)}</td><td class="num">${pc(rec.recovered_pct, 1)}</td>
        <td class="num">${num(rec.r2, 3)}</td></tr>
      <tr><td>FFA at zero hours (intercept)</td><td class="num">${pc(rec.intercept_planted, 2)}</td>
        <td class="num">${pc(rec.intercept_recovered, 3)}</td><td class="num">—</td><td class="num"></td></tr>
      <tr><td>FFA per hour of mill queue alone</td><td class="num">via turnaround</td>
        <td class="num">${num(fq.slope_per_h, 4)}</td><td class="num">—</td>
        <td class="num">${num(fq.r2, 3)}</td></tr>
      <tr><td>FFA per hour of roadside wait</td><td class="num">none planted</td>
        <td class="num">${num(fw.slope_per_h, 4)}</td><td class="num">—</td>
        <td class="num">${num(fw.r2, 3)}</td></tr>
      <tr><td>FFA per hour of cut to mill (derived)</td><td class="num">none planted</td>
        <td class="num">${num(fc.slope_per_h, 4)}</td><td class="num">—</td>
        <td class="num">${num(fc.r2, 3)}</td></tr>
    </table>
    <div class="sheet-note">${esc(rec.verdict)} Rule: ${esc((d.planted || {}).rule)} — ${esc((d.planted || {}).source)}.</div>`;

  const blockCols = [
    { key: 'division_code', label: 'Div' },
    { key: 'trips', label: 'Trips', num: true, fmt: v => idr(v) },
    { key: 'mean_ffa_pct', label: 'Mean FFA', num: true, fmt: v => pc(v) },
    { key: 'cut_to_mill_h', label: 'Cut to mill h', num: true, fmt: v => num(v) },
    { key: 'queue_h', label: 'Queue h', num: true, fmt: v => num(v) },
    { key: 'km_to_mill', label: 'km', num: true, fmt: v => num(v, 1) },
  ];
  const worst = (d.worst_ffa_blocks || []).length ? `<div class="sub-t">Highest FFA at the gate</div>
    ${blockList(d.worst_ffa_blocks, { cols: blockCols,
      caption: 'Block means over every trip in the ledger; the spread between blocks is the spread in distance and queue.' })}` : '';
  const longest = (d.longest_delay_blocks || []).length ? `<div class="sub-t">Longest cut to mill</div>
    ${blockList(d.longest_delay_blocks, { cols: blockCols })}` : '';

  const routes = (d.routes || []).length ? `<div class="sub-t">By route</div>
    <table class="tbl">
      <tr><th>Route</th><th>Div</th><th class="num">Blocks</th><th class="num">Trips</th>
          <th class="num">Tonnes</th><th class="num">Mean FFA</th>
          <th class="num">Cut to mill h</th><th class="num">Queue h</th><th class="num">km</th></tr>
      ${d.routes.map((x, i) => `<tr class="${i === 0 ? 'hi' : ''}">
        <td>${esc(x.route_code)}</td><td>${esc(x.division_code === null ? '—' : x.division_code)}</td>
        <td class="num">${idr(x.blocks)}</td><td class="num">${idr(x.trips)}</td>
        <td class="num">${idr(Math.round(x.hauled_t))}</td><td class="num">${pc(x.mean_ffa_pct)}</td>
        <td class="num">${num(x.cut_to_mill_h)}</td><td class="num">${num(x.queue_h)}</td>
        <td class="num">${num(x.km_to_mill, 1)}</td></tr>`).join('')}
    </table>` : '';

  const pr = d.pricing || {};
  const priced = pr.idr_per_kg_point ? `<div class="sheet-note"><b>Priced, on a placeholder tariff.</b>
    At IDR ${idr(pr.idr_per_kg_point)}/kg per FFA point, the mill queue adds
    ${num(pr.queue_ffa_points, 3)} points to every tonne, IDR ${idr(pr.queue_cost_idr)} over
    ${idr(Math.round(t.hauled_t))} t; afternoon departures add ${num(pr.afternoon_extra_points, 3)} points,
    IDR ${idr(pr.afternoon_cost_idr)}; the over-line penalty is IDR ${idr(pr.over_threshold_penalty_idr)}.
    ${esc(pr.note)}</div>` : '';

  const badges = d.badges ? Object.entries(d.badges)
    .map(([k, v]) => `${esc(k)}: ${esc(v)}`).join(' · ') : '';

  return `${kpis}${lead}${chart}
    ${bucketTable('FFA by hours from cut to mill', d.buckets, 'Cut to mill')}
    <div class="sheet-note"><b>Assumption.</b> ${esc(d.assumption)}</div>
    ${bucketTable('FFA by turnaround on the ticket', d.turnaround_buckets, 'Turnaround')}
    ${split}${hours}${recovery}${worst}${longest}${routes}${priced}
    <div class="sheet-note"><b>Caveat.</b> ${esc(d.caveat)}<br><br>
      <b>Threshold.</b> ${esc((d.threshold || {}).note)}<br><br>
      <b>Provenance.</b> ${esc(d.provenance)} ${badges ? `(${badges}.)` : ''}<br><br>${esc(d.note)}</div>`;
}
