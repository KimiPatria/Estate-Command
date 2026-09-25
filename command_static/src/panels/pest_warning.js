/* Early warning: canopy anomaly against census.
 *
 * Two provenances share one screen and must stay apart: the canopy is a real
 * Sentinel-2 measurement, the census is invented. Every figure below says
 * which it is. The quadrant scatter carries the panel: real vigour anomaly up
 * the side, synthetic incidence along the bottom, and the early-warning
 * quadrant - weak canopy, quiet census - shaded as the thing to act on.
 */
import { esc, firstSentence, fmt, idr, pct } from '../lib/fmt.js';
import { blockChips, blockList } from './_shared.js';

const GROUPS = {
  early_warning: { label: 'Early warning', colour: 'var(--gold)' },
  corroborated:  { label: 'Corroborated',  colour: 'var(--error)' },
  census_only:   { label: 'Census only',   colour: 'var(--accent)' },
  clear:         { label: 'Clear',         colour: 'var(--muted)' },
};

const num = d => (v => v === null || v === undefined ? '—' : fmt(v, d));
const signed = d => v => v === null || v === undefined ? '—' : (v > 0 ? '+' : '') + fmt(v, d);
const rho = v => v === null || v === undefined ? 'n/a' : (v > 0 ? '+' : '') + Number(v).toFixed(2);

export async function panelPestWarning() {
  const r = await fetch('/gis/pest-warning?estate=EC&top=12');
  if (!r.ok) return `<div class="empty">Early warning is unavailable (HTTP ${r.status}).</div>`;
  const d = await r.json();
  if (!d.available) return `<div class="empty">${esc(d.reason || 'Early warning unavailable.')}</div>`;

  const g = d.groups, a = d.agreement, t = d.thresholds;

  const head = `
    <p class="pw-lead">${esc(firstSentence(d.summary))}</p>
    <div class="kpis">
      <div class="kpi alert"><b>${g.early_warning}</b><span>early warning · satellite only</span></div>
      <div class="kpi"><b>${g.corroborated}</b><span>corroborated · weak on both</span></div>
      <div class="kpi"><b>${g.census_only}</b><span>census high · canopy fine</span></div>
      <div class="kpi"><b>${g.clear}</b><span>clear on both</span></div>
      <div class="kpi"><b>${rho(a.spearman_rho)}</b><span>rank agreement ρ (expected &lt; 0)</span></div>
    </div>`;

  const chart = quadrant(d.points || [], t, d.scene);

  const warning = `
    <div class="sub-t">Targeted census, ranked</div>
    <div class="pw-warn">${blockList(d.early_warning, {
      cols: [
        { key: 'ndre_anomaly', label: 'Anomaly', num: true, fmt: signed(3) },
        { key: 'ndre_spread',  label: 'Spread',  num: true, fmt: num(2) },
        { key: 'ganoderma_pct', label: 'Census %', num: true, fmt: v => pct(v) },
        { key: 'coverage_pct', label: 'Inspected', num: true, fmt: v => v === null || v === undefined ? '—' : `${fmt(v, 0)}%` },
        { key: 'why', label: 'Why', fmt: v => String(v || '—').replace(/ canopy/g, '') },
      ],
    })}</div>
    ${g.strongest_ask ? `
      <div class="sub-t">Strongest ask</div>
      <div class="blk-cap" style="margin-top:0">${g.strongest_ask} blocks under
        ${fmt(t.coverage_low_pct, 0)}% inspected · ${idr(g.palms_uninspected_in_ask)} palms unchecked</div>
      ${blockChips(d.strongest_ask.map(x => x.block_label), { title: 'Strongest ask' })}` : ''}`;

  const corroborated = d.corroborated.length ? `
    <div class="sub-t">Weak on both</div>
    ${blockList(d.corroborated, {
      cols: [
        { key: 'ganoderma_pct', label: 'Census %', num: true, fmt: v => pct(v) },
        { key: 'ndre_anomaly', label: 'Anomaly', num: true, fmt: signed(3) },
        { key: 'ganoderma_trend_pct', label: 'Rise R1→R3', num: true, fmt: signed(2) },
        { key: 'treated', label: 'Treated', fmt: v => v ? 'yes' : 'no' },
      ],
    })}` : '';

  const censusOnly = d.census_only.length ? `
    <div class="sub-t">Census high, canopy fine</div>
    ${blockList(d.census_only, {
      cols: [
        { key: 'ganoderma_pct', label: 'Census %', num: true, fmt: v => pct(v) },
        { key: 'ndre_anomaly', label: 'Anomaly', num: true, fmt: signed(3) },
        { key: 'ndre_spread', label: 'Spread', num: true, fmt: num(2) },
        { key: 'treated', label: 'Treated', fmt: v => v ? 'yes' : 'no' },
      ],
    })}` : '';

  const agreement = `
    <div class="sub-t">Do the two agree?</div>
    <div class="pw-verdict">ρ ${rho(a.spearman_rho)} across ${a.blocks_with_both} blocks.</div>
    <table class="tbl pw-rho">
      <tr><th>Pair · expected sign if the disease were real</th><th class="num">ρ</th></tr>
      <tr><td>vigour anomaly (real) vs Ganoderma incidence (synthetic)
            <small>${esc(a.expected_sign)}</small></td>
          <td class="num"><b>${rho(a.spearman_rho)}</b></td></tr>
      ${(a.others || []).map(o => `<tr><td>${esc(o.a)} vs ${esc(o.b)}
            <small>${esc(o.expected)}</small></td>
          <td class="num">${rho(o.rho)}</td></tr>`).join('')}
    </table>`;

  return STYLE + head + chart + warning + corroborated + censusOnly + agreement;
}

