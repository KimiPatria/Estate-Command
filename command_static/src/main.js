import '../styles/command.css';
import 'maplibre-gl/dist/maplibre-gl.css';
import { renderAsk, submitAsk, toggleAsk } from './ai/ask.js';
import { AI, activeChat, loadAiStatus, loadChats, newChat } from './ai/store.js';
import { selectEstate } from './map/estate.js';
import { addFireLayers, renderScenarios } from './map/fire.js';
import { map } from './map/instance.js';
import { addHighlightLayers, raiseHighlights } from './map/highlight.js';
import { initHover } from './map/hover.js';
import { initMap, paintScaleHud } from './map/init.js';
import { initWorkbench, onWorkbenchChange } from './shell/workbench.js';
import { buildEstatePins, renderEstatePins } from './map/pins.js';
import { selectBlock } from './map/select.js';
import { toggleSelection } from './state/selection.js';
import { initTray } from './shell/tray.js';
import { loadFeatures, markActivePanel, primePanelHints } from './panels/registry.js';
import { raiseAlerts } from './shell/alerts.js';
import { dismissIntro, introStep } from './shell/intro.js';
import { initRail, refreshRail } from './shell/rail.js';
import { initContextBar, renderCoverage, renderEstates, renderReadinessButton, renderRoles } from './shell/context-bar.js';
import { initMapCtl, renderMetrics } from './map/mapctl.js';
import { S } from './state/store.js';

