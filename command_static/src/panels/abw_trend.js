/* Average bunch weight trend per block - "Is bunch weight moving, and where?"
 *
 * Reads GET /gis/abw-trend (gis/abw_trend.py). The order on screen is the
 * order a reader needs it: the sentence to act on, the calibration that
 * qualifies every number below it, the figures, then the method and the ask.
 *
 * Charts are inline SVG. The estate line is drawn against a y-axis that spans
 * the divisions, not the estate's own wobble, and every sparkline shares one
 * percent scale - a flat block must look flat, or the panel is selling noise.
 */
import { esc, firstSentence, fmt } from '../lib/fmt.js';
import { blockList } from './_shared.js';

const MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
             'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const mon = m => { const p = String(m || '').split('-'); return MON[+p[1] - 1] || m; };
const num = (v, d = 2) => { const s = fmt(v, d); return s === null ? '—' : s; };
const spct = (v, d = 1) => v === null || v === undefined || Number.isNaN(v)
  ? '—' : `${v > 0 ? '+' : ''}${Number(v).toFixed(d)} %`;
const pctAbs = (v, d = 1) => v === null || v === undefined ? '—' : `${Number(v).toFixed(d)} %`;

/* ── the estate line with the divisions behind it ─────────────────────── */

function trendChart(d) {
  const months = d.months || [];
  const n = months.length;
  const est = (d.estate_series || []).map(p => p.abw_kg);
  const divs = d.division_series || [];
  if (n < 2 || !est.some(v => v !== null)) return '';

  const W = 560, H = 210, L = 46, R = 74, T = 14, B = 26;
  const all = [...est, ...divs.flatMap(s => s.abw_kg)].filter(v => v !== null && v !== undefined);
  let lo = Math.min(...all), hi = Math.max(...all);
  const mean = d.totals.estate_abw_kg || (lo + hi) / 2;
  // Never let the axis zoom into the wobble: at least ±2 % of the estate mean.
  const pad = Math.max((hi - lo) * 0.18, mean * 0.02);
  lo -= pad; hi += pad;
  const x = i => L + (W - L - R) * (n === 1 ? 0.5 : i / (n - 1));
  const y = v => T + (H - T - B) * (1 - (v - lo) / (hi - lo));
  const pts = arr => arr.map((v, i) => v === null || v === undefined ? null
    : `${x(i).toFixed(1)},${y(v).toFixed(1)}`).filter(Boolean).join(' ');
  const ticks = [0, 1, 2, 3].map(k => lo + (hi - lo) * k / 3);

  // Direct labels at the right end, pushed apart so none overlap.
  const labels = [
    ...divs.map(s => {
      const last = [...s.abw_kg].reverse().find(v => v !== null && v !== undefined);
      return { text: `D${s.division_code}`, v: last, cls: 'div' };
    }).filter(l => l.v !== null && l.v !== undefined),
    { text: `Estate ${num(est[est.length - 1], 2)}`, v: est[est.length - 1], cls: 'est' },
  ].map(l => ({ ...l, y: y(l.v) })).sort((a, b) => a.y - b.y);
  const GAP = 11;
  for (let i = 1; i < labels.length; i++) {
    if (labels[i].y - labels[i - 1].y < GAP) labels[i].y = labels[i - 1].y + GAP;
  }
  for (let i = labels.length - 1; i >= 0; i--) {
    const limit = i === labels.length - 1 ? H - B : labels[i + 1].y - GAP;
    if (labels[i].y > limit) labels[i].y = limit;
  }

  const txt = 'font-size:10.5px;font-family:var(--font);fill:var(--muted)';
  const divLines = divs.map(s => `<polyline fill="none" style="stroke:var(--muted);stroke-opacity:.55"
      stroke-width="1.25" stroke-linejoin="round" stroke-linecap="round" points="${pts(s.abw_kg)}">
      <title>Division ${esc(s.division_code)} · ${s.blocks} blocks · mean ${num(s.mean_kg, 2)} kg · ${spct(s.change_pct)} first to last month</title>
    </polyline>`).join('');
  const estPts = est.map((v, i) => v === null || v === undefined ? '' :
    `<circle cx="${x(i).toFixed(1)}" cy="${y(v).toFixed(1)}" r="3.5"
       style="fill:var(--accent);stroke:var(--surface)" stroke-width="2">
       <title>${esc(mon(months[i]))}: ${num(v, 3)} kg over ${num(d.estate_series[i].trips, 0)} trips</title>
     </circle>`).join('');

  return `<svg class="st-chart abw-chart" viewBox="0 0 ${W} ${H}" role="img"
      aria-label="Estate average bunch weight by month, divisions behind it">
    ${ticks.map(t => `<line x1="${L}" x2="${W - R}" y1="${y(t).toFixed(1)}" y2="${y(t).toFixed(1)}"
        style="stroke:var(--border-s)" stroke-width="1"/>
      <text x="${L - 6}" y="${(y(t) + 3.5).toFixed(1)}" text-anchor="end" style="${txt}">${t.toFixed(2)}</text>`).join('')}
    ${months.map((m, i) => `<text x="${x(i).toFixed(1)}" y="${H - 8}" text-anchor="middle" style="${txt}">${esc(mon(m))}</text>`).join('')}
    ${divLines}
    <polyline fill="none" style="stroke:var(--accent)" stroke-width="2" stroke-linejoin="round"
      stroke-linecap="round" points="${pts(est)}"/>
    ${estPts}
    ${labels.map(l => `<text x="${W - R + 6}" y="${(l.y + 3.5).toFixed(1)}"
        style="${l.cls === 'est' ? txt.replace('var(--muted)', 'var(--accent)') + ';font-weight:600' : txt}">${esc(l.text)}</text>`).join('')}
  </svg>
  <div class="st-legend">
    <span><i class="lg-mid"></i>Estate, trip-weighted</span>
    <span><i style="height:0;border-top:1.5px solid var(--muted);opacity:.7"></i>Divisions, labelled at the right</span>
    <span>y-axis ${lo.toFixed(2)}–${hi.toFixed(2)} kg</span>
  </div>`;
}