/* ── the quadrant scatter ─────────────────────────────────────────────── */

function quadrant(points, t, scene) {
  const pts = points.filter(p => typeof p.ndre_anomaly === 'number');
  if (!pts.length) return '';
  const W = 460, H = 280, L = 46, R = 14, T = 16, B = 40;
  const xs = pts.map(p => p.ganoderma_pct || 0);
  const ys = pts.map(p => p.ndre_anomaly);
  const xMax = Math.max(t.ganoderma_high_pct * 2, Math.ceil(Math.max(...xs)));
  const yMin = Math.min(...ys), yMax = Math.max(...ys);
  const pad = (yMax - yMin) * 0.06 || 0.01;
  const y0 = yMin - pad, y1 = yMax + pad;
  const x = v => L + (W - L - R) * (v / xMax);
  const y = v => T + (H - T - B) * (1 - (v - y0) / (y1 - y0));
  const xc = x(t.ganoderma_high_pct), yc = y(t.anomaly_weak_at);

  const xTicks = [];
  for (let v = 0; v <= xMax; v += 5) xTicks.push(v);
  const yTicks = [y0 + pad, t.anomaly_weak_at, 0, y1 - pad]
    .filter((v, i, arr) => v >= y0 && v <= y1 && arr.indexOf(v) === i)
    .sort((p, q) => p - q);

  const dots = pts.map(p => {
    const c = GROUPS[p.group] || GROUPS.clear;
    const title = `${p.block_label}: anomaly ${p.ndre_anomaly > 0 ? '+' : ''}${p.ndre_anomaly}, `
      + `spread ${p.ndre_spread ?? '—'}, census ${p.ganoderma_pct ?? '—'}%, inspected ${p.coverage_pct ?? '—'}%`;
    return `<circle cx="${x(p.ganoderma_pct || 0).toFixed(1)}" cy="${y(p.ndre_anomaly).toFixed(1)}" r="${p.group === 'clear' ? 2.2 : 3}"
      fill="${c.colour}" fill-opacity="${p.group === 'clear' ? 0.55 : 0.9}"><title>${esc(title)}</title></circle>`;
  }).join('');

  return `
    <svg class="pw-chart" viewBox="0 0 ${W} ${H}" role="img"
         aria-label="Vigour anomaly against census incidence, one point per block">
      <rect class="quad" x="${L}" y="${yc.toFixed(1)}" width="${(xc - L).toFixed(1)}" height="${(H - B - yc).toFixed(1)}"/>
      ${yTicks.map(v => `<line class="grid" x1="${L}" x2="${W - R}" y1="${y(v).toFixed(1)}" y2="${y(v).toFixed(1)}"/>
        <text x="${L - 5}" y="${(y(v) + 3).toFixed(1)}" text-anchor="end">${(v > 0 ? '+' : '') + v.toFixed(2)}</text>`).join('')}
      ${xTicks.map(v => `<text x="${x(v).toFixed(1)}" y="${H - B + 13}" text-anchor="middle">${v}%</text>`).join('')}
      <line class="cut" x1="${xc.toFixed(1)}" x2="${xc.toFixed(1)}" y1="${T}" y2="${H - B}"/>
      <line class="cut" x1="${L}" x2="${W - R}" y1="${yc.toFixed(1)}" y2="${yc.toFixed(1)}"/>
      ${dots}
      <text class="ql warn" x="${L + 5}" y="${H - B - 6}">early warning</text>
      <text class="ql bad" x="${W - R - 4}" y="${H - B - 6}" text-anchor="end">corroborated</text>
      <text class="ql" x="${W - R - 4}" y="${T + 11}" text-anchor="end">census only</text>
      <text class="ql" x="${L + 5}" y="${T + 11}">clear</text>
      <text class="ax" x="${((L + W - R) / 2).toFixed(1)}" y="${H - 4}" text-anchor="middle">confirmed Ganoderma, % of palms inspected · census, synthetic</text>
      <text class="ax" transform="translate(11 ${((T + H - B) / 2).toFixed(1)}) rotate(-90)" text-anchor="middle">NDRE anomaly vs age-matched peers · satellite, real</text>
    </svg>
    <div class="pw-legend">
      ${Object.entries(GROUPS).map(([k, v]) => `<span><i style="background:${v.colour}"></i>${v.label}</span>`).join('')}
      <span class="pw-legend-note">lines at ${t.ganoderma_high_pct}% census and ${t.anomaly_weak_at > 0 ? '+' : ''}${t.anomaly_weak_at} anomaly
        (the bottom fifth); gold above the line is flagged for patchiness, not weakness. Scene ${esc(scene.date)}.</span>
    </div>`;
}

