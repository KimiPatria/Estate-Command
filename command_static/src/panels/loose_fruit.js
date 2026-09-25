/* Loose fruit recovery - the one harvesting loss the export already counts.
 *
 * The export carries a loose_fruits count on every harvest record and the
 * app used to drop it. This panel reads it back: the ratio per bunch, the
 * rate against the estate's own best quarter of blocks, the months, the
 * divisions, and the blocks to walk first. Counts are real; the tonnes and
 * rupiah at the bottom rest on a labelled placeholder fruit weight.
 */
import { esc, firstSentence, idr } from '../lib/fmt.js';
import { blockList } from './_shared.js';

const r2 = v => v === null || v === undefined ? '—' : Number(v).toFixed(2);
const r1 = v => v === null || v === undefined ? '—' : Number(v).toFixed(1);
const p0 = v => v === null || v === undefined ? '—' : Number(v).toFixed(0) + '%';
const fruits = n => n === null || n === undefined ? '—'
  : n >= 1e6 ? (n / 1e6).toFixed(1) + ' M' : idr(Math.round(n));

/* Loose fruit per bunch by month, as bars, with the benchmark as the dashed
   line the stores chart already uses for its reorder point. */
function monthChart(series, benchmark) {
  if (!series || !series.length) return '';
  const W = 440, H = 150, L = 34, R = 10, T = 14, B = 24;
  const top = Math.max(benchmark || 0, ...series.map(s => s.loose_per_bunch || 0));
  const hi = (top * 1.18) || 1;
  const slot = (W - L - R) / series.length;
  const bw = slot * 0.6;
  const y = v => T + (H - T - B) * (1 - v / hi);
  const ticks = [0, 0.5, 1, 1.5, 2, 2.5, 3].filter(t => t <= hi);
  // Past a dozen bars the per-bar values collide, and every other month
  // label is enough to read the axis. The year sits on the first bar and on
  // each January, both of which land on a labelled slot from the start.
  const dense = series.length > 12;
  const every = dense ? 2 : 1;
  return `<svg class="st-chart lf-chart" viewBox="0 0 ${W} ${H}" role="img"
      aria-label="Loose fruit per bunch by month">
    ${ticks.map(t => `<line class="grid" x1="${L}" x2="${W - R}" y1="${y(t).toFixed(1)}" y2="${y(t).toFixed(1)}"/>
      <text x="${L - 6}" y="${(y(t) + 3).toFixed(1)}" text-anchor="end">${t.toFixed(1)}</text>`).join('')}
    ${series.map((s, i) => {
      const v = s.loose_per_bunch || 0;
      const x = L + slot * i + (slot - bw) / 2;
      return `<rect x="${x.toFixed(1)}" y="${y(v).toFixed(1)}" width="${bw.toFixed(1)}"
          height="${Math.max(0, y(0) - y(v)).toFixed(1)}" fill="var(--accent)"
          opacity="${benchmark && v >= benchmark ? 1 : 0.55}"><title>${esc(s.label)}: ${v.toFixed(2)} per bunch</title></rect>
        ${dense ? '' : `<text x="${(x + bw / 2).toFixed(1)}" y="${(y(v) - 4).toFixed(1)}" text-anchor="middle">${v.toFixed(2)}</text>`}
        ${i % every ? '' : `<text x="${(x + bw / 2).toFixed(1)}" y="${H - 7}" text-anchor="middle">${esc(s.label)}</text>`}`;
    }).join('')}
    ${benchmark ? `<line class="rop" x1="${L}" x2="${W - R}" y1="${y(benchmark).toFixed(1)}" y2="${y(benchmark).toFixed(1)}"/>
      <text class="lbl-rop" x="${W - R - 2}" y="${(y(benchmark) - 5).toFixed(1)}" text-anchor="end">best quarter ${benchmark.toFixed(2)}</text>` : ''}
  </svg>`;
}

