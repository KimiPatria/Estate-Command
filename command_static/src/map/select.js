import { map } from './instance.js';
import { renderDrawer } from '../shell/drawer.js';
import { S } from '../state/store.js';
import { blockFeature } from './blocks.js';
import { currentRightPad } from './camera.js';

/* ── inspector ──────────────────────────────────────────────────────── */
export function selectBlock(feature) {
  const id = feature.properties._id || feature.id;
  S.selected = id;
  map.setFilter('blocks-sel', ['==', ['get', '_id'], id]);
  selectBlockById(id);
}

export function selectBlockById(id) {
  const f = blockFeature(id);
  if (!f) return;
  S.selected = id;
  if (map && map.getLayer('blocks-sel')) {
    map.setFilter('blocks-sel', ['==', ['get', '_id'], id]);
  }
  renderDrawer(f);
}

/* Frame one block, leaving it clear of whatever dock is open. Used when a row
   in a panel is clicked: the point of the row is the place, so go there. */
export function flyToBlock(id) {
  const f = blockFeature(id);
  if (!f || !map) return;
  let [w, s, e, n] = [180, 90, -180, -90];
  const walk = c => Array.isArray(c[0]) ? c.forEach(walk) : (() => {
    const [x, y] = c;
    if (x < w) w = x; if (x > e) e = x;
    if (y < s) s = y; if (y > n) n = y;
  })();
  walk(f.geometry.coordinates);
  if (w > e || s > n) return;
  try {
    map.fitBounds([[w, s], [e, n]], {
      padding: { top: 90, bottom: 90, left: 90, right: 90 + currentRightPad() },
      maxZoom: 16.2, duration: 700,
    });
  } catch (err) { /* framing is a nicety; the selection already happened */ }
}

/* ── drawer ─────────────────────────────────────────────────────────── */
