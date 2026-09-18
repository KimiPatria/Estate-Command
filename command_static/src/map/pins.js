import maplibregl from 'maplibre-gl';
import { map } from './instance.js';
import { fmt, row } from '../lib/fmt.js';
import { selectEstate } from './estate.js';
import { CLUSTER_PX, PIN_MAX_ZOOM } from '../state/constants.js';
import { S } from '../state/store.js';

/* ── estate pins ────────────────────────────────────────────────────── */
/* Rendered as DOM markers rather than a clustered GeoJSON source on purpose:
   the style carries no glyphs endpoint, so a symbol layer could not draw the
   estate code or the count, and a hover dropdown is not something a canvas
   layer can hold at all. */
export let pinMarkers = [];
let pinSig = '';

/* Drop the memoised grouping signature so the next render rebuilds the pins
   even though the grouping inputs did not change. Selecting an estate needs
   this, because the highlight lives on the pin rather than in the data. */
export function invalidatePins() { pinSig = ''; }
export let pinFrame = 0;
export let pinPopup = null;

export function estateBboxCentre(f) {
  let [w, s, e, n] = [180, 90, -180, -90];
  f.geometry.coordinates[0].forEach(([x, y]) => {
    if (x < w) w = x; if (x > e) e = x;
    if (y < s) s = y; if (y > n) n = y;
  });
  return [(w + e) / 2, (s + n) / 2];
}

export function buildEstatePins() {
  const feats = (S.estatesGeo && S.estatesGeo.features) || [];
  if (!feats.length) { S.estatePins = null; return; }
  const byCode = new Map((S.estates || []).map(r => [r.estate_code, r]));
  S.estatePins = feats.map(f => {
    const code = f.properties.estate_code;
    return { estate_code: code, lngLat: estateBboxCentre(f), row: byCode.get(code) || {} };
  });
}

/* Greedy screen-space grouping around a leader, largest estate first, so the
   dot that survives a merge is the one worth flying to. Chaining is
   deliberately not transitive: a chain would let one dot swallow a line of
   estates that are nowhere near each other. */
export function clusterPins(pins) {
  const pts = pins.map(p => ({ pin: p, xy: map.project(p.lngLat) }));
  pts.sort((a, b) => (b.pin.row.planted_ha || 0) - (a.pin.row.planted_ha || 0));
  const groups = [];
  const taken = new Array(pts.length).fill(false);
  for (let i = 0; i < pts.length; i++) {
    if (taken[i]) continue;
    taken[i] = true;
    const members = [pts[i].pin];
    for (let j = i + 1; j < pts.length; j++) {
      if (taken[j]) continue;
      const dx = pts[i].xy.x - pts[j].xy.x;
      const dy = pts[i].xy.y - pts[j].xy.y;
      if (dx * dx + dy * dy <= CLUSTER_PX * CLUSTER_PX) {
        taken[j] = true;
        members.push(pts[j].pin);
      }
    }
    groups.push(members);
  }
  return groups;
}

export function clearEstatePins() {
  pinMarkers.forEach(m => m.remove());
  pinMarkers = [];
}

export function closeEstateCard() {
  if (pinPopup) { pinPopup.remove(); pinPopup = null; }
}

export function scheduleEstatePins() {
  if (pinFrame) return;
  pinFrame = requestAnimationFrame(() => { pinFrame = 0; renderEstatePins(); });
}

export function renderEstatePins() {
  if (!map || !S.estatePins) return;
  if (map.getZoom() >= PIN_MAX_ZOOM) {
    // Flown in past the pins: they would only sit on top of the blocks.
    if (pinSig !== '#off') { clearEstatePins(); closeEstateCard(); pinSig = '#off'; }
    return;
  }
  const groups = clusterPins(S.estatePins);
  const sig = groups
    .map(g => g.map(m => m.estate_code).sort().join('+'))
    .sort().join('|');
  // Nothing regrouped, so the markers already on the map are still correct and
  // maplibre keeps them pinned to their coordinates on its own.
  if (sig === pinSig) return;
  pinSig = sig;
  clearEstatePins();
  groups.forEach(g => pinMarkers.push(g.length > 1 ? clusterMarker(g) : estateMarker(g[0])));
}

export function estateMarker(pin) {
  const r = pin.row;
  const real = !!r.has_block_geometry;
  const el = document.createElement('div');
  el.className = 'pin' + (real ? '' : ' hull') + (pin.estate_code === S.estate ? ' on' : '');
  el.innerHTML = `
    <button class="pin-dot" title="Estate ${pin.estate_code}"
            aria-label="Estate ${pin.estate_code}"><span>${pin.estate_code}</span></button>
    <div class="pin-label">${fmt(r.planted_ha, 0) || '—'} ha</div>`;
  el.querySelector('.pin-dot').onclick = ev => {
    ev.stopPropagation();
    openEstateCard(pin);
  };
  return new maplibregl.Marker({ element: el, anchor: 'center' })
    .setLngLat(pin.lngLat).addTo(map);
}

