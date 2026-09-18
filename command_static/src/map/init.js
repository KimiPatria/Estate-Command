import maplibregl from 'maplibre-gl';
import { map, setMap } from './instance.js';
import { scheduleEstatePins } from './pins.js';
import { dismissIntro, introStep } from '../shell/intro.js';
import { SCALE_STEPS } from '../state/constants.js';

export const MAP_CONFIG = {
  container: 'map',
  style: {
    version: 8,
    sources: {
      sat: {
        type: 'raster', tileSize: 256, maxzoom: 19,
        tiles: ['https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}'],
        attribution: 'Imagery © Esri, Maxar, Earthstar Geographics',
      },
    },
    layers: [{ id: 'sat', type: 'raster', source: 'sat' }],
  },
  center: [140.84, -7.04],
  zoom: 11,
  attributionControl: { compact: true },
};

/* maplibre-gl is a module import now rather than a deferred CDN script, so
   the bundler decides when it arrives. The curtain still paints first because
   main.js waits for DOMContentLoaded before calling this.

   `boot` is passed in rather than imported: this module importing the entry
   made a cycle that Vite's HMR resolved by loading main.js twice, and the
   second boot threw on the first one's sources. Harmless in the build, an
   error on every dev reload. */
export function initMap(boot) {
  setMap(new maplibregl.Map(MAP_CONFIG));
  // Shift belongs to selection here, not to box zoom. MapLibre binds
  // shift+mousedown to a zoom rectangle by default, which swallows both the
  // shift-click that adds one block and the shift-drag that adds several.
  // Box zoom is a gesture almost nobody reaches for; building a working set
  // is the point of the page.
  map.boxZoom.disable();
  map.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'top-right');
  map.addControl(new maplibregl.ScaleControl({ maxWidth: 110, unit: 'metric' }), 'bottom-right');

  map.on('load', () => {
    introStep(32, 'Imagery ready');
    // A failed boot must still lift the curtain, or the failure is invisible.
    boot().catch(err => { console.error('boot failed', err); dismissIntro(); });
  });
  map.on('zoom', paintScaleHud);
  // Coalesced into one frame: regrouping three points is cheap, but doing it
  // twice per rendered frame is still waste.
  map.on('zoom', scheduleEstatePins);
  map.on('move', scheduleEstatePins);
  introStep(14, 'Loading imagery');
}


export function paintScaleHud() {
  const z = map.getZoom();
  const active = SCALE_STEPS.find(s => z < s.maxZoom) || SCALE_STEPS[SCALE_STEPS.length - 1];
  document.querySelectorAll('.scale-hud .step').forEach(el => {
    el.classList.toggle('on', el.dataset.step === active.step);
  });
  // Blocks are the finest unit and only earn their outlines close in.
  if (map.getLayer('blocks-line')) {
    map.setPaintProperty('blocks-line', 'line-opacity', z >= 12.5 ? 0.9 : 0.25);
  }
  if (map.getLayer('divisions-line')) {
    map.setLayoutProperty('divisions-line', 'visibility', z < 14.2 ? 'visible' : 'none');
  }
}

