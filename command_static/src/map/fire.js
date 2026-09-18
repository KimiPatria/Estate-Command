import { map } from './instance.js';
import { esc, fmt, row } from '../lib/fmt.js';
import { raiseAlerts } from '../shell/alerts.js';
import { openDrawer, wireDrawer } from '../shell/drawer.js';
import { refreshRail } from '../shell/rail.js';
import { S } from '../state/store.js';

/* ── fire and force majeure (UC-06) ─────────────────────────────────── */
/* Real detection, real exposure, invented response. The layer styling keeps
   those apart: hotspots and threatened blocks read as data, the response
   assets read as the placeholders they are. */

/* Short button copy, which is a UI decision. The set of scenarios and what
   each one means come from /gis/fire/scenarios, so adding one server-side
   shows up here without an edit; a key with no short label falls back to its
   own name rather than disappearing. */
export const SCENARIO_LABELS = { live: 'Live', near_miss: 'Near miss', severe: 'Severe' };

let SCENARIOS = null;   // key -> description, from the server

async function scenarios() {
  if (SCENARIOS) return SCENARIOS;
  try {
    const d = await (await fetch('/gis/fire/scenarios')).json();
    SCENARIOS = d.scenarios || d;
  } catch (e) {
    // The switch must still work when the describe call fails.
    SCENARIOS = Object.fromEntries(Object.keys(SCENARIO_LABELS).map(k => [k, '']));
  }
  return SCENARIOS;
}

function shortLabel(key) {
  return SCENARIO_LABELS[key]
    || key.replace(/_/g, ' ').replace(/^./, c => c.toUpperCase());
}
export const BAND_COLOR = { critical: '#e0644f', high: '#d4a84b', watch: '#8fa5a0' };
export const EMPTY_FC = { type: 'FeatureCollection', features: [] };

export function addFireLayers() {
  ['cones', 'threat', 'route', 'fire-assets', 'hotspots'].forEach(id => {
    map.addSource(id, { type: 'geojson', data: EMPTY_FC });
  });

  // Spread envelope. Deliberately soft: it is a screening estimate, and a
  // hard-edged polygon would imply a precision the model does not have.
  map.addLayer({
    id: 'cones-fill', type: 'fill', source: 'cones',
    paint: { 'fill-color': '#e0644f', 'fill-opacity': 0.13 },
  });
  map.addLayer({
    id: 'cones-line', type: 'line', source: 'cones',
    paint: { 'line-color': 'rgba(224,100,79,.55)', 'line-width': 1, 'line-dasharray': [2, 2] },
  });

  map.addLayer({
    id: 'threat-fill', type: 'fill', source: 'threat',
    paint: {
      'fill-color': ['match', ['get', 'band'],
        'critical', BAND_COLOR.critical, 'high', BAND_COLOR.high, BAND_COLOR.watch],
      'fill-opacity': ['match', ['get', 'band'], 'critical', 0.62, 'high', 0.46, 0.3],
    },
  });
  map.addLayer({
    id: 'threat-line', type: 'line', source: 'threat',
    paint: {
      'line-color': ['match', ['get', 'band'],
        'critical', '#ff8e78', 'high', '#e8c179', 'rgba(255,255,255,.5)'],
      'line-width': ['match', ['get', 'band'], 'critical', 1.8, 1],
    },
  });

  map.addLayer({
    id: 'route-line', type: 'line', source: 'route',
    paint: {
      'line-color': '#3fb0c4', 'line-width': 2.4, 'line-dasharray': [1.5, 1.2],
      'line-opacity': 0.9,
    },
  });

  // Synthetic assets. Dashed rings and a muted gold say "invented" before
  // anyone reads the label.
  map.addLayer({
    id: 'assets-water-line', type: 'line', source: 'fire-assets',
    filter: ['==', ['geometry-type'], 'LineString'],
    paint: { 'line-color': '#4f9dbf', 'line-width': 2, 'line-opacity': 0.75 },
  });
  map.addLayer({
    id: 'assets-point', type: 'circle', source: 'fire-assets',
    filter: ['==', ['geometry-type'], 'Point'],
    paint: {
      'circle-radius': ['match', ['get', 'kind'], 'pos_induk', 8, 'pos_divisi', 6, 5],
      'circle-color': ['match', ['get', 'entity'],
        'fire_post', '#d4a84b', 'fire_tower', '#8b8c7a', 'water_source', '#4f9dbf', '#8fa5a0'],
      'circle-stroke-color': '#0e1614',
      'circle-stroke-width': 1.5,
      'circle-opacity': 0.95,
    },
  });

  // Hotspots last, so nothing draws over a live detection.
  map.addLayer({
    id: 'hotspots-halo', type: 'circle', source: 'hotspots',
    paint: {
      'circle-radius': ['interpolate', ['linear'], ['get', 'frp_total'], 0, 8, 300, 26],
      'circle-color': '#e0644f', 'circle-opacity': 0.16,
    },
  });
  map.addLayer({
    id: 'hotspots-point', type: 'circle', source: 'hotspots',
    paint: {
      'circle-radius': ['interpolate', ['linear'], ['get', 'frp_total'], 0, 4, 300, 11],
      'circle-color': ['case', ['==', ['get', 'upwind_of_estate'], true], '#ff4f2f', '#e8a06e'],
      'circle-stroke-color': '#ffffff',
      'circle-stroke-width': ['case', ['==', ['get', 'upwind_of_estate'], true], 1.6, 0.6],
      'circle-opacity': 0.95,
    },
  });

  map.on('click', 'hotspots-point', e => showHotspot(e.features[0].properties));
  map.on('click', 'assets-point', e => showAsset(e.features[0].properties));
  ['hotspots-point', 'assets-point'].forEach(id => {
    map.on('mouseenter', id, () => map.getCanvas().style.cursor = 'pointer');
    map.on('mouseleave', id, () => map.getCanvas().style.cursor = '');
  });
}

