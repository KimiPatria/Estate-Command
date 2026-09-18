import { esc, idr, pct } from '../lib/fmt.js';

export async function panelShrinkage() {
  const d = await (await fetch('/gis/shrinkage?top=12')).json();
  if (!d.available) return `<div class="empty">${esc(d.reason)}</div>`;
  const t = d.totals, v = d.validation, f = d.flags;

  const scoreRow = (name, s) => `<tr>
    <td>${name}</td><td class="num">${s.flagged}</td>
    <td class="num">${s.true_positives}</td><td class="num">${s.false_positives}</td>
    <td class="num">${s.precision === null ? '—' : s.precision}</td>
    <td class="num">${s.recall === null ? '—' : s.recall}</td></tr>`;

  return `<div class="kpis">
      <div class="kpi"><b>${idr(t.trips)}</b><span>trips</span></div>
      <div class="kpi"><b>${idr(Math.round(t.expected_t))} t</b><span>expected</span></div>
      <div class="kpi"><b>${idr(Math.round(t.weighed_t))} t</b><span>weighed in</span></div>
      <div class="kpi alert"><b>${idr(Math.round(t.gap_t))} t</b><span>unaccounted</span></div>
      <div class="kpi"><b>${pct(t.shrinkage_pct)}</b><span>shrinkage</span></div>
    </div>

  <div class="sub-t">Injected versus recovered</div>
  <table class="tbl">
    <tr><th>Detector</th><th class="num">Flagged</th><th class="num">Correct</th>
        <th class="num">False</th><th class="num">Precision</th><th class="num">Recall</th></tr>
    ${scoreRow('Robust z-score', v.robust_z)}
    ${scoreRow('Isolation forest', v.isolation_forest)}
    ${scoreRow('Both agree', v.corroborated)}
  </table>
  <div class="sheet-note">${esc(v.reading)}</div>

  ${v.ablation && v.ablation.available ? `
  <div class="sub-t">What the planted answers caught us doing wrong</div>
  <table class="tbl">
    <tr><th>Feature set</th><th class="num">Columns</th>
        <th class="num">Precision</th><th class="num">Recall</th></tr>
    <tr class="good"><td>Deviation only</td>
      <td class="num">${v.ablation.narrow.features.length}</td>
      <td class="num">${v.ablation.narrow.precision}</td>
      <td class="num">${v.ablation.narrow.recall}</td></tr>
    <tr class="hi"><td>With haulage context</td>
      <td class="num">${v.ablation.wide.features.length}</td>
      <td class="num">${v.ablation.wide.precision}</td>
      <td class="num">${v.ablation.wide.recall}</td></tr>
  </table>
  <div class="sheet-note">${esc(v.ablation.reading)}</div>` : ''}

  <div class="sub-t">Where the loss concentrates</div>
  <table class="tbl">
    <tr><th>Driver</th><th class="num">Trips</th><th class="num">Flagged</th>
        <th class="num">Shrinkage</th></tr>
    ${d.by_driver.slice(0, 5).map(r => `<tr class="${r.flagged > 5 ? 'hi' : ''}">
      <td>${esc(r.driver_id)}</td><td class="num">${idr(r.trips)}</td>
      <td class="num">${r.flagged}</td><td class="num">${pct(r.shrinkage_pct)}</td>
    </tr>`).join('')}
  </table>

  <div class="sub-t">Worst trips</div>
  <table class="tbl">
    <tr><th>Trip</th><th>Date</th><th>Block</th><th>Driver</th>
        <th class="num">Expected</th><th class="num">Weighed</th>
        <th class="num">Short</th><th class="num">z</th></tr>
    ${d.worst_trips.map(r => `<tr class="${r.corroborated ? 'hi' : ''}">
      <td>${esc(r.trip_id)}</td><td>${esc(r.date)}</td><td>${esc(r.block)}</td>
      <td>${esc(r.driver_id)}</td>
      <td class="num">${idr(r.expected_kg)}</td><td class="num">${idr(r.net_kg)}</td>
      <td class="num">${pct(r.deviation_pct)}</td><td class="num">${r.robust_z}</td>
    </tr>`).join('')}
  </table>
  <div class="sheet-note">
    <b>Method.</b> ${esc(d.method.expected)}. ${esc(d.method.primary)};
    ${esc(d.method.secondary)}. ${esc(d.method.note)}<br><br>
    <b>Provenance.</b> ${esc(d.provenance)}
  </div>
  <div class="acts" style="margin-top:14px;max-width:380px">
    <button class="accept" data-decide='${JSON.stringify({
      use_case: 'Transport, field-to-mill shrinkage',
      title: `Investigate ${f.corroborated} corroborated short trips`,
      subject: 'shrinkage', action: 'accepted',
      evidence: [`${f.either} trips flagged of ${t.trips}`,
                 `${t.gap_t} t unaccounted across the window`]
    }).replace(/'/g, "&#39;")}'>Raise an investigation</button>
    <button data-decide='${JSON.stringify({
      use_case: 'Transport, field-to-mill shrinkage',
      title: 'Accept current shrinkage', subject: 'shrinkage', action: 'deferred',
      evidence: [`Estate shrinkage ${t.shrinkage_pct}%`]
    }).replace(/'/g, "&#39;")}'>Defer</button>
  </div>
  <div class="stub" id="act-note"></div>`;
}

