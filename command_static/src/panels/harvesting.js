import { idr } from '../lib/fmt.js';

export async function panelRotation() {
  const d = await (await fetch('/gis/rotation?estate=EC&top=20')).json();
  return `<div class="sheet-note" style="margin:0 0 12px;padding:0;border:none">
    <b>${d.overdue_blocks}</b> blocks past their target round, covering
    <b>${idr(d.overdue_ha)}</b> ha.</div>
  <table class="tbl">
    <tr><th>Block</th><th>Div</th><th>Gang</th><th class="num">Days since</th>
        <th class="num">Target</th><th class="num">Pressure</th><th class="num">ha</th></tr>
    ${d.queue.map(b => `<tr class="${b.ripeness_pressure > 1 ? 'hi' : ''}">
      <td>${b.block_label}</td><td>${b.division_code}</td><td>${b.gang_code}</td>
      <td class="num">${b.days_since_harvest}</td>
      <td class="num">${b.rotation_target_days}</td>
      <td class="num"><b>${b.ripeness_pressure}</b></td>
      <td class="num">${b.planted_ha}</td>
    </tr>`).join('')}
  </table>
  <div class="acts" style="margin-top:14px;max-width:340px">
    <button class="accept" data-decide='${JSON.stringify({
      use_case: 'UC-01 harvest rotation',
      title: `${d.overdue_blocks} blocks overdue for harvest`,
      subject: `${d.overdue_ha} ha past target round`, action: 'accepted',
      artifact_kind: 'harvesting_plan', rotation: true,
      evidence: [`${d.overdue_blocks} blocks past their target round`,
                 `${d.overdue_ha} ha affected`]
    }).replace(/'/g, "&#39;")}'>Draft harvesting plan</button>
    <button data-decide='${JSON.stringify({
      use_case: 'UC-01 harvest rotation', title: 'Hold the current round',
      subject: 'rotation', action: 'rejected', evidence: ['Manager holds current plan']
    }).replace(/'/g, "&#39;")}'>Reject</button>
  </div>
  <div class="stub" id="act-note"></div>`;
}

export async function panelLabour() {
  const d = await (await fetch('/gis/labour?estate=EC')).json();
  const months = Object.entries(d.deficit_by_month);
  return `<table class="tbl">
    <tr><th>Month</th><th class="num">Harvester deficit</th></tr>
    ${months.map(([m, v]) => `<tr class="${v > 100 ? 'hi' : ''}">
      <td>${m}</td><td class="num">${v}</td></tr>`).join('')}
  </table>
  <div class="sheet-note">
    Worst month <b>${d.worst_month}</b> at <b>${d.worst_deficit}</b> harvesters short
    across ${d.gangs} gangs.
  </div>`;
}

