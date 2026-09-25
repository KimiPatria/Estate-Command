import { easeMapPadding } from '../map/camera.js';
import { map } from '../map/instance.js';
import { blockBriefSection, canopySection, loadBlockBrief } from '../ai/briefs.js';
import { fmt, pct, row } from '../lib/fmt.js';
import { recordDecision } from '../panels/decisions.js';
import { S } from '../state/store.js';

export const DRAWER_W = 372;

/* The panel floats over the map rather than sitting beside it, so nothing is
   resized when it appears. The camera takes matching padding instead, which
   slides the imagery out from under the panel in one movement and leaves the
   selected block in the clear half of the canvas. */
export function openDrawer(d) {
  if (d.classList.contains('open')) return;   // content swap, panel stays put
  d.scrollTop = 0;
  // Settle the offscreen start position before the class lands, so the
  // transition never gets skipped when markup was written in the same frame.
  void d.offsetWidth;
  d.classList.add('open');
  easeMapPadding(DRAWER_W);
}

export function closeDrawer() {
  const d = document.getElementById('drawer');
  if (d.classList.contains('open')) easeMapPadding(0);
  d.classList.remove('open');
  S.selected = null;
  if (map && map.getLayer('blocks-sel')) {
    map.setFilter('blocks-sel', ['==', ['get', '_id'], '__none__']);
  }
}

export function drawerIsOpen() {
  const d = document.getElementById('drawer');
  return !!d && d.classList.contains('open');
}



export function renderDrawer(f) {
  const p = f.properties;
  const d = document.getElementById('drawer');
  const peer = S.metricData && S.metricData.metric === 'peer_index'
    ? S.metricData.values[f.id] : null;
  const windowBunches = S.month
    ? (p.bunches_by_month || {})[S.month]
    : p.bunches_total;
  const perHa = windowBunches && p.planted_ha ? windowBunches / p.planted_ha : null;

  d.innerHTML = `
    <div class="dr-head">
      <div class="top">
        <div>
          <h3>Block ${p.block_label || p.block_code}</h3>
          <div class="sub">Estate ${p.estate_code} · Division ${p.division_code} · SAP ${p.block_sap || '—'}</div>
        </div>
        <button class="x" id="drawer-x" aria-label="Close">&times;</button>
      </div>
    </div>

    <div class="dr-sec">
      <div class="dr-sec-t">The block</div>
      <dl class="kv">
        ${row('Planted', p.planted_year)}
        ${row('Palm age', p.palm_age_years, 'yr')}
        ${row('Variety', p.seed_variety)}
        ${row('Planted area', fmt(p.planted_ha, 2), 'ha')}
        ${row('Surveyed polygon', fmt(p.polygon_area_ha, 2), 'ha')}
        ${row('Palms', fmt(p.palms))}
        ${row('Stand density', p.sph, 'per ha')}
        ${row('Ownership', p.ownership)}
      </dl>
      <div class="src">Source: ArcGIS export</div>
    </div>

    <div class="dr-sec">
      <div class="dr-sec-t">Recorded harvest ${S.month ? `· ${S.month}` : '· full window'}</div>
      <dl class="kv">
        ${row('Bunches', fmt(windowBunches))}
        ${row('Bunches per ha', fmt(perHa, 1))}
        ${row('Harvest days', S.month ? (p.harvest_days_by_month || {})[S.month] : p.harvest_days)}
        ${row('Ripe rate', p.ripe_rate === null ? null : (p.ripe_rate * 100).toFixed(2), '%')}
        ${row('Deduction rate', p.deduction_rate === null ? null : (p.deduction_rate * 100).toFixed(2), '%')}
        ${row('Tonnage', null)}
      </dl>
      <div class="src">Source: EPMS OPH records</div>
    </div>

    ${canopySection(f)}
    ${blockThreatSection(f.id)}
    ${decisionCard(p, peer)}
    ${blockBriefSection(f.id)}
  `;
  openDrawer(d);
  wireDrawer(d);
}

/* Shown only when the selected block is in an active fire's path, so the
   fire threat surfaces where the user is already looking rather than only
   in the alert strip. */
export function blockThreatSection(blockId) {
  if (!S.fire || !S.fire.exposure.blocks) return '';
  const t = S.fire.threatened_blocks.find(b => b.block_id === blockId);
  if (!t) return '';
  return `<div class="dr-sec">
    <div class="dr-sec-t">Fire threat
      <span class="band-chip ${t.band}" style="flex:0 0 auto;padding:2px 7px">${t.band}</span>
    </div>
    <dl class="kv">
      ${row('Estimated arrival', t.eta_hours, 'h')}
      ${row('Distance to fire', t.distance_km, 'km')}
      ${row('Off the wind axis', t.bearing_offset_deg, '°')}
      ${row('Fire radiative power', fmt(t.frp_total, 1), 'MW')}
    </dl>
  </div>`;
}

