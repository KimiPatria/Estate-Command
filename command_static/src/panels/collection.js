/* Collection point coverage: is every block's fruit collected the day it is
 * cut, and where does it wait?
 *
 * The ledger joins the harvest work orders (the cutting days) to the trip
 * ledger (the trip days). In the synthetic feeds that join is exact by
 * construction, and the panel says so in its first line rather than dressing
 * a 100% up as a result. What the ledger does carry is where the day's fruit
 * waits: how much of it leaves the platform after noon, how much longer the
 * afternoon queue at the mill is, and how full the loads are on each route
 * and in each vehicle class. Those are the lists the map gets.
 */
import { esc, idr } from '../lib/fmt.js';
import { blockList } from './_shared.js';

const nil = v => v === null || v === undefined;
const n1 = v => nil(v) ? '—' : Number(v).toFixed(1);
const n2 = v => nil(v) ? '—' : Number(v).toFixed(2);
const p0 = v => nil(v) ? '—' : `${Math.round(v)}%`;
const p1 = v => nil(v) ? '—' : `${Number(v).toFixed(1)}%`;
const t1 = v => nil(v) ? '—' : `${idr(Math.round(v))} t`;

/* One series of vertical bars. Bars after the cut are gold and named so in
   the caption, so the split never rides on colour alone. Every bar carries a
   <title> for hover; only the highest and lowest are labelled inline. */
function hourBars(rows, key, unit) {
  const W = 520, H = 150, L = 36, R = 8, T = 16, B = 22;
  const n = rows.length;
  if (!n) return '';
  const vals = rows.map(r => Number(r[key]) || 0);
  const max = Math.max(...vals) || 1;
  const slot = (W - L - R) / n, gap = 2, bw = slot - gap;
  const y = v => T + (H - T - B) * (1 - v / max);
  const base = H - B;
  const iMax = vals.indexOf(Math.max(...vals)), iMin = vals.indexOf(Math.min(...vals));
  const ticks = [0, max / 2, max];
  const bar = (r, i) => {
    const v = vals[i], x = L + i * slot + gap / 2, top = y(v), rad = Math.min(3, bw / 2);
    const h = Math.max(0, base - top);
    const d = h > rad
      ? `M${x},${base} V${top + rad} a${rad},${rad} 0 0 1 ${rad},-${rad} H${x + bw - rad} a${rad},${rad} 0 0 1 ${rad},${rad} V${base} Z`
      : `M${x},${base} V${top} H${x + bw} V${base} Z`;
    const fill = r.afternoon ? 'var(--gold)' : 'var(--accent)';
    const lbl = (i === iMax || i === iMin)
      ? `<text x="${(x + bw / 2).toFixed(1)}" y="${(top - 4).toFixed(1)}" text-anchor="middle">${n2(v)}</text>` : '';
    return `<path d="${d}" fill="${fill}"><title>${esc(r.label)}: ${n2(v)} ${unit} mean queue, ${idr(r.trips)} trips, ${t1(r.hauled_t)}</title></path>${lbl}`;
  };
  return `<svg class="st-chart" viewBox="0 0 ${W} ${H}" role="img"
      aria-label="Mean queue at the mill by departure hour">
    ${ticks.map(t => `<line class="grid" x1="${L}" x2="${W - R}" y1="${y(t).toFixed(1)}" y2="${y(t).toFixed(1)}"/>
      <text x="${L - 5}" y="${(y(t) + 3).toFixed(1)}" text-anchor="end">${n1(t)}</text>`).join('')}
    ${rows.map(bar).join('')}
    ${rows.map((r, i) => `<text x="${(L + i * slot + slot / 2).toFixed(1)}" y="${H - 7}" text-anchor="middle">${esc(r.label)}</text>`).join('')}
  </svg>`;
}

/* A load-factor cell: a short inline bar and the number beside it. */
function loadCell(pct) {
  if (nil(pct)) return '—';
  const w = 54, h = 7, f = Math.max(0, Math.min(1, pct / 100));
  return `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" style="vertical-align:middle;margin-right:5px" aria-hidden="true">
      <rect x="0" y="0" width="${w}" height="${h}" rx="3" fill="var(--surface-2)"/>
      <rect x="0" y="0" width="${(w * f).toFixed(1)}" height="${h}" rx="3" fill="${pct < 75 ? 'var(--gold)' : 'var(--accent)'}"/>
    </svg>${p0(pct)}`;
}

