/* Camera padding and framing.
 *
 * Panels on this page float over the map rather than taking width out of the
 * flex row. Reflowing the canvas on every selection forces a resize and reads
 * as a jolt; easing the camera's padding instead slides the imagery out from
 * under the panel in one movement and leaves the subject in the clear half.
 *
 * This module remembers the padding it last applied, so fitAllEstates can
 * frame a bounds around whatever dock is open without importing the dock, and
 * neither the drawer nor the workbench has to know about the other.
 */
import { map } from './instance.js';
import { S } from '../state/store.js';

let rightPad = 0;

export function easeMapPadding(right) {
  rightPad = right;
  // Deliberately not gated on map.loaded(): that reports false while imagery
  // tiles are still in flight, which is exactly when a first selection tends
  // to happen, and the pan would be skipped with no sign it had been.
  if (!map) return;
  try {
    map.easeTo({ padding: { top: 0, bottom: 0, left: 0, right }, duration: 520 });
  } catch (e) { /* a camera nicety must never break a selection */ }
}


/* What the open dock is currently costing the canvas, so anything framing a
   bounds can keep its subject in the clear half. */
export function currentRightPad() { return rightPad; }

export function fitAllEstates(duration = 1100) {
  const feats = (S.estatesGeo && S.estatesGeo.features) || [];
  if (!feats.length) return;
  let [w, s, e, n] = [180, 90, -180, -90];
  feats.forEach(f => f.geometry.coordinates[0].forEach(([x, y]) => {
    if (x < w) w = x; if (x > e) e = x;
    if (y < s) s = y; if (y > n) n = y;
  }));
  // Asymmetric on purpose. A pin hangs its label 25px below the dot, and the
  // scale bar and attribution sit in the bottom right, so an estate framed at
  // the corner would have its area readout land underneath them.
  map.fitBounds([[w, s], [e, n]], {
    padding: { top: 100, bottom: 132, left: 100, right: 104 + rightPad },
    duration,
  });
}

