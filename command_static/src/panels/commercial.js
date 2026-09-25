import { idr, sign } from '../lib/fmt.js';
import { S } from '../state/store.js';

export async function panelContract() {
  const d = S.contract = await (await fetch('/gis/contract?estate=EC')).json();
  const w = d.worst_forward_month;
  return `<table class="tbl">
    <tr><th>Month</th><th class="num">Produced t</th><th class="num">P10 t</th>
        <th class="num">Committed t</th><th class="num">Gap t</th><th class="num">Gap at P10</th></tr>
    ${d.months.map(r => `<tr class="${r.gap_t < 0 ? 'hi' : ''}">
      <td>${r.month}${r.is_forecast ? ' <span class="prov synthetic">fcst</span>' : ''}</td>
      <td class="num">${idr(Math.round(r.produced_t))}</td>
      <td class="num">${r.band_t ? idr(Math.round(r.band_t[0])) : '—'}</td>
      <td class="num">${idr(r.committed_t)}</td>
      <td class="num">${sign(r.gap_t)}</td>
      <td class="num">${r.gap_p10_t !== null ? sign(r.gap_p10_t) : '—'}</td>
    </tr>`).join('')}
  </table>
  <div class="sheet-note">
    ${d.months_in_p50_deficit} of 6 forecast months are short at P50, and
    ${d.months_in_p10_deficit} are short at P10. Worst is <b>${w.month}</b> at
    ${idr(Math.round(w.gap_t))} t.
  </div>
  <div class="acts" style="margin-top:14px;max-width:340px">
    <button class="accept" data-decide='${JSON.stringify({
      use_case: 'UC-08 contract position', title: `Cover the ${w.month} shortfall`,
      subject: w.month, action: 'accepted', artifact_kind: 'purchase_requisition',
      shortfall_t: Math.abs(w.gap_t),
      evidence: [`P50 gap ${Math.round(w.gap_t)} t in ${w.month}`,
                 `${d.months_in_p10_deficit} of 6 forecast months short at P10`]
    }).replace(/'/g, "&#39;")}'>Raise requisition</button>
    <button data-decide='${JSON.stringify({
      use_case: 'UC-08 contract position', title: `Accept the ${w.month} shortfall`,
      subject: w.month, action: 'deferred', evidence: [`P50 gap ${Math.round(w.gap_t)} t`]
    }).replace(/'/g, "&#39;")}'>Defer</button>
  </div>
  <div class="stub" id="act-note"></div>`;
}

export async function panelVendors() {
  const gap = S.contract && S.contract.worst_forward_month
    ? Math.abs(S.contract.worst_forward_month.gap_t) : 2000;
  const d = await (await fetch(`/gis/vendors?shortfall_t=${gap}`)).json();
  return `<table class="tbl">
    <tr><th>#</th><th>Vendor</th><th class="num">km</th><th class="num">Price</th>
        <th class="num">Transport</th><th class="num">Quality</th>
        <th class="num">Reliability</th><th class="num">Landed</th></tr>
    ${d.vendors.map(v => `<tr class="${v.rank === 1 ? 'good' : ''}">
      <td>${v.rank}</td><td>${v.name}</td>
      <td class="num">${v.distance_km}</td>
      <td class="num">${idr(v.price_idr_per_kg)}</td>
      <td class="num">${idr(v.transport_idr_per_kg)}</td>
      <td class="num">${idr(v.quality_adj_idr_per_kg)}</td>
      <td class="num">${idr(v.reliability_adj_idr_per_kg)}</td>
      <td class="num"><b>${idr(v.landed_idr_per_kg)}</b></td>
    </tr>`).join('')}
  </table>
  <div class="sheet-note">
    Cheapest price: <b>${d.cheapest_headline}</b>. Best buy: <b>${d.best_landed}</b>.<br>
    Allocation for a ${idr(Math.round(gap))} t shortfall:
    ${d.allocation.map(a => `${a.name} ${idr(a.tonnes)} t`).join(', ') || 'none needed'}.
  </div>`;
}