/* The fire controls live in the fire panel now rather than in a rail group of
   their own. The panel may well be shut, and the fire feed keeps polling
   either way, so every one of these renderers no-ops on a missing host. */
export async function renderScenarios() {
  const host = document.getElementById('scenarios');
  if (!host) return;
  const all = await scenarios();
  // The panel can be shut and reopened while this awaits.
  const live = document.getElementById('scenarios');
  if (!live) return;
  live.innerHTML = Object.keys(all)
    .map(k => `<button data-scen="${k}" class="${k === S.scenario ? 'on' : ''}"
                       title="${esc(all[k] || '')}">${esc(shortLabel(k))}</button>`)
    .join('');
  live.querySelectorAll('[data-scen]').forEach(b => {
    b.onclick = () => { S.scenario = b.dataset.scen; renderScenarios(); loadFire(); };
  });
  refreshRail();
}

export async function loadFire() {
  if (!S.blocks) { clearFire(); return; }
  // The fire panel may well be shut: the feed loads regardless, because the
  // map layers and the alert strip both depend on it.
  const loading = document.getElementById('fire-stat');
  if (loading) loading.textContent = 'Assessing…';
  const res = await fetch(`/gis/fire?estate=${encodeURIComponent(S.estate)}&scenario=${S.scenario}`);
  if (!res.ok) { clearFire(); return; }
  S.fire = await res.json();

  const L = S.fire.layers;
  map.getSource('hotspots').setData(L.hotspots);
  map.getSource('cones').setData(L.cones);
  map.getSource('route').setData(L.route);
  map.getSource('fire-assets').setData(L.assets);

  // Threatened blocks are drawn as their own copy of the real polygons,
  // carrying the band, so the choropleth underneath is left untouched.
  const byId = new Map(S.fire.threatened_blocks.map(b => [b.block_id, b]));
  map.getSource('threat').setData({
    type: 'FeatureCollection',
    features: S.blocks.features.filter(f => byId.has(f.id)).map(f => ({
      type: 'Feature',
      geometry: f.geometry,
      properties: { ...byId.get(f.id) },
    })),
  });

  renderFireStat();
  renderFireAlert();
  refreshRail();
}

