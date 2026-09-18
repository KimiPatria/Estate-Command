/* Metric agreement: do two independent measurements say the same thing?
 *
 * `/gis/metrics/compare` has existed since the build and the UI never called
 * it once, which is odd, because the finding it produces is one of the
 * headlines of the whole project: satellite canopy vigour and recorded yield
 * rank this estate almost independently (Spearman rho 0.10), and the thirteen
 * blocks weak on both are therefore corroborated by two measurements that
 * share no instrument, no operator and no error.
 *
 * That last point is the argument. One weak reading is a lead. Two independent
 * weak readings on the same block is a reason to send someone.
 */
import { esc, fmt } from '../lib/fmt.js';
import { S } from '../state/store.js';

/* Where the rho sits, in words the room can use. */
function strength(rho) {
  const a = Math.abs(rho);
  if (a >= 0.7) return { cls: 'ok', word: 'strong' };
  if (a >= 0.4) return { cls: 'partial', word: 'moderate' };
  if (a >= 0.2) return { cls: 'partial', word: 'weak' };
  return { cls: 'unavailable', word: 'none' };
}

function blockTable(rows, a, b) {
  if (!rows || !rows.length) return '<div class="empty">None.</div>';
  return `<table class="tbl">
    <tr><th>Block</th><th>Div</th><th class="num">${esc(a.label)}</th>
        <th class="num">${esc(b.label)}</th><th class="num">Planted ha</th></tr>
    ${rows.map(r => `<tr>
      <td>${esc(r.block_label)}</td><td>${esc(r.division_code)}</td>
      <td class="num">${fmt(r[a.key], 3)}</td>
      <td class="num">${fmt(r[b.key], 1)}</td>
      <td class="num">${fmt(r.planted_ha, 2)}</td>
    </tr>`).join('')}
  </table>`;
}

export async function panelCompare(metricB) {
  const a = S.metric;
  const b = metricB;
  const qs = new URLSearchParams({ estate: S.estate, metric_a: a, metric_b: b });
  if (S.month) qs.set('month', S.month);
  const d = await (await fetch(`/gis/metrics/compare?${qs}`)).json();
  if (d.error) {
    return `<div class="empty">${esc(d.error)}${
      d.recovery ? `<br><br>${esc(d.recovery)}` : ''}</div>`;
  }

  const s = strength(d.spearman_rho);

  return `
    <div class="kpis">
      <div class="kpi"><b>${d.spearman_rho}</b><span>Spearman rho</span></div>
      <div class="kpi alert"><b>${d.weak_on_both_count}</b><span>weak on both</span></div>
      <div class="kpi"><b>${d.blocks_with_both}</b><span>blocks measured on both</span></div>
      <div class="kpi"><b>${d.bottom_fifth_size}</b><span>in each bottom fifth</span></div>
    </div>

    <div class="cap">
      <div class="cap-h">
        <h4>${esc(d.metric_a.label)} against ${esc(d.metric_b.label)}</h4>
        <span class="status ${s.cls}">${s.word}</span>
      </div>
      <div class="sheet-note" style="margin:0;border:none;padding:0">
        ${esc(d.reading)}</div>
      <div class="blk-cap">
        <span class="prov ${d.metric_a.provenance}">${esc(d.metric_a.provenance)}</span>
        ${esc(d.metric_a.label)} &nbsp;·&nbsp;
        <span class="prov ${d.metric_b.provenance}">${esc(d.metric_b.provenance)}</span>
        ${esc(d.metric_b.label)} &nbsp;·&nbsp; ${esc(d.month)}
      </div>
    </div>

    <div class="sub-t">Weak on both — corroborated</div>
    <div class="sheet-note" style="margin:0 0 9px;border:none;padding:0">
      Two measurements that share no instrument agree about these blocks. That
      is evidence, not a lead.</div>
    ${blockTable(d.weak_on_both, d.metric_a, d.metric_b)}

    <div class="sub-t">Weak on ${esc(d.metric_a.label)} only</div>
    ${blockTable(d.weak_on_a_only, d.metric_a, d.metric_b)}

    <div class="sub-t">Weak on ${esc(d.metric_b.label)} only</div>
    ${blockTable(d.weak_on_b_only, d.metric_a, d.metric_b)}

    <div class="sheet-note"><b>Read this carefully.</b> ${esc(d.note)}</div>`;
}
