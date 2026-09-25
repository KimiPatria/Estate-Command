import { esc, idr } from '../lib/fmt.js';

export async function panelTransport(view) {
  const d = await (await fetch('/gis/transport?estate=EC')).json();
  if (!d.available) return `<div class="empty">${esc(d.reason)}</div>`;
  const t = d.totals;

  const kpis = {
    turnaround: `<div class="kpi"><b>${t.mean_turnaround_h} h</b><span>mean turnaround</span></div>
      <div class="kpi alert"><b>${t.mean_queue_h} h</b><span>of it queueing</span></div>
      <div class="kpi"><b>${t.queue_share_pct}%</b><span>cycle spent waiting</span></div>
      <div class="kpi"><b>${idr(t.trips)}</b><span>trips</span></div>`,
    fuel: `<div class="kpi"><b>${idr(t.diesel_l)} L</b><span>diesel issued</span></div>
      <div class="kpi"><b>${t.diesel_l_per_tonne} L</b><span>per tonne hauled</span></div>
      <div class="kpi"><b>${idr(Math.round(t.hauled_t))} t</b><span>evacuated</span></div>`,
    fleet: `<div class="kpi"><b>${d.fleet.reduce((a, f) => a + f.vehicles, 0)}</b><span>vehicles</span></div>
      <div class="kpi alert"><b>${d.fleet.reduce((a, f) => a + f.failures, 0)}</b><span>breakdowns</span></div>
      <div class="kpi"><b>${Math.round(d.fleet.reduce((a, f) => a + f.downtime_h, 0))} h</b><span>downtime</span></div>`,
  }[view] || '';

  const fleetTable = `<div class="sub-t">Fleet reliability</div>
    <table class="tbl">
      <tr><th>Class</th><th class="num">Vehicles</th><th class="num">Trips</th>
          <th class="num">Failures</th><th class="num">Trips between</th>
          <th class="num">Downtime h</th><th class="num">Cost IDR</th></tr>
      ${d.fleet.map(f => `<tr class="${f.mtbf_trips && f.mtbf_trips < 100 ? 'hi' : ''}">
        <td>${esc(f.vehicle_class)}</td><td class="num">${f.vehicles}</td>
        <td class="num">${idr(f.trips)}</td><td class="num">${f.failures}</td>
        <td class="num">${f.mtbf_trips === null ? '—' : idr(f.mtbf_trips)}</td>
        <td class="num">${f.downtime_h}</td><td class="num">${idr(f.downtime_cost_idr)}</td>
      </tr>`).join('')}
    </table>`;

  const slowTable = `<div class="sub-t">Slowest blocks to clear</div>
    <table class="tbl">
      <tr><th>Block</th><th>Div</th><th class="num">km</th>
          <th class="num">Turnaround h</th><th class="num">Queue h</th></tr>
      ${d.slowest_turnaround.map(r => `<tr>
        <td>${esc(r.block_label)}</td><td>${esc(r.division_code)}</td>
        <td class="num">${r.km_to_mill}</td>
        <td class="num">${r.turnaround_h}</td><td class="num">${r.queue_h}</td>
      </tr>`).join('')}
    </table>`;

  const body = view === 'fleet' ? fleetTable : slowTable + (view === 'fuel' ? '' : fleetTable);
  const lead = view === 'turnaround'
    ? `<div class="sheet-note">${t.queue_share_pct}% of the haulage cycle is spent
       queueing at the mill.</div>` : '';

  return `${lead}<div class="kpis">${kpis}</div>${body}`;
}

/* ── pest and disease ───────────────────────────────────────────────────── */