export function clusterMarker(members) {
  const ha = members.reduce((a, m) => a + (m.row.planted_ha || 0), 0);
  const lng = members.reduce((a, m) => a + m.lngLat[0], 0) / members.length;
  const lat = members.reduce((a, m) => a + m.lngLat[1], 0) / members.length;
  const el = document.createElement('div');
  el.className = 'pin cluster';
  el.innerHTML = `
    <div class="pin-drop"><div class="pin-drop-in">
      <div class="pin-drop-h"><b>${members.length}</b> estates &middot; <b>${fmt(ha, 0)}</b> ha</div>
      ${members.map(m => `
        <button class="pin-drop-row" data-pin-estate="${m.estate_code}">
          <span class="code">${m.estate_code}</span>
          <span class="info">${fmt(m.row.planted_ha, 0) || '—'} ha &middot; ${
            m.row.has_block_geometry ? `${fmt(m.row.blocks)} blocks` : 'outline only'}</span>
          <span class="pill ${m.row.has_block_geometry ? 'real' : 'hull'}">${
            m.row.has_block_geometry ? 'ArcGIS' : 'hull'}</span>
        </button>`).join('')}
      <div class="pin-drop-f">Pick one to open it, or click the dot to zoom in
        until they separate.</div>
    </div></div>
    <button class="pin-dot" title="${members.length} estates here"
            aria-label="${members.length} estates, zoom in to separate"><span>${members.length}</span></button>
    <div class="pin-label">${members.length} estates &middot; ${fmt(ha, 0)} ha</div>`;

  el.querySelector('.pin-dot').onclick = ev => {
    ev.stopPropagation();
    closeEstateCard();
    map.easeTo({ center: [lng, lat], zoom: map.getZoom() + 1.8, duration: 700 });
  };
  el.querySelectorAll('[data-pin-estate]').forEach(b => {
    b.onclick = ev => {
      ev.stopPropagation();
      closeEstateCard();
      selectEstate(b.dataset.pinEstate);
    };
  });
  // Lift the hovered cluster over its neighbours; markers are siblings and
  // each one is its own stacking context, so this cannot be done in CSS.
  el.addEventListener('mouseenter', () => { el.style.zIndex = '30'; });
  el.addEventListener('mouseleave', () => { el.style.zIndex = ''; });

  return new maplibregl.Marker({ element: el, anchor: 'center' })
    .setLngLat([lng, lat]).addTo(map);
}

/* Executive-level only: scale, coverage and how much of it is real. Anything
   finer belongs to the estate you land on, not to a card on the overview. */
export function openEstateCard(pin) {
  closeEstateCard();
  const r = pin.row;
  const real = !!r.has_block_geometry;
  const hw = r.harvest_window;
  const html = `
    <div class="est-card">
      <div class="est-card-h">
        <div>
          <h4>Estate ${pin.estate_code}</h4>
          <div class="sub">${real ? 'Surveyed blocks' : 'GPS hull, no blocks'}</div>
        </div>
        <span class="pill ${real ? 'real' : 'hull'}">${real ? 'ArcGIS' : 'GPS hull'}</span>
        <button class="x" data-pin-close aria-label="Close">&times;</button>
      </div>
      <dl class="kv">
        ${row('Planted area', fmt(r.planted_ha, 1), 'ha')}
        ${row('Blocks', real ? fmt(r.blocks) : null)}
        ${row('Carrying harvest', real ? fmt(r.blocks_with_harvest) : null)}
        ${row('Divisions', r.divisions === undefined ? null : r.divisions)}
        ${row('Harvest window', hw ? `${hw.from} → ${hw.to}` : null)}
      </dl>
      <button class="est-card-go" data-pin-estate="${pin.estate_code}">Zoom to estate</button>
      <div class="src">${real
        ? 'Area and counts: <b>real</b>, from the ArcGIS overlay joined to EPMS harvest.'
        : `Outline is a <b>GPS hull</b>, not a survey. ${r.caveat || 'No block geometry, so there is nothing to drill into.'}`}</div>
    </div>`;

  pinPopup = new maplibregl.Popup({
    className: 'est-pop', closeButton: false, closeOnClick: true,
    maxWidth: 'none', offset: 26,
  }).setLngLat(pin.lngLat).setHTML(html).addTo(map);

  const el = pinPopup.getElement();
  el.querySelector('[data-pin-close]').onclick = closeEstateCard;
  el.querySelector('[data-pin-estate]').onclick = () => {
    const code = pin.estate_code;
    closeEstateCard();
    selectEstate(code);
  };
}

