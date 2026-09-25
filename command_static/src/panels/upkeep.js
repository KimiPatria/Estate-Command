import { esc, idr, pct } from '../lib/fmt.js';

export async function panelReplant() {
  const d = await (await fetch('/gis/replant?estate=EC')).json();
  return `<table class="tbl">
    <tr><th>Replant year</th><th class="num">Blocks</th><th class="num">ha</th>
        <th class="num">% of estate</th><th class="num">Immature until</th></tr>
    ${d.schedule.map(s => `<tr class="${s.pct_of_estate > 30 ? 'hi' : ''}">
      <td>${s.replant_year}</td><td class="num">${s.blocks}</td>
      <td class="num">${idr(s.ha)}</td>
      <td class="num"><b>${s.pct_of_estate}%</b></td>
      <td class="num">${s.immature_until}</td>
    </tr>`).join('')}
  </table>
  <div class="sheet-note">${d.finding}</div>`;
}


export async function panelNutrition() {
  const d = await (await fetch('/gis/nutrition?estate=EC&top=12')).json();
  if (!d.available) return `<div class="empty">${esc(d.reason)}</div>`;
  const t = d.totals;
  return `<div class="kpis">
      <div class="kpi"><b>${idr(Math.round(t.target_kg / 1000))} t</b><span>programme</span></div>
      <div class="kpi"><b>${idr(Math.round(t.issued_kg / 1000))} t</b><span>issued</span></div>
      <div class="kpi alert"><b>${pct(t.gap_pct)}</b><span>short of programme</span></div>
      <div class="kpi"><b>${t.mean_lag_days} d</b><span>mean application lag</span></div>
      <div class="kpi"><b>${t.blocks_under_80pct}</b><span>blocks under 80%</span></div>
    </div>
  <div class="sub-t">By material</div>
  <table class="tbl">
    <tr><th>Material</th><th class="num">Programme kg</th>
        <th class="num">Issued kg</th><th class="num">Short</th></tr>
    ${d.by_material.map(m => `<tr class="${m.gap_pct > 15 ? 'hi' : ''}">
      <td>${esc(m.material)}</td><td class="num">${idr(m.target_kg)}</td>
      <td class="num">${idr(m.issued_kg)}</td><td class="num">${pct(m.gap_pct)}</td>
    </tr>`).join('')}
  </table>
  <div class="sub-t">Stock cover against lead time</div>
  <table class="tbl">
    <tr><th>Material</th><th class="num">On hand kg</th><th class="num">Days cover</th>
        <th class="num">Quoted lead</th><th>Old flag</th><th>The store says</th></tr>
    ${d.stock.map(s => `<tr class="${s.status === 'order_now' || s.status === 'this_week' ? 'hi' : ''}">
      <td>${esc(s.material)}</td><td class="num">${idr(s.stock_on_hand_kg)}</td>
      <td class="num">${s.days_cover}</td><td class="num">${s.lead_time_days}</td>
      <td>${s.short_before_resupply ? 'runs dry first' : 'covered'}</td>
      <td>${s.headline ? esc(s.headline) : '—'}</td>
    </tr>`).join('')}
  </table>
  <div class="fp-actbar"><button class="ops-btn small" data-open-panel="stores">Open the store</button></div>
  <div class="sub-t">Blocks furthest below programme</div>
  <table class="tbl">
    <tr><th>Block</th><th>Div</th><th class="num">Short</th>
        <th class="num">kg/palm</th><th class="num">Bunches/ha</th></tr>
    ${d.worst_blocks.map(r => `<tr>
      <td>${esc(r.block_label)}</td><td>${esc(r.division_code)}</td>
      <td class="num">${pct(r.nutrient_gap_pct)}</td>
      <td class="num">${r.kg_per_palm}</td>
      <td class="num">${r.bunches_per_ha === null ? '—' : idr(r.bunches_per_ha)}</td>
    </tr>`).join('')}
  </table>
`;
}

export async function panelRoads() {
  const d = await (await fetch('/gis/roads?estate=EC&top=12')).json();
  if (!d.available) return `<div class="empty">${esc(d.reason)}</div>`;
  const t = d.totals, h = d.haulage_effect;
  return `<div class="kpis">
      <div class="kpi"><b>${t.segments}</b><span>segments</span></div>
      <div class="kpi"><b>${t.km} km</b><span>estate roads</span></div>
      <div class="kpi"><b>${t.mean_days_since_graded} d</b><span>since grading</span></div>
      <div class="kpi"><b>${t.culvert_repairs_12m}</b><span>culvert repairs</span></div>
    </div>
  <table class="tbl">
    <tr><th>Condition</th><th class="num">Segments</th><th class="num">km</th></tr>
    ${d.by_condition.map(c => `<tr class="${c.condition.startsWith('impass') ? 'hi' : ''}">
      <td>${esc(c.condition)}</td><td class="num">${c.segments}</td>
      <td class="num">${c.km}</td></tr>`).join('')}
  </table>
  <div class="sub-t">What condition costs in haulage</div>
  <table class="tbl">
    <tr><th>Roads</th><th class="num">Blocks</th><th class="num">Turnaround h</th>
        <th class="num">Diesel L/t</th></tr>
    <tr><td>Poor or worse</td><td class="num">${h.poor_roads_blocks}</td>
      <td class="num">${h.turnaround_h_poor}</td><td class="num">${h.diesel_per_t_poor}</td></tr>
    <tr class="good"><td>Good or fair</td><td class="num">${h.good_roads_blocks}</td>
      <td class="num">${h.turnaround_h_good}</td><td class="num">${h.diesel_per_t_good}</td></tr>
  </table>

  <div class="sub-t">Worst segments</div>
  <table class="tbl">
    <tr><th>Block</th><th>Div</th><th>Condition</th>
        <th class="num">Days since graded</th><th class="num">Turnaround h</th></tr>
    ${(d.worst_segments || []).map(r => `<tr class="${
        r.condition === 'impassable-when-wet' ? 'hi' : ''}">
      <td>${esc(r.block_label)}</td><td>${esc(r.division_code)}</td>
      <td>${esc(r.condition)}</td>
      <td class="num">${r.days_since_graded}</td>
      <td class="num">${r.turnaround_h}</td>
    </tr>`).join('')}
  </table>

`;
}

/* ── the models ─────────────────────────────────────────────────────────── */

