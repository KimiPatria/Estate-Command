/* Realised cutting interval against target.
 *
 * The realised complement of the rotation panel. The rotation panel says which
 * blocks are due against a synthetic target; this one measures, from the
 * client's own cutting days, how long the round actually ran on every block
 * and how it drifted from January to May. The intervals are real; the target
 * and the gang are synthetic, and the screen says which is which.
 */
import { esc, fmt } from '../lib/fmt.js';
import { blockList } from './_shared.js';

// Inline-block: `.kpi span` is display:block, which would turn the badge into a bar.
const REAL = '<span class="prov real" style="display:inline-block;margin-left:4px" title="From the client\'s EPMS harvest export">real</span>';
const SYN = '<span class="prov synthetic" style="display:inline-block;margin-left:4px" title="Generated: gis/data/synthetic/ec_rotation.csv">synthetic</span>';

const n1 = v => v === null || v === undefined ? '—' : fmt(v, Number.isInteger(v) ? 0 : 1);
const days = v => v === null || v === undefined ? '—' : `${n1(v)} d`;
const x = v => v === null || v === undefined ? '—' : `${Number(v).toFixed(2)}×`;
const pc = v => v === null || v === undefined ? '—' : `${Math.round(v)}%`;
const shortDate = s => {
  if (!s) return '—';
  const d = new Date(`${s}T00:00:00`);
  return d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short' });
};

/* Monthly median and P90 round as an SVG line chart, with the synthetic
   target's median as a dashed reference and the Lebaran stop marked. */
function roundChart(series, targetMedian, closure) {
  if (!series || series.length < 2) return '';
  const W = 640, H = 210, L = 38, R = 18, T = 18, B = 30;
  const n = series.length;
  const top = Math.max(...series.map(s => Math.max(s.p90 || 0, s.median || 0)), targetMedian || 0);
  const yMax = Math.ceil((top + 3) / 5) * 5;
  const xAt = i => L + (W - L - R) * (n === 1 ? 0.5 : i / (n - 1));
  const yAt = v => T + (H - T - B) * (1 - v / yMax);
  const ticks = [];
  for (let t = 0; t <= yMax; t += 5) ticks.push(t);
  const pts = key => series.map((s, i) => `${xAt(i).toFixed(1)},${yAt(s[key] || 0).toFixed(1)}`);
  const band = [...pts('p90'), ...pts('median').reverse()].join(' ');
  const txt = 'fill:var(--muted);font-size:10.5px;font-family:var(--font)';

  // The stop fell in the last days of March; mark it just before April's point.
  let lebaran = '';
  if (closure && closure.from) {
    const mi = series.findIndex(s => s.month === closure.from.slice(0, 7));
    if (mi >= 0 && mi + 1 < n) {
      const xm = xAt(mi) + (xAt(mi + 1) - xAt(mi)) * 0.87;
      lebaran = `<line x1="${xm.toFixed(1)}" x2="${xm.toFixed(1)}" y1="${T}" y2="${H - B}"
          style="stroke:var(--text);stroke-width:1;stroke-dasharray:2 3;opacity:.6"/>
        <text x="${(xm - 4).toFixed(1)}" y="${T + 10}" text-anchor="end" style="${txt};fill:var(--text)">Lebaran stop, ${closure.days} d</text>`;
    }
  }
  const target = targetMedian ? `
    <line x1="${L}" x2="${W - R}" y1="${yAt(targetMedian).toFixed(1)}" y2="${yAt(targetMedian).toFixed(1)}"
        style="stroke:var(--gold);stroke-width:1.5;stroke-dasharray:6 4"/>
    <text x="${W - R}" y="${(yAt(targetMedian) - 5).toFixed(1)}" text-anchor="end"
        style="${txt};fill:var(--gold)">synthetic target, median ${n1(targetMedian)} d</text>` : '';

  return `<svg class="ci-chart" viewBox="0 0 ${W} ${H}" role="img"
      style="display:block;width:100%;height:auto;max-width:760px;margin:4px 0 2px"
      aria-label="Median and P90 cutting round by month">
    ${ticks.map(t => `<line x1="${L}" x2="${W - R}" y1="${yAt(t).toFixed(1)}" y2="${yAt(t).toFixed(1)}"
        style="stroke:var(--border-s);stroke-width:1"/>
      <text x="${L - 6}" y="${(yAt(t) + 3).toFixed(1)}" text-anchor="end" style="${txt}">${t}</text>`).join('')}
    ${series.map((s, i) => `<text x="${xAt(i).toFixed(1)}" y="${H - 9}"
        text-anchor="${i === 0 ? 'start' : i === n - 1 ? 'end' : 'middle'}" style="${txt}">${esc(s.label)}${s.partial ? ' (to 23rd)' : ''}</text>`).join('')}
    <polygon points="${band}" style="fill:rgba(63,176,196,.16);stroke:none"/>
    <polyline points="${pts('p90').join(' ')}" style="fill:none;stroke:var(--accent);stroke-width:1;stroke-dasharray:3 3;opacity:.8"/>
    <polyline points="${pts('median').join(' ')}" style="fill:none;stroke:var(--accent);stroke-width:2;stroke-linejoin:round"/>
    ${series.map((s, i) => `<circle cx="${xAt(i).toFixed(1)}" cy="${yAt(s.median || 0).toFixed(1)}" r="3" style="fill:var(--accent)"/>
      <text x="${xAt(i).toFixed(1)}" y="${(yAt(s.median || 0) - 8).toFixed(1)}" text-anchor="middle"
        style="${txt};fill:var(--text);font-weight:600">${n1(s.median)}</text>`).join('')}
    ${target}
    ${lebaran}
  </svg>
  <div class="st-legend"><span><i class="lg-mid"></i>median round</span>
    <span><i class="lg-band"></i>median to P90</span>
    <span><i class="lg-rop"></i>synthetic target</span></div>`;
}