export function clearFire() {
  S.fire = null;
  ['hotspots', 'cones', 'route', 'threat', 'fire-assets'].forEach(id => {
    if (map.getSource(id)) map.getSource(id).setData(EMPTY_FC);
  });
  const stat = document.getElementById('fire-stat');
  if (stat) stat.textContent = 'Needs block geometry.';
  const bands = document.getElementById('fire-bands');
  if (bands) bands.innerHTML = '';
  refreshRail();
}

export function renderFireStat() {
  const statEl = document.getElementById('fire-stat');
  if (!statEl || !S.fire) return;
  const f = S.fire, h = f.hotspots, e = f.exposure, m = f.model;
  let feed;
  if (!h.available) {
    feed = `<span class="down">feed unavailable</span> — ${h.reason || 'unknown'}`;
  } else if (h.provenance === 'real:firms') {
    feed = `<span class="live">live NASA FIRMS</span> — ${fmt(h.detections)} detections, ${h.clusters_total} fires`;
  } else {
    feed = `<span class="synth">synthetic scenario</span> — ${h.clusters_total} fires`;
  }

  statEl.innerHTML = `
    ${feed}<br>
    Wind <b>${f.wind.speed_kmh ?? '—'}</b> km/h from <b>${f.wind.from_deg}°</b>,
    spread <b>${m.rate_of_spread_kmh}</b> km/h, reach <b>${m.reach_km}</b> km in ${m.horizon_hours} h.<br>
    <b>${h.threatening}</b> of ${h.clusters_total} fires threaten this estate.<br>
    Exposure <b>${fmt(e.planted_ha, 1)}</b> ha, <b>${fmt(e.palms)}</b> palms.`;

  const bands = ['critical', 'high', 'watch'];
  const bandEl = document.getElementById('fire-bands');
  if (!bandEl) return;
  bandEl.innerHTML = e.blocks
    ? bands.map(b => `<div class="band-chip ${b}">${e.by_band[b] || 0}<small>${b}</small></div>`).join('')
    : '';
}

export function renderFireAlert() {
  const f = S.fire, el = document.getElementById('alerts');
  if (!f || !f.exposure.blocks) { raiseAlerts(); return; }
  const e = f.exposure, mob = f.mobilisation, front = f.threatened_blocks[0];
  const synth = f.hotspots.provenance === 'synthetic';
  el.innerHTML = `<div class="alert">
    <span class="sev">${synth ? 'Scenario' : 'Live'}</span>
    <div class="body">
      <b>${e.blocks} blocks downwind of an active fire.</b>
      <span class="meta">${fmt(e.planted_ha, 1)} ha and ${fmt(e.palms)} palms exposed.
      Block ${front.block_label} first, in about ${front.eta_hours} h.
      ${mob.post ? `${mob.post.name} is ${mob.post.travel_minutes} min away with ${mob.post.crew_on_shift} on shift.` : ''}</span>
    </div>
    <button class="open" id="alert-open">Open</button>
    <button class="x" aria-label="Dismiss">&times;</button>
  </div>`;
  el.querySelector('.x').onclick = () => el.innerHTML = '';
  el.querySelector('#alert-open').onclick = () => showFireDecision();
}

export function showHotspot(p) {
  const d = document.getElementById('drawer');
  const synth = p.provenance === 'synthetic';
  d.innerHTML = `
    <div class="dr-head"><div class="top">
      <div><h3>Fire cluster</h3>
        <div class="sub">${p.pixels} VIIRS pixels · ${p.distance_km} km from estate centre</div></div>
      <button class="x" id="drawer-x" aria-label="Close">&times;</button>
    </div></div>
    <div class="dr-sec">
      <div class="dr-sec-t">Detection</div>
      <dl class="kv">
        ${row('Total radiative power', fmt(p.frp_total, 2), 'MW')}
        ${row('Strongest pixel', fmt(p.frp_max, 2), 'MW')}
        ${row('High-confidence pixels', p.confidence_high)}
        ${row('Latest pass', p.latest_detection)}
        ${row('Bearing from estate', fmt(p.bearing_from_estate, 1), '°')}
        ${row('Blocks in its path', p.threatens_blocks)}
      </dl>
      <div class="src">${synth
        ? 'This cluster is <b>synthetic</b>, generated for the selected scenario.'
        : 'Detection is <b>real</b>, from NASA FIRMS VIIRS SNPP near-real-time. Low-confidence pixels were dropped before clustering.'}</div>
    </div>
    ${S.fire && S.fire.exposure.blocks ? fireCard() : ''}`;
  openDrawer(d);
  wireDrawer(d);
}