/* ── a sparkline on a shared percent scale ─────────────────────────────── */

function spark(devs, scale, months) {
  const W = 66, H = 18, P = 2;
  const vals = devs || [];
  if (!vals.some(v => v !== null && v !== undefined)) return '—';
  const n = vals.length;
  const x = i => P + (W - 2 * P) * (n === 1 ? 0.5 : i / (n - 1));
  const y = v => H / 2 - (v / scale) * (H / 2 - P);
  const pts = vals.map((v, i) => v === null || v === undefined ? null
    : `${x(i).toFixed(1)},${y(v).toFixed(1)}`).filter(Boolean).join(' ');
  let lastI = -1;
  for (let i = n - 1; i >= 0 && lastI < 0; i--) if (vals[i] !== null && vals[i] !== undefined) lastI = i;
  const title = vals.map((v, i) => `${mon((months || [])[i] || i + 1)} ${spct(v)}`).join(' · ');
  return `<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" style="vertical-align:middle;overflow:visible"
      role="img" aria-label="${esc(title)}"><title>${esc(title)} (scale ±${scale} %)</title>
    <line x1="${P}" x2="${W - P}" y1="${H / 2}" y2="${H / 2}" style="stroke:var(--border-s)" stroke-width="1"/>
    <polyline fill="none" style="stroke:var(--muted)" stroke-width="1.5" stroke-linejoin="round"
      stroke-linecap="round" points="${pts}"/>
    ${lastI >= 0 ? `<circle cx="${x(lastI).toFixed(1)}" cy="${y(vals[lastI]).toFixed(1)}" r="2.2"
      style="fill:var(--accent);stroke:var(--surface)" stroke-width="1"/>` : ''}
  </svg>`;
}

/* ── the panel ────────────────────────────────────────────────────────── */