export async function panelCuttingInterval() {
  const r = await fetch('/gis/cutting-interval?estate=EC&top=12');
  if (!r.ok) return `<div class="empty">Cutting interval endpoint answered ${r.status}.</div>`;
  const d = await r.json();
  if (!d.available) return `<div class="empty">${esc(d.reason || 'No cutting-interval data.')}</div>`;

  const t = d.totals, tg = d.target || {}, s = d.series || [];
  const anchor = shortDate(d.window && d.window.anchor);
  const monthKeys = s.map(m => m.month);

  return `<div class="sheet-note" style="margin:0 0 12px;padding:0;border:none;color:var(--text);font-size:12.5px">
    ${esc(d.summary)}</div>

  <div class="kpis">
    <div class="kpi"><b>${days(t.early_round)}</b><span>round, Jan–Feb ${REAL}</span></div>
    <div class="kpi"><b>${days(t.late_round)}</b><span>round, Apr–May ${REAL}</span></div>
    <div class="kpi"><b>${x(t.stretch)}</b><span>stretch ${REAL}</span></div>
    <div class="kpi"><b>${t.gap_over_14_blocks}</b><span>blocks &gt;14 d uncut at ${esc(anchor)} ${REAL}</span></div>
    <div class="kpi"><b>${pc(t.beyond_14_pct)}</b><span>rounds over 14 d ${REAL}</span></div>
    <div class="kpi"><b>${pc(t.beyond_target_pct)}</b><span>rounds beyond target ${SYN}</span></div>
  </div>

  ${roundChart(s, tg.median, d.closure)}

  <table class="tbl">
    <tr><th>Month</th><th class="num">Rounds</th><th class="num">Median</th><th class="num">P90</th>
        <th class="num">Max</th><th class="num">Over 14 d</th><th class="num">Beyond target</th></tr>
    ${s.map(m => `<tr class="${(m.median || 0) > 14 ? 'hi' : ''}">
      <td>${esc(m.label)}${m.partial ? ' <span style="color:var(--muted)">to 23rd</span>' : ''}</td>
      <td class="num">${m.intervals}</td>
      <td class="num"><b>${days(m.median)}</b></td>
      <td class="num">${days(m.p90)}</td>
      <td class="num">${days(m.max)}</td>
      <td class="num">${pc(m.beyond_14_pct)}</td>
      <td class="num" style="color:var(--gold)">${pc(m.beyond_target_pct)}</td>
    </tr>`).join('')}
  </table>
  <div class="blk-cap">Each round is filed under the month the return cut fell in. ${REAL} throughout;
    the target column is the only synthetic figure.</div>

  <div class="sub-t">By division</div>
  <table class="tbl">
    <tr><th>Div</th><th class="num">Blocks</th>
        ${monthKeys.map((k, i) => `<th class="num">${esc(s[i].label.slice(0, 3))}</th>`).join('')}
        <th class="num">Stretch</th><th class="num">&gt;14 d uncut</th></tr>
    ${(d.by_division || []).map(v => `<tr class="${(v.stretch || 0) >= 2 ? 'hi' : ''}">
      <td>${esc(v.division_code)}</td><td class="num">${v.blocks}</td>
      ${monthKeys.map(k => `<td class="num">${n1(v.months[k])}</td>`).join('')}
      <td class="num"><b>${x(v.stretch)}</b></td>
      <td class="num">${v.gap_over_14}</td>
    </tr>`).join('')}
  </table>

  <div class="sub-t">Blocks whose round stretched most</div>
  ${blockList(d.most_stretched, {
    cols: [
      { key: 'division_code', label: 'Div' },
      { key: 'early_round', label: 'Jan–Feb', num: true, fmt: days },
      { key: 'late_round', label: 'Apr–May', num: true, fmt: days },
      { key: 'stretch_vs_own', label: 'Stretch', num: true, fmt: x },
      { key: 'target', label: 'Target', num: true, fmt: days },
      { key: 'beyond_target_pct', label: 'Beyond target', num: true, fmt: pc },
    ],
    caption: 'Stretch is the block\'s April–May round over its own January–February round: real against real. '
      + 'Target and beyond-target are against the synthetic round.',
  })}

  <div class="sub-t">Longest without a cut at ${esc(anchor)}</div>
  ${blockList(d.longest_gap, {
    cols: [
      { key: 'division_code', label: 'Div' },
      { key: 'gap_at_anchor', label: 'Days uncut', num: true, fmt: days },
      { key: 'last_cut', label: 'Last cut', fmt: shortDate },
      { key: 'median_round', label: 'Median round', num: true, fmt: days },
      { key: 'target', label: 'Target', num: true, fmt: days },
      { key: 'synthetic_days_since', label: 'Rotation panel says', num: true, fmt: days },
    ],
    caption: `Days uncut is the export's last day less the block's last real cutting day. `
      + `The rotation panel's figure follows the synthetic ledger and disagrees on ${tg.last_cut_disagrees_blocks} of ${t.blocks} blocks.`,
  })}

  ${(d.by_gang || []).length ? `<div class="sub-t">By gang ${SYN}</div>
  <table class="tbl">
    <tr><th>Gang</th><th class="num">Blocks</th><th class="num">Jan–Feb</th><th class="num">Apr–May</th><th class="num">Stretch</th></tr>
    ${d.by_gang.slice(0, 6).map(g => `<tr>
      <td>${esc(g.gang_code)}</td><td class="num">${g.blocks}</td>
      <td class="num">${days(g.early_round)}</td><td class="num">${days(g.late_round)}</td>
      <td class="num"><b>${x(g.stretch)}</b></td></tr>`).join('')}
  </table>
  <div class="blk-cap">The rounds are real; the gang on each block is invented, so this attribution is illustrative only.</div>` : ''}

  <div class="sheet-note">
    ${d.closure ? `<b>Lebaran.</b> ${esc(d.closure.note)}<br><br>` : ''}
    <b>The target.</b> ${esc(tg.note || '')}<br><br>
    <b>Caveat.</b> ${esc(d.caveat)}<br><br>
    <b>What the client learns on their own data.</b> ${(d.learns && d.learns.on_own_data || []).map(esc).join(' ')}<br>
    <b>What the synthetic layer adds.</b> ${(d.learns && d.learns.synthetic_adds || []).map(esc).join(' ')}<br><br>
    <b>Provenance.</b> ${esc(d.provenance)}. ${esc(d.note)}
  </div>`;
}
