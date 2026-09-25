import { esc, idr, pct } from '../lib/fmt.js';

export async function panelPest(view) {
  const d = await (await fetch('/gis/pest?estate=EC&top=12')).json();
  if (!d.available) return `<div class="empty">${esc(d.reason)}</div>`;
  const c = d.census;

  const head = `<div class="kpis">
    <div class="kpi"><b>${idr(c.palms_inspected)}</b><span>palms inspected</span></div>
    <div class="kpi alert"><b>${pct(c.incidence_pct)}</b><span>ganoderma confirmed</span></div>
    <div class="kpi"><b>${pct(c.suspect_pct)}</b><span>suspect</span></div>
    <div class="kpi"><b>${d.blocks_over_5pct}</b><span>blocks over 5%</span></div>
    <div class="kpi"><b>${c.mean_coverage_pct}%</b><span>census coverage</span></div>
  </div>`;

  if (view === 'pest_spread') {
    return head + `<table class="tbl">
      <tr><th>Block</th><th>Div</th><th class="num">Own %</th>
          <th class="num">Neighbours %</th><th class="num">Exposure</th></tr>
      ${d.spread_risk.map(r => `<tr class="${r.exposure > 2 ? 'hi' : ''}">
        <td>${esc(r.block_label)}</td><td>${esc(r.division_code)}</td>
        <td class="num">${r.ganoderma_pct}</td>
        <td class="num">${r.neighbour_mean_pct}</td>
        <td class="num"><b>+${r.exposure}</b></td>
      </tr>`).join('')}
    </table>
`;
  }

  if (view === 'pest_treatment') {
    return head + `<table class="tbl">
      <tr><th>Block</th><th>Div</th><th class="num">Follow-up overdue, days</th></tr>
      ${d.treatment_overdue.map(r => `<tr class="${r.treatment_overdue > 120 ? 'hi' : ''}">
        <td>${esc(r.block_label)}</td><td>${esc(r.division_code)}</td>
        <td class="num">${r.treatment_overdue}</td>
      </tr>`).join('')}
    </table>
    <div class="sheet-note">${d.blocks_untreated} blocks have no treatment recorded
      at all.</div>`;
  }

  const rows = view === 'pest_damage' ? d.fastest_rising : d.worst_blocks;
  const title = view === 'pest_damage' ? 'Fastest rising' : 'Worst blocks';
  return head + `<div class="sub-t">${title}</div>
  <table class="tbl">
    <tr><th>Block</th><th>Div</th><th class="num">Incidence</th>
        <th class="num">Confirmed</th><th class="num">Change</th></tr>
    ${rows.map(r => `<tr class="${(r.ganoderma_pct || 0) > 8 ? 'hi' : ''}">
      <td>${esc(r.block_label)}</td><td>${esc(r.division_code)}</td>
      <td class="num">${pct(r.ganoderma_pct)}</td>
      <td class="num">${idr(r.ganoderma_confirmed)}</td>
      <td class="num">${r.ganoderma_trend_pct === null || r.ganoderma_trend_pct === undefined
        ? '—' : '+' + r.ganoderma_trend_pct}</td>
    </tr>`).join('')}
  </table>
`;
}

/* ── nutrition, roads ───────────────────────────────────────────────────── */