export async function panelAbwTrend() {
  const r = await fetch('/gis/abw-trend?estate=EC&top=12');
  if (!r.ok) return `<div class="empty">The weighbridge ledger did not answer (HTTP ${r.status}).</div>`;
  const d = await r.json();
  if (!d.available) return `<div class="empty">${esc(d.reason || 'No bunch-weight series.')}</div>`;

  const t = d.totals, cal = d.calibration || {};
  const months = d.months || [];
  const falling = d.falling || [], rising = d.rising || [];

  // One percent scale for every sparkline in both lists, so a block that
  // barely moved draws as a line that barely moves.
  const maxDev = Math.max(2, ...[...falling, ...rising]
    .flatMap(b => (b.dev_pct || []).filter(v => v !== null && v !== undefined).map(Math.abs)));
  const scale = Math.ceil(maxDev);
  const cols = [
    { key: 'division_code', label: 'Div' },
    { key: 'palm_age_years', label: 'Age', num: true, fmt: v => v === null || v === undefined ? '—' : v },
    { key: 'abw_kg', label: 'ABW kg', num: true, fmt: v => num(v, 2) },
    { key: 'change_pct', label: 'Window', num: true, fmt: v => spct(v) },
    { key: 'slope_pct_month', label: '%/month', num: true, fmt: v => spct(v, 2) },
    { key: 'dev_pct', label: `${mon(months[0])}–${mon(months[months.length - 1])}`, html: true,
      fmt: v => spark(v, scale, months) },
    { key: 'trips', label: 'Trips', num: true, fmt: v => num(v, 0) },
    { key: 'direction', label: 'Read as', fmt: v => v || 'noise' },
  ];

  const moving = t.moving_blocks || 0;
  const ageRows = ((d.age_curve || {}).buckets || []);
  const thin = new Set(((d.age_curve || {}).thin_buckets || []).map(String));
  const widest = (d.spread || {}).widest || [];

  return `
  <p style="margin:6px 0 12px;font-size:12.5px;line-height:1.55;color:var(--text)">${esc(firstSentence(d.summary))}</p>

  <div class="sheet-note" style="margin:0 0 14px;padding:8px 12px;border:1px solid rgba(212,168,75,.4);
      border-radius:8px;background:var(--gold-dim);color:var(--text)">
    <b style="color:var(--gold)">Calibration.</b> ${esc(cal.headline)}
  </div>

  <div class="kpis">
    <div class="kpi"><b>${num(t.estate_abw_kg, 2)} kg</b><span>estate ABW, ${esc(mon(months[0]))}–${esc(mon(months[months.length - 1]))} (calibrated level)</span></div>
    <div class="kpi"><b>${spct(t.window_change_pct)}</b><span>first to last month · ${pctAbs(t.peak_to_trough_pct)} peak to trough</span></div>
    <div class="kpi${moving ? ' warn' : ''}"><b>${moving} of ${t.blocks}</b><span>blocks moving ≥ ${t.move_threshold_pct} % clear of noise · ${t.falling_blocks} falling, ${t.rising_blocks} rising</span></div>
    <div class="kpi"><b>${pctAbs(t.median_cv_pct)}</b><span>trips disagree inside a block-month (median CV, p90 ${pctAbs(t.p90_cv_pct)})</span></div>
  </div>

  <div class="sub-t">Estate bunch weight by month, divisions behind it</div>
  ${trendChart(d)}

  <div class="sub-t">Falling fastest</div>
  ${blockList(falling, { cols })}

  <div class="sub-t">Rising fastest</div>
  ${blockList(rising, { cols })}

  <div class="sub-t">Bunch weight against palm age</div>
  <table class="tbl">
    <tr><th>Palm age</th><th class="num">Blocks</th><th class="num">Trips</th>
        <th class="num">ABW kg</th><th class="num">vs age ${esc((d.age_curve || {}).anchor_age)}</th></tr>
    ${ageRows.map(a => `<tr${thin.has(String(a.age)) ? ' style="color:var(--muted)"' : ''}>
      <td>${esc(a.age)} yr${thin.has(String(a.age)) ? ' <span style="font-size:10px">(thin)</span>' : ''}</td>
      <td class="num">${num(a.blocks, 0)}</td><td class="num">${num(a.trips, 0)}</td>
      <td class="num">${num(a.abw_kg, 2)}</td><td class="num">${spct(a.vs_age9_pct)}</td></tr>`).join('')}
  </table>

  <div class="sub-t">Where the tickets disagree most</div>
  <table class="tbl">
    <tr><th>Block</th><th>Div</th><th>Month</th><th class="num">Trips</th>
        <th class="num">ABW kg</th><th class="num">min–max</th><th class="num">CV</th></tr>
    ${widest.map(w => `<tr class="${(w.cv_pct || 0) >= 4 ? 'hi' : ''}">
      <td>${esc(w.block_label)}</td><td>${esc(w.division_code)}</td><td>${esc(w.label)}</td>
      <td class="num">${num(w.trips, 0)}</td><td class="num">${num(w.abw_kg, 2)}</td>
      <td class="num">${num(w.min_kg, 2)}–${num(w.max_kg, 2)}</td>
      <td class="num">${pctAbs(w.cv_pct)}</td></tr>`).join('')}
  </table>`;
}