/* The decision card. Fixed structure everywhere: what was detected, the
   evidence for it, the recommended action, then accept / reject / defer. */
export function decisionCard(p, peer) {
  const idx = peer !== null && peer !== undefined ? peer : null;
  if (idx === null) {
    return `<div class="dr-sec">
      <div class="dr-sec-t">Decision</div>
      <div class="empty">Colour the map by <b>yield vs age-matched peers</b> to evaluate this block
        against its cohort.</div>
    </div>`;
  }

  const behind = idx < 0.85;
  const ahead = idx > 1.15;
  const pct = Math.round(Math.abs(1 - idx) * 100);
  const cohort = S.blocks.features.filter(f => f.properties.planted_year === p.planted_year).length;

  let title, sev, action;
  if (behind) {
    sev = 'warn';
    title = `Yielding ${pct}% below its planting cohort`;
    action = `Send an agronomist to inspect. This says the block is behind its peers, not why.`;
  } else if (ahead) {
    sev = 'ok';
    title = `Yielding ${pct}% above its planting cohort`;
    action = `Worth understanding what is different here before assuming it is noise.`;
  } else {
    sev = 'ok';
    title = 'Performing in line with its cohort';
    action = 'No action. Included so the card is not only shown when something is wrong.';
  }

  return `<div class="dr-sec">
    <div class="dr-sec-t">Decision</div>
    <div class="card">
      <div class="card-h">
        <span class="sev ${sev}">${behind ? 'Investigate' : 'Observation'}</span>
        <h4>${title}</h4>
      </div>
      <div class="card-b">
        <ul class="ev">
          <li>Peer index <b>${idx.toFixed(2)}</b>, where 1.00 is the median of its cohort.</li>
          <li>Compared against <b>${cohort}</b> blocks planted in <b>${p.planted_year}</b> on this estate.</li>
          <li>Recorded <b>${fmt(p.bunches_total)}</b> bunches over <b>${p.harvest_days}</b> harvest days
              across ${S.months.length} months.</li>
          <li>Grading gives no corroboration: deduction rate
              <b>${p.deduction_rate === null || p.deduction_rate === undefined
                    ? 'not recorded' : (p.deduction_rate * 100).toFixed(2) + '%'}</b>,
              against an estate mean of 0.25% and a maximum of 2.1%.</li>
        </ul>
      </div>
      <div class="card-act">
        <p>${action}</p>
        <div class="acts">
          <button class="accept" data-decide='${JSON.stringify({
            use_case: 'UC-03 vegetation and yield anomaly',
            title: `Inspect block ${p.block_label}`,
            subject: `block ${p.block_label}`, action: 'accepted',
            artifact_kind: 'inspection_order',
            inspection: { block_label: p.block_label, division_code: p.division_code,
                          reason: title, peer_index: idx, planted_ha: p.planted_ha },
            evidence: [`Peer index ${idx.toFixed(2)} against ${cohort} cohort blocks`,
                       `${fmt(p.bunches_total)} bunches over ${p.harvest_days} harvest days`]
          }).replace(/'/g, "&#39;")}'>Order inspection</button>
          <button data-decide='${JSON.stringify({
            use_case: 'UC-03 vegetation and yield anomaly',
            title: `No action on ${p.block_label}`, subject: `block ${p.block_label}`,
            action: 'rejected', evidence: [`Peer index ${idx.toFixed(2)} judged within tolerance`]
          }).replace(/'/g, "&#39;")}'>Reject</button>
          <button data-decide='${JSON.stringify({
            use_case: 'UC-03 vegetation and yield anomaly',
            title: `Revisit ${p.block_label} next round`, subject: `block ${p.block_label}`,
            action: 'deferred', evidence: [`Peer index ${idx.toFixed(2)}`]
          }).replace(/'/g, "&#39;")}'>Defer</button>
        </div>
        <div class="stub" id="act-note"></div>
      </div>
    </div>
  </div>`;
}


export function wireDrawer(d) {
  const x = d.querySelector('#drawer-x');
  if (x) x.onclick = closeDrawer;
  d.querySelectorAll('[data-brief]').forEach(b => {
    b.onclick = () => loadBlockBrief(b.dataset.brief);
  });
  d.querySelectorAll('[data-decide]').forEach(b => {
    b.onclick = () => recordDecision(JSON.parse(b.dataset.decide), b);
  });
}