export function showAsset(p) {
  const d = document.getElementById('drawer');
  d.innerHTML = `
    <div class="dr-head"><div class="top">
      <div><h3>${p.name}</h3><div class="sub">${p.label}</div></div>
      <button class="x" id="drawer-x" aria-label="Close">&times;</button>
    </div></div>
    <div class="dr-sec">
      <div class="dr-sec-t">Asset</div>
      <dl class="kv">
        ${row('Type', p.kind)}
        ${p.crew !== undefined ? row('Crew', p.crew) : ''}
        ${p.crew_on_shift !== undefined ? row('On shift now', p.crew_on_shift) : ''}
        ${p.tanker_litres ? row('Tanker', fmt(p.tanker_litres), 'L') : ''}
        ${p.pumps !== undefined ? row('Pumps', p.pumps) : ''}
        ${p.usable_m3 ? row('Usable water', fmt(p.usable_m3), 'm³') : ''}
        ${p.draw_rate_lpm ? row('Draw rate', fmt(p.draw_rate_lpm), 'L/min') : ''}
        ${p.height_m ? row('Tower height', p.height_m, 'm') : ''}
        ${p.spotters_on_shift !== undefined ? row('Spotters on shift', p.spotters_on_shift) : ''}
      </dl>
      <div class="src">This asset is <b>synthetic</b>. EPMS has no table that can hold a
        fire post, water source or shift roster. It would come from:
        ${p.would_come_from}.</div>
    </div>`;
  openDrawer(d);
  wireDrawer(d);
}

export function showFireDecision() {
  const d = document.getElementById('drawer');
  d.innerHTML = `
    <div class="dr-head"><div class="top">
      <div><h3>Fire threat</h3>
        <div class="sub">Estate ${S.fire.estate} · ${shortLabel(S.scenario)} · ${S.fire.hotspots.threatening} active front(s)</div></div>
      <button class="x" id="drawer-x" aria-label="Close">&times;</button>
    </div></div>
    ${fireExposureSection()}
    ${fireCard()}`;
  openDrawer(d);
  wireDrawer(d);
}

export function fireExposureSection() {
  const f = S.fire, e = f.exposure, m = f.model;
  return `<div class="dr-sec">
    <div class="dr-sec-t">Exposure</div>
    <dl class="kv">
      ${row('Blocks in the path', e.blocks)}
      ${row('Planted area', fmt(e.planted_ha, 1), 'ha')}
      ${row('Palms', fmt(e.palms))}
      ${row('Critical, under 2 h', e.by_band.critical || 0)}
      ${row('High, under 6 h', e.by_band.high || 0)}
      ${row('Watch, under 12 h', e.by_band.watch || 0)}
      ${row('Value at risk', null)}
    </dl>
    <div class="src">Hectares and palms are <b>real</b>, from the client's ArcGIS attributes.
      ${e.valuation_note}</div>
    <div class="src">${m.caveat}</div>
  </div>`;
}

/* The fire decision card. Same fixed structure as every other card:
   what was detected, the evidence, the action, then accept / reject / defer. */