/* ── boot ───────────────────────────────────────────────────────────── */
export async function boot() {
  introStep(42, 'Estate index');
  const [estates, estatesGeo, readiness, catalogue] = await Promise.all([
    fetch('/gis/estates').then(r => r.json()),
    fetch('/gis/estates.geojson').then(r => r.json()),
    fetch('/gis/readiness').then(r => r.json()),
    fetch('/gis/metrics/catalogue').then(r => r.json()),
    // The feature manifest drives the whole rail, so it is part of boot
    // rather than a later fetch. A failure here leaves the panel list empty
    // instead of taking the map down with it.
    loadFeatures().catch(() => null),
  ]);
  S.estates = estates.estates;
  S.estatesGeo = estatesGeo;
  S.readiness = readiness;
  S.catalogue = catalogue.metrics;
  S.forwardMonths = catalogue.months.forward;
  S.satellite = catalogue.satellite || null;
  loadAiStatus();   // not awaited: the launcher explains itself without it
  loadChats();
  introStep(62, 'Readiness scan');

  map.addSource('estates', { type: 'geojson', data: estatesGeo });
  map.addLayer({
    id: 'estates-fill', type: 'fill', source: 'estates',
    paint: {
      'fill-color': ['case', ['==', ['get', 'provenance'], 'real:arcgis'], '#3fb0c4', '#d4a84b'],
      'fill-opacity': ['case', ['==', ['get', 'provenance'], 'real:arcgis'], 0.05, 0.09],
    },
  });
  // Two line layers rather than one with a case expression: line-dasharray is
  // not a data-driven property in MapLibre, and feeding it one throws.
  // Solid outline means surveyed geometry, dashed means hulled from GPS.
  map.addLayer({
    id: 'estates-line', type: 'line', source: 'estates',
    filter: ['==', ['get', 'provenance'], 'real:arcgis'],
    paint: { 'line-color': '#3fb0c4', 'line-width': 2 },
  });
  map.addLayer({
    id: 'estates-line-hull', type: 'line', source: 'estates',
    filter: ['!=', ['get', 'provenance'], 'real:arcgis'],
    paint: { 'line-color': '#d4a84b', 'line-width': 2, 'line-dasharray': [3, 2] },
  });

  map.addSource('blocks', { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
  map.addLayer({
    id: 'blocks-fill', type: 'fill', source: 'blocks',
    paint: { 'fill-color': '#2d8a8a', 'fill-opacity': 0.62 },
  });
  map.addLayer({
    id: 'blocks-line', type: 'line', source: 'blocks',
    paint: { 'line-color': 'rgba(255,255,255,.45)', 'line-width': 0.7 },
  });
  // Selection as its own filtered layer rather than feature-state. GeoJSON
  // feature-state keyed on string ids fails silently in some builds, and a
  // choropleth that never paints is a bad way to find that out.
  map.addLayer({
    id: 'blocks-sel', type: 'line', source: 'blocks',
    filter: ['==', ['get', '_id'], '__none__'],
    paint: { 'line-color': '#ffffff', 'line-width': 2.6 },
  });

  // What the copilot points at. Separate from the selection outline so an
  // answer about eight blocks cannot clobber the one block a manager clicked.
  map.addLayer({
    id: 'blocks-ai', type: 'line', source: 'blocks',
    filter: ['==', ['get', '_id'], '__none__'],
    paint: { 'line-color': '#a695e8', 'line-width': 2.2, 'line-opacity': 0.95 },
  });

  // The panel and flash outlines, added beside the two that already existed.
  addHighlightLayers();

  map.addSource('divisions', { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
  map.addLayer({
    id: 'divisions-line', type: 'line', source: 'divisions',
    paint: { 'line-color': 'rgba(255,255,255,.55)', 'line-width': 1.6, 'line-dasharray': [4, 3] },
  });

  introStep(76, 'Composing layers');
  addFireLayers();
  // Keep every highlight above anything the fire layers draw.
  raiseHighlights();

  map.on('click', 'blocks-fill', e => {
    // Shift builds the working set; a plain click inspects one block. This is
    // the gesture every map product has trained people to expect.
    if (e.originalEvent && (e.originalEvent.shiftKey || e.originalEvent.metaKey)) {
      const f = e.features[0];
      toggleSelection(f.properties._id || f.id);
      return;
    }
    selectBlock(e.features[0]);
  });
  map.on('click', 'estates-fill', e => {
    if (map.getZoom() < 11) selectEstate(e.features[0].properties.estate_code);
  });
  map.on('mouseenter', 'blocks-fill', () => map.getCanvas().style.cursor = 'pointer');
  map.on('mouseleave', 'blocks-fill', () => map.getCanvas().style.cursor = '');
  initHover();

  renderRoles();
  renderEstates();
  renderMetrics();
  renderReadinessButton();
  primePanelHints();
  raiseAlerts();

  buildEstatePins();
  introStep(88, 'Loading blocks');
  const withGeometry = S.estates.find(e => e.has_block_geometry);
  await selectEstate(withGeometry ? withGeometry.estate_code : S.estates[0].estate_code);
  paintScaleHud();
  renderEstatePins();
  renderCoverage();
  dismissIntro();
}


/* ── wiring ─────────────────────────────────────────────────────────── */

export function wireAI() {
  document.getElementById('ask-launch').onclick = () => toggleAsk(true);
  document.getElementById('ask-x').onclick = () => toggleAsk(false);
  document.getElementById('ask-new').onclick = () => {
    // Clicking + on a chat that was never used opens nothing new - it just
    // refocuses it, so mashing the button does not litter the tab bar.
    const cur = activeChat();
    if (!cur || cur.history.length) newChat();
    renderAsk();
    const input = document.getElementById('ask-input'); if (input) input.focus();
  };
  document.getElementById('ask-form').onsubmit = e => {
    e.preventDefault();
    submitAsk(document.getElementById('ask-input').value);
  };

  document.addEventListener('keydown', e => {
    // Slash to ask, the shortcut every map product has trained people to try.
    if (e.key === '/' && !AI.open && !/^(INPUT|TEXTAREA)$/.test(document.activeElement.tagName)) {
      e.preventDefault();
      toggleAsk(true);
    } else if (e.key === 'Escape' && AI.open) {
      toggleAsk(false);
    }
  });
}

/* ── entry ──────────────────────────────────────────────────────────── */
/* The rail is restored before the map is built so a collapsed rail never
   flashes open, and initMap takes the width it will actually have. */
wireAI();
document.addEventListener('DOMContentLoaded', () => {
  initRail();
  initContextBar();
  initMapCtl();
  initTray();
  initWorkbench();
  onWorkbenchChange(markActivePanel);
  initMap(boot);
});