export async function panelCollection() {
  const r = await fetch('/gis/collection?estate=EC&top=12');
  if (!r.ok) return `<div class="empty">The collection endpoint answered ${r.status}.</div>`;
  const d = await r.json();
  if (!d.available) return `<div class="empty">${esc(d.reason)}</div>`;
  const c = d.coverage, t = d.totals, after = d.afternoon_label || d.afternoon_from;
  const worstRoute = d.by_route[0];
  const worstClass = d.by_class[0];

  const kpis = `<div class="kpis">
      <div class="kpi"><b>${p0(c.same_day_pct)}</b><span>collected same day${c.by_construction ? ' · by construction' : ''}</span></div>
      <div class="kpi"><b>${idr(c.days_left_overnight)}</b><span>block-days left overnight</span></div>
      <div class="kpi alert"><b>${p0(t.afternoon_share_pct)}</b><span>of the crop leaves after ${esc(after)}</span></div>
      <div class="kpi"><b>${n2(t.queue_afternoon_h)} h</b><span>mill queue after ${esc(after)} · ${n2(t.queue_morning_h)} h before</span></div>
      <div class="kpi${t.load_factor_pct < 75 ? ' warn' : ''}"><b>${p0(t.load_factor_pct)}</b><span>load factor, all trips</span></div>
      <div class="kpi"><b>${n2(t.trips_per_cutting_day)}</b><span>trips per cutting day</span></div>
    </div>`;

  const construction = c.by_construction ? `<div class="sheet-note">
      <b>Why 100% is not a finding.</b> Every one of the ${idr(c.cutting_days)} block-cutting-days in the
      ledger has a trip on that date, and the ${idr(c.bunches_cut)} bunches cut equal the
      ${idr(c.bunches_hauled)} hauled on every one of them. The generated feeds place cutting days and
      trip days with the same seeded function, so this join can only ever reconcile. The
      ${idr(c.plan_only_days)} plan-only order days (rain-outs and no-shows) have no fruit and no
      trip, which is correct. To measure fruit left at the collection point the estate needs
      ${(d.needs || []).map(n => `<b>${esc(n.entity)}</b> (${esc(n.system)})`).join(' and ')}: both
      are recorded today and neither was exported.</div>` : '';

  const hourChart = `<div class="sub-t">Mill queue by departure hour</div>
    ${hourBars(d.by_hour, 'mean_queue_h', 'h')}
    <div class="blk-cap">Mean hours queued at the weighbridge, by the hour the load left the platform.
      Gold bars are departures from ${esc(d.afternoon_from)}: ${p0(t.afternoon_share_pct)} of the crop
      (${t1(t.afternoon_t)}) leaves then and waits ${n2(t.queue_afternoon_h)} h against
      ${n2(t.queue_morning_h)} h before. Fruit that sat at the platform through the morning then sits
      again at the mill.</div>`;

  const monthTable = `<div class="sub-t">Coverage by month</div>
    <table class="tbl">
      <tr><th>Month</th><th class="num">Cutting days</th><th class="num">Same day</th>
          <th class="num">Trips / day</th><th class="num">After ${esc(after)}</th>
          <th class="num">Plan met</th></tr>
      ${d.by_month.map(m => `<tr>
        <td>${esc(m.month)}</td><td class="num">${idr(m.cutting_days)}</td>
        <td class="num">${p0(m.same_day_pct)}</td><td class="num">${n2(m.trips_per_cutting_day)}</td>
        <td class="num">${p0(m.afternoon_share_pct)}</td>
        <td class="num">${p0(m.dispatch_adherence_pct)}</td>
      </tr>`).join('')}
    </table>
    <div class="blk-cap">Plan met is the dispatch orders' weighed tonnes over planned tonnes, so its gap is
      harvest shortfall and shrinkage together, not a collection miss.</div>`;

  const divTable = `<div class="sub-t">By division</div>
    <table class="tbl">
      <tr><th>Div</th><th class="num">Blocks</th><th class="num">Cutting days</th>
          <th class="num">Same day</th><th class="num">Trips / day</th>
          <th class="num">Load</th><th class="num">After ${esc(after)}</th></tr>
      ${d.by_division.map(v => `<tr class="${v.load_factor_pct < 75 ? 'hi' : ''}">
        <td>${esc(v.division_code)}</td><td class="num">${v.blocks}</td>
        <td class="num">${idr(v.cutting_days)}</td><td class="num">${p0(v.same_day_pct)}</td>
        <td class="num">${n2(v.trips_per_cutting_day)}</td><td class="num">${p0(v.load_factor_pct)}</td>
        <td class="num">${p0(v.afternoon_share_pct)}</td>
      </tr>`).join('')}
    </table>`;

  const routeTable = `<div class="sub-t">Routes, emptiest loads first</div>
    <table class="tbl">
      <tr><th>Route</th><th>Div</th><th class="num">Blocks</th><th class="num">Trips</th>
          <th class="num">km</th><th class="num">Queue h</th><th class="num">Load</th>
          <th class="num">Under half</th></tr>
      ${d.by_route.map((x, i) => `<tr class="${i === 0 ? 'hi' : ''}">
        <td>${esc(x.route_code)}</td><td>${esc(x.division_code)}</td>
        <td class="num">${x.blocks}</td><td class="num">${idr(x.trips)}</td>
        <td class="num">${n1(x.mean_km)}</td><td class="num">${n2(x.mean_queue_h)}</td>
        <td class="num">${loadCell(x.load_factor_pct)}</td>
        <td class="num">${p1(x.loads_under_half_pct)}</td>
      </tr>`).join('')}
    </table>
    <div class="blk-cap">Load is net kg weighed over the capacity of the vehicle that carried it.
      Under half is the share of loads carrying less than half that capacity: the wrong vehicle for
      the block-day, or a block-day too small to send one for.</div>`;

  const classTable = `<div class="sub-t">Vehicle classes</div>
    <table class="tbl">
      <tr><th>Class</th><th class="num">Cap t</th><th class="num">Trips</th>
          <th class="num">Load</th><th class="num">Under half</th>
          <th class="num">Loads / day</th></tr>
      ${d.by_class.map((x, i) => `<tr class="${i === 0 ? 'hi' : ''}">
        <td>${esc(x.vehicle_class)} <span style="color:var(--muted)">×${x.vehicles}</span></td>
        <td class="num">${n1(x.capacity_t)}</td><td class="num">${idr(x.trips)}</td>
        <td class="num">${loadCell(x.load_factor_pct)}</td>
        <td class="num">${p1(x.loads_under_half_pct)}</td>
        <td class="num">${n1(x.mean_loads_per_day)} <span style="color:var(--muted)">· ${n1(x.p80_loads_per_day)}</span></td>
      </tr>`).join('')}
    </table>
    <div class="blk-cap">${esc(worstClass.vehicle_class)}s run ${p0(worstClass.load_factor_pct)} full and
      ${p1(worstClass.loads_under_half_pct)} of their loads are under half: the largest vehicle is
      the emptiest. Loads per day is the mean per vehicle, then the 80th-percentile day from the
      dispatch ledger.</div>`;

  const waitingList = `<div class="sub-t">Most fruit at the platform after ${esc(after)}</div>
    ${blockList(d.waiting, {
      cols: [
        { key: 'division_code', label: 'Div' },
        { key: 'route_code', label: 'Route' },
        { key: 'afternoon_t', label: `t after ${after}`, num: true, fmt: n1 },
        { key: 'afternoon_share_pct', label: 'Share', num: true, fmt: p0 },
        { key: 'last_load', label: 'Last load', num: true },
        { key: 'mean_queue_h', label: 'Queue h', num: true, fmt: n2 },
      ],
      caption: c.by_construction
        ? `Ranked by tonnes leaving after ${after}, since no block has fruit left overnight here. With the two timestamps above this list becomes tonnes still at the collection point the next morning.`
        : 'Ranked by bunches left overnight, then by tonnes leaving in the afternoon.',
    })}`;

  const loadList = `<div class="sub-t">Emptiest loads, blocks with five or more trips</div>
    ${blockList(d.worst_load, {
      cols: [
        { key: 'division_code', label: 'Div' },
        { key: 'trips_per_cutting_day', label: 'Trips / day', num: true, fmt: n2 },
        { key: 'mean_load_t', label: 'Load t', num: true, fmt: n2 },
        { key: 'load_factor_pct', label: 'Load', num: true, fmt: p0 },
        { key: 'loads_under_half_pct', label: 'Under half', num: true, fmt: p0 },
      ],
      caption: 'One trip a day carrying a quarter of a dump truck is a block-day for the mini-truck, or two blocks for one vehicle.',
    })}`;

  return `<div class="sheet-note" style="color:var(--text)">${esc(d.summary)}</div>
    ${kpis}
    ${construction}
    ${hourChart}
    ${monthTable}
    ${divTable}
    ${routeTable}
    ${classTable}
    ${waitingList}
    ${loadList}
    <div class="sheet-note">Route ${esc(worstRoute.route_code)} at ${p0(worstRoute.load_factor_pct)} and the
      afternoon queue are scheduling questions; tomorrow's vehicle plan is in the transport window.
      <button class="btn ops-btn small" data-open-panel="ops_dispatch">Open the transport window</button></div>
    <div class="sheet-note"><b>Caveat.</b> ${esc(d.caveat)}<br><br>
      <b>Provenance.</b> ${esc(d.provenance)}<br><br>${esc(d.note)}</div>`;
}