export function fireCard() {
  const f = S.fire;
  if (!f || !f.exposure.blocks) {
    return `<div class="dr-sec"><div class="dr-sec-t">Decision</div>
      <div class="empty">No block is downwind of an active detection.
        Nothing to mobilise.</div></div>`;
  }
  const e = f.exposure, mob = f.mobilisation, front = f.threatened_blocks[0];
  const wind = f.wind, m = f.model;
  const tight = mob.margin_hours !== null && mob.margin_hours < 1;
  const shortHanded = mob.post && mob.crew_reachable_in_window < e.blocks * 0.25;

  return `<div class="dr-sec">
    <div class="dr-sec-t">Decision</div>
    <div class="card">
      <div class="card-h">
        <span class="sev ${tight ? '' : 'warn'}" style="${tight
          ? 'background:var(--error-dim);color:var(--error)' : ''}">Mobilise</span>
        <h4>${e.blocks} blocks downwind, ${front.block_label} first in ~${front.eta_hours} h</h4>
      </div>
      <div class="card-b">
        <ul class="ev">
          <li><b>${f.hotspots.threatening}</b> of ${f.hotspots.clusters_total} detected fires sit upwind,
              the nearest <b>${f.hotspots.clusters.find(c => c.upwind_of_estate)?.distance_km ?? '—'}</b> km out
              at <b>${fmt(f.hotspots.clusters.find(c => c.upwind_of_estate)?.frp_total, 1) ?? '—'}</b> MW.</li>
          <li>Wind <b>${wind.speed_kmh ?? '—'}</b> km/h from <b>${wind.from_deg}°</b>,
              carrying fire toward <b>${wind.travel_deg}°</b>.</li>
          <li>Dryness index <b>${m.dryness.value}</b> (${m.dryness.basis}),
              giving an estimated spread of <b>${m.rate_of_spread_kmh}</b> km/h.</li>
          <li>Exposure <b>${fmt(e.planted_ha, 1)}</b> ha and <b>${fmt(e.palms)}</b> palms,
              of which <b>${(e.by_band.critical || 0) + (e.by_band.high || 0)}</b> blocks are inside six hours.</li>
          ${mob.post ? `<li>Nearest crew: <b>${mob.post.name}</b>, ${mob.post.crew_on_shift} on shift,
              <b>${mob.post.road_km}</b> km by road, arriving in <b>${mob.post.travel_minutes}</b> min.
              Margin against the fire <b>${mob.margin_hours}</b> h.</li>` : ''}
          ${mob.water ? `<li>Nearest water: <b>${mob.water.name}</b>, ${mob.water.supply},
              ${mob.water.distance_km} km from the front block.</li>` : ''}
        </ul>
        <div class="src">Detections, wind and block attributes are <b>real</b>.
          Fire posts, water sources, crew numbers and vehicle speeds are
          <b>synthetic</b> — EPMS records none of them. ${mob.note || ''}</div>
      </div>
      <div class="card-act">
        <p>Dispatch ${mob.post ? mob.post.name : 'the nearest post'} to
          ${front.block_label} and wet the downwind boundary.
          ${shortHanded ? 'Crew reachable inside the window covers only a fraction of the exposed area — escalate to the regional office.' : ''}
          ${tight ? 'Margin is under an hour. Treat the arrival estimate as an ordering, not a countdown.' : ''}</p>
        <div class="acts">
          <button class="accept" data-decide='${JSON.stringify({
            use_case: 'UC-06 fire and force majeure',
            title: `Mobilise to ${front.block_label}`,
            subject: `${e.blocks} blocks, ${e.planted_ha} ha exposed`,
            action: 'accepted', artifact_kind: 'fire_mobilisation', fire: true,
            evidence: [
              `${f.hotspots.threatening} of ${f.hotspots.clusters_total} fires upwind`,
              `Wind ${wind.speed_kmh} km/h from ${wind.from_deg} degrees`,
              `${e.blocks} blocks, ${e.planted_ha} ha, ${e.palms} palms exposed`,
              `${front.block_label} first in about ${front.eta_hours} h`]
          }).replace(/'/g, "&#39;")}'>Dispatch</button>
          <button data-decide='${JSON.stringify({
            use_case: 'UC-06 fire and force majeure', title: 'No mobilisation',
            subject: `${e.blocks} blocks exposed`, action: 'rejected',
            evidence: ['Manager judged the threat not actionable']
          }).replace(/'/g, "&#39;")}'>Reject</button>
          <button data-decide='${JSON.stringify({
            use_case: 'UC-06 fire and force majeure', title: 'Hold and watch',
            subject: `${e.blocks} blocks exposed`, action: 'deferred',
            evidence: [`${front.eta_hours} h until first arrival`]
          }).replace(/'/g, "&#39;")}'>Defer</button>
        </div>
        <div class="stub" id="act-note">Accepting drafts a mobilisation order. Nothing is written to EPMS.</div>
      </div>
    </div>
  </div>`;
}