export async function panelLooseFruit() {
  const r = await fetch('/gis/loose-fruit?estate=EC&top=12');
  if (!r.ok) return `<div class="empty">The loose fruit endpoint answered ${r.status}.</div>`;
  const d = await r.json();
  if (!d.available) return `<div class="empty">${esc(d.reason)}</div>`;
  const t = d.totals, b = d.benchmark, q = d.distribution, v = d.value;
  const fw = d.falls_window || {};
  const divs = d.by_division || [];
  const lo = divs.reduce((a, r) => (r.loose_per_bunch < a.loose_per_bunch ? r : a), divs[0] || {});
  const hi = divs.reduce((a, r) => (r.loose_per_bunch > a.loose_per_bunch ? r : a), divs[0] || {});

  const cols = [
    { key: 'loose_per_bunch', label: 'Per bunch', num: true, fmt: r2 },
    { key: 'recovery_pct', label: 'Of best quarter', num: true, fmt: p0 },
    { key: 'bunches', label: 'Bunches', num: true, fmt: idr },
    { key: 'records_with_count_pct', label: 'Records with a count', num: true, fmt: p0 },
  ];
  const worstCols = [cols[0], cols[1], { key: 'gap_fruits', label: 'Fruits short', num: true, fmt: idr }, cols[3]];
  const fallCols = [
    { key: 'ratio_first', label: fw.first_short || 'first', num: true, fmt: r2 },
    { key: 'ratio_last', label: fw.last_short || 'last', num: true, fmt: r2 },
    { key: 'delta', label: 'Change', num: true, fmt: x => (x > 0 ? '+' : '') + r2(x) },
    { key: 'bunches', label: 'Bunches', num: true, fmt: idr },
  ];

  return `<div class="sheet-note lf-lead" style="color:var(--text);font-size:12.5px;margin-bottom:12px">${esc(firstSentence(d.summary))}</div>
  <div class="kpis">
    <div class="kpi"><b>${r2(t.loose_per_bunch)}</b><span>loose fruit per bunch</span></div>
    <div class="kpi alert"><b>${p0(b.recovery_pct)}</b><span>of the best-quarter rate</span></div>
    <div class="kpi"><b>${r2(b.loose_per_bunch)}</b><span>best quarter reach</span></div>
    <div class="kpi"><b>${fruits(b.gap_fruits)}</b><span>fruits short of it</span></div>
    <div class="kpi"><b>${b.blocks_below}</b><span>of ${t.blocks} blocks below it</span></div>
  </div>

  <div class="sub-t">By month${d.trend ? ` · ${esc(d.trend)}` : ''}</div>
  ${monthChart(d.by_month, b.loose_per_bunch)}

  <div class="sub-t">Across the ${q.n} blocks</div>
  <table class="tbl">
    <tr><th class="num">Lowest</th><th class="num">Lower quarter</th><th class="num">Median</th>
        <th class="num">Upper quarter</th><th class="num">Highest</th></tr>
    <tr><td class="num">${r2(q.min)}</td><td class="num">${r2(q.q1)}</td>
        <td class="num">${r2(q.median)}</td><td class="num"><b>${r2(q.q3)}</b></td>
        <td class="num">${r2(q.max)}</td></tr>
  </table>

  <div class="sub-t">By division</div>
  <table class="tbl">
    <tr><th>Division</th><th class="num">Blocks</th><th class="num">Bunches</th>
        <th class="num">Per bunch</th><th class="num">Of best quarter</th><th class="num">Blocks below</th></tr>
    ${divs.map(r => `<tr class="${r === lo ? 'hi' : r === hi ? 'good' : ''}">
      <td>${esc(r.label)}</td><td class="num">${r.blocks}</td>
      <td class="num">${idr(r.bunches)}</td><td class="num"><b>${r2(r.loose_per_bunch)}</b></td>
      <td class="num">${p0(r.recovery_pct)}</td><td class="num">${r.blocks_below_benchmark}</td>
    </tr>`).join('')}
  </table>

  <div class="sub-t">Lowest ratio · walk these first</div>
  ${blockList(d.worst_blocks, { cols: worstCols })}

  <div class="sub-t">Fell most, ${esc(fw.first_label || 'first month')} to ${esc(fw.last_label || 'last month')}</div>
  ${(d.biggest_falls || []).length
    ? blockList(d.biggest_falls, { cols: fallCols })
    : '<div class="empty">No block fell over the window.</div>'}

  <div class="sub-t">Best quarter · the benchmark</div>
  ${blockList(d.best_blocks, { cols })}

  <div class="sub-t">What the gap is worth</div>
  <table class="tbl">
    <tr><th>Term</th><th class="num">Value</th><th>Basis</th></tr>
    <tr><td>Fruits short of the benchmark</td><td class="num">${idr(v.gap_fruits)}</td><td>real count</td></tr>
    <tr><td>Weight per loose fruit</td><td class="num">${(v.kg_per_loose_fruit * 1000).toFixed(0)} g</td>
        <td>placeholder, not measured here</td></tr>
    <tr><td>Tonnes</td><td class="num">${r1(v.gap_t)} t</td><td>count × placeholder</td></tr>
    <tr><td>FFB price</td><td class="num">${idr(v.price_idr_kg)} IDR/kg</td><td>assumption register</td></tr>
    <tr class="hi"><td>Value</td><td class="num"><b>${r1(v.gap_idr_m)} M IDR</b></td><td>a floor: loose fruit carries more oil than FFB</td></tr>
  </table>`;
}