/* Turnaround, fuel and fleet all read the same payload and differ only in
   which half of it leads, so one renderer serves the three panels. */

export async function panelClusters() {
  const d = await (await fetch('/gis/clusters?estate=EC')).json();
  if (!d.available) return `<div class="empty">${esc(d.reason)}</div>`;
  return `<div class="kpis">
      <div class="kpi"><b>${d.clusters}</b><span>groups found</span></div>
      <div class="kpi"><b>${d.blocks}</b><span>blocks</span></div>
      <div class="kpi"><b>${d.unclustered}</b><span>in no group</span></div>
      <div class="kpi"><b>${d.feature_provenance.real.length}/${d.features.length}</b>
        <span>axes are real</span></div>
    </div>
  <table class="tbl">
    <tr><th>Group</th><th class="num">Blocks</th><th class="num">Peer index</th>
        <th class="num">Bunches/ha</th><th>Characterised by</th></tr>
    ${d.groups.map(g => `<tr class="${d.worst_group && g.cluster === d.worst_group.cluster ? 'hi' : ''}">
      <td>${g.cluster}</td><td class="num">${g.blocks}</td>
      <td class="num">${g.mean_peer_index}</td>
      <td class="num">${idr(g.mean_bunches_per_ha)}</td>
      <td>${esc(g.label)}</td>
    </tr>`).join('')}
  </table>
  ${d.worst_group ? `<div class="sub-t">Weakest group against its own planting cohort</div>
  <table class="tbl">
    <tr><th>Axis</th><th>Direction</th><th class="num">z</th><th>Source</th></tr>
    ${d.worst_group.traits.map(t => `<tr>
      <td>${esc(t.label)}</td><td>${esc(t.direction)}</td>
      <td class="num">${t.z}</td>
      <td><span class="prov ${t.provenance}">${esc(t.provenance)}</span></td>
    </tr>`).join('')}
  </table>
  <div class="sheet-note">Blocks:
    ${d.worst_group.block_labels.map(esc).join(', ')}</div>` : ''}
  <div class="sheet-note"><b>Read this carefully.</b> ${esc(d.warning)}<br><br>
    <b>Method.</b> ${esc(d.method.algorithm)}. ${esc(d.method.labelling)}.
    ${esc(d.method.unclustered)}<br><br>
    <b>Provenance.</b> ${esc(d.provenance)}</div>`;
}

