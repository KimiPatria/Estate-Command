import { map } from './instance.js';
import { loadBlockRows } from '../ai/briefs.js';
import { row } from '../lib/fmt.js';
import { setMetric } from './choropleth.js';
import { clearFire, loadFire } from './fire.js';
import { closeEstateCard, invalidatePins, renderEstatePins } from './pins.js';
import { hideMapMessage, showMapMessage } from '../shell/alerts.js';
import { DRAWER_W, closeDrawer, drawerIsOpen } from '../shell/drawer.js';
import { renderEstates } from '../shell/context-bar.js';
import { resetSelectionForEstate } from '../state/selection.js';
import { invalidateBlockIndex } from './blocks.js';
import { renderMetrics } from './mapctl.js';
import { renderMonths } from '../shell/timeline.js';
import { ROLES } from '../state/constants.js';
import { S } from '../state/store.js';

/* ── estate selection ───────────────────────────────────────────────── */
export async function selectEstate(code) {
  S.estate = code;
  S.selected = null;
  closeDrawer();
  closeEstateCard();
  renderEstates();
  // Block ids are estate-scoped, so both the index and any set built
  // against the old estate are meaningless now.
  invalidateBlockIndex();
  resetSelectionForEstate();
  // The pins carry the selected-estate highlight, so force a rebuild rather
  // than waiting for the grouping to happen to change.
  invalidatePins();
  renderEstatePins();

  const row = S.estates.find(e => e.estate_code === code);
  const res = await fetch(`/gis/blocks?estate=${encodeURIComponent(code)}`);

  if (!res.ok) {
    // Honest N/A, never a fabricated tessellation.
    const err = await res.json();
    S.blocks = null;
    S.months = [];
    S.month = null;
    map.getSource('blocks').setData({ type: 'FeatureCollection', features: [] });
    map.getSource('divisions').setData({ type: 'FeatureCollection', features: [] });
    showMapMessage(
      `Estate ${code} has no block geometry`,
      err.degrades_to || 'Estate outline only.',
      row && row.caveat
    );
    renderMetrics();
    renderMonths();
    setProvenance(row);
    clearFire();
    fitTo(code);
    return;
  }

  hideMapMessage();
  S.blocks = await res.json();
  // Mirror the feature id into properties so paint expressions and the
  // selection filter can both reach it.
  S.blocks.features.forEach(f => { f.properties._id = f.id; });
  map.getSource('blocks').setData(S.blocks);
  fetch(`/gis/divisions?estate=${encodeURIComponent(code)}`)
    .then(r => r.ok ? r.json() : { type: 'FeatureCollection', features: [] })
    .then(d => map.getSource('divisions').setData(d));

  S.months = (row && row.harvest_window && row.harvest_window.months) || [];
  S.forwardMonths = (S.catalogue && code === 'EC') ? S.forwardMonths : [];
  S.month = null;
  renderMetrics();
  renderMonths();
  setProvenance(row);
  await setMetric(ROLES[S.role].metric);
  loadBlockRows(true);   // not awaited: the drawer fills in when it lands
  fitTo(code);
  loadFire();   // not awaited: the fire feeds are external and can be slow
}

export function fitTo(code) {
  const feats = S.blocks
    ? S.blocks.features
    : (S.estatesGeo ? S.estatesGeo.features.filter(f => f.properties.estate_code === code) : []);
  if (!feats.length) return;
  let [w, s, e, n] = [180, 90, -180, -90];
  feats.forEach(f => f.geometry.coordinates[0].forEach(([x, y]) => {
    if (x < w) w = x; if (x > e) e = x;
    if (y < s) s = y; if (y > n) n = y;
  }));
  // Fit into the visible half when the drawer is out, not under it.
  map.fitBounds([[w, s], [e, n]], {
    padding: { top: 56, bottom: 56, left: 56, right: 56 + (drawerIsOpen() ? DRAWER_W : 0) },
    duration: 800,
  });
}
document.getElementById('fit-btn').onclick = () => fitTo(S.estate);

export function setProvenance(row) {
  const el = document.getElementById('provenance-text');
  if (!row) { el.textContent = 'Unknown source'; return; }
  el.textContent = row.has_block_geometry
    ? `Real data · ${row.estate_code} ArcGIS + EPMS harvest`
    : `Real data · ${row.estate_code} outline from harvester GPS`;
}