/* Scoped styling for the pieces the shared stylesheet does not carry. */
const STYLE = `<style>
  .pw-lead { margin:0 0 12px; line-height:1.55; }
  .pw-chart { display:block; width:100%; height:auto; max-width:560px; margin:2px 0 6px; }
  .pw-chart .grid { stroke: var(--border-s); stroke-width:1; }
  .pw-chart .cut { stroke: var(--gold); stroke-width:1; stroke-dasharray:5 4; opacity:.8; }
  .pw-chart .quad { fill: var(--gold-dim); }
  .pw-chart text { fill: var(--muted); font-size:10px; font-family: var(--font); }
  .pw-chart .ql { font-size:10.5px; font-weight:600; text-transform:uppercase; letter-spacing:.03em; }
  .pw-chart .ql.warn { fill: var(--gold); }
  .pw-chart .ql.bad { fill: var(--error); }
  .pw-chart .ax { font-size:10px; }
  .pw-legend { display:flex; flex-wrap:wrap; gap:6px 14px; font-size:11px; color: var(--muted); margin-bottom:14px; }
  .pw-legend i { display:inline-block; width:9px; height:9px; border-radius:50%; margin-right:5px; vertical-align:-1px; }
  .pw-legend-note { flex-basis:100%; line-height:1.45; }
  .pw-verdict { font-size:12px; line-height:1.55; margin-bottom:8px; }
  .pw-warn table td:last-child { white-space:normal; line-height:1.3; font-size:11px; color: var(--muted); }
  .pw-rho td { font-size:11.5px; }
  .pw-rho td small { display:block; color: var(--muted); font-size:10.5px; line-height:1.4; }
</style>`;