export async function panelProductivity() {
  const d = await (await fetch('/gis/productivity?estate=EC&top=10')).json();
  if (!d.available) return `<div class="empty">${esc(d.reason)}</div>`;
  const m = d.model, r = d.recovery;
  return `<div class="kpis">
      <div class="kpi"><b>${idr(d.worker_days)}</b><span>worker-days</span></div>
      <div class="kpi"><b>${d.flat_quota}</b><span>flat quota would be</span></div>
      <div class="kpi alert"><b>${d.target_range[0]}–${d.target_range[1]}</b>
        <span>fair target range</span></div>
      <div class="kpi"><b>${m.r_squared}</b><span>R²</span></div>
    </div>
  <div class="sheet-note">A single estate-wide quota of ${d.flat_quota} bunches is
    ${d.flat_quota_error[0]} too high on the hardest blocks and
    ${d.flat_quota_error[1]} too low on the easiest. That gap is why cutters leave
    the difficult blocks half-collected.</div>
  <div class="sub-t">What the model reads</div>
  <table class="tbl">
    <tr><th>Condition</th><th class="num">Coefficient</th><th>Source</th></tr>
    ${m.coefficients.map(c => `<tr>
      <td>${esc(c.label)}</td><td class="num">${c.coefficient}</td>
      <td><span class="prov ${c.provenance}">${esc(c.provenance)}</span></td>
    </tr>`).join('')}
  </table>
  ${r && r.available ? `<div class="sub-t">Fitted against what the generator put in</div>
  <table class="tbl">
    <tr><th>Condition</th><th class="num">Generator</th><th class="num">Fitted</th>
        <th class="num">Overstated</th></tr>
    ${r.coefficients.map(c => `<tr>
      <td>${esc(c.label)}</td><td class="num">${c.generator_effect}</td>
      <td class="num">${c.fitted_coefficient}</td>
      <td class="num">×${c.overstated_by}</td></tr>`).join('')}
  </table>
  <div class="sheet-note">${esc(r.reading)}</div>` : ''}
  <div class="sub-t">Hardest blocks to cut</div>
  <table class="tbl">
    <tr><th>Block</th><th class="num">Fair target</th><th class="num">Actual</th>
        <th class="num">Slope°</th><th class="num">Palm age</th></tr>
    ${d.hardest_blocks.map(t => `<tr>
      <td>${esc(t.block)}</td><td class="num"><b>${t.adjusted_target}</b></td>
      <td class="num">${t.actual_mean}</td>
      <td class="num">${t.slope_deg}</td><td class="num">${t.palm_age_years}</td>
    </tr>`).join('')}
  </table>
  <div class="sheet-note"><b>${esc(d.warning)}</b><br><br>
    <b>Method.</b> ${esc(m.note)}<br><br>
    <b>Provenance.</b> ${esc(d.provenance)}</div>`;
}

export async function panelForecast() {
  const d = await (await fetch('/gis/forecast/lagged?estate=EC')).json();
  if (!d.available) return `<div class="empty">${esc(d.reason)}</div>`;
  const m = d.model, b = d.biology, q = d.data_requirement;

  const maxImp = Math.max(...d.lag_importance.map(l => l.importance)) || 1;
  const bars = d.lag_importance.map(l => {
    const inSex = l.lag_months >= b.sex_determination_window[0]
      && l.lag_months <= b.sex_determination_window[1];
    const inAb = l.lag_months >= b.abortion_window[0]
      && l.lag_months <= b.abortion_window[1];
    const col = inSex ? 'var(--ok)' : inAb ? 'var(--accent)' : 'var(--gold)';
    return `<div style="display:flex;align-items:center;gap:7px;font-size:11px">
      <span style="width:58px;color:var(--muted)">lag ${l.lag_months}</span>
      <span style="flex:1;background:var(--surface-2);border-radius:3px;height:10px">
        <span style="display:block;height:10px;border-radius:3px;background:${col};
          width:${Math.max(1, 100 * l.importance / maxImp)}%"></span></span>
    </div>`;
  }).join('');

  return `<div class="kpis">
      <div class="kpi"><b>${d.months}</b><span>months modelled</span></div>
      <div class="kpi alert"><b>${q.has_months}</b><span>months you supplied</span></div>
      <div class="kpi"><b>${m.mae_test_bunches_per_ha}</b><span>holdout MAE bunches/ha</span></div>
      <div class="kpi"><b>${m.mape_test_pct}%</b><span>holdout error</span></div>
    </div>
  <div class="sheet-note"><b>${esc(q.statement)}</b></div>
  <div class="sub-t">Where the model looked, lag 1 to 24 months</div>
  ${bars}
  <div class="sheet-note">
    Green is the sex-determination window (${b.sex_determination_window.join('–')} months),
    blue is the abortion window (${b.abortion_window.join('–')} months).
    Only ${b.share_in_sex_window_pct}% of rainfall importance landed in the first
    and ${b.share_in_abortion_window_pct}% in the second.<br><br>
    ${esc(b.finding)}
  </div>
  <div class="sheet-note"><b>What this does and does not show.</b> ${esc(d.honesty)}
    <br><br><b>Provenance.</b> ${esc(d.provenance)}</div>`;
}

