/* The four highlight layers, and who owns each.
 *
 * They never compete: a copilot answer about eight blocks must not clobber the
 * one block a manager clicked, and a panel painting its worst thirty must not
 * clobber either. One layer per source of truth, drawn in a fixed order, is
 * cheaper to reason about than any shared-state scheme.
 *
 *   blocks-sel     one block, clicked          white
 *   blocks-ai      what the copilot points at  violet
 *   blocks-panel   what the open panel names   amber
 *   blocks-set     the working set             teal, filled
 *   blocks-flash   one row, hovered in a panel bright white, brief
 */
import { map } from './instance.js';

export const HL_LAYERS = ['blocks-panel', 'blocks-ai', 'blocks-set',
                          'blocks-sel', 'blocks-flash'];

const NONE = ['==', ['get', '_id'], '__none__'];

function setIds(layer, ids) {
  if (!map || !map.getLayer(layer)) return;
  map.setFilter(layer, ids && ids.length
    ? ['in', ['get', '_id'], ['literal', ids]]
    : NONE);
}

export function addHighlightLayers() {
  // Panel first, so a click and a copilot answer both read over it.
  map.addLayer({
    id: 'blocks-panel', type: 'line', source: 'blocks', filter: NONE,
    paint: { 'line-color': '#d4a84b', 'line-width': 2.0, 'line-opacity': 0.92 },
  });
  // The working set is filled as well as outlined: it is a thing the user
  // built and is about to act on, so it should read as a mass, not a border.
  map.addLayer({
    id: 'blocks-set-fill', type: 'fill', source: 'blocks', filter: NONE,
    paint: { 'fill-color': '#3fb0c4', 'fill-opacity': 0.28 },
  });
  map.addLayer({
    id: 'blocks-set', type: 'line', source: 'blocks', filter: NONE,
    paint: { 'line-color': '#3fb0c4', 'line-width': 2.2 },
  });
  map.addLayer({
    id: 'blocks-flash', type: 'line', source: 'blocks', filter: NONE,
    paint: { 'line-color': '#ffffff', 'line-width': 3.4 },
  });
}

export function highlightSet(ids) {
  setIds('blocks-set', ids);
  setIds('blocks-set-fill', ids);
}

export function highlightPanel(ids) { setIds('blocks-panel', ids); }
export function clearPanelHighlight() { setIds('blocks-panel', []); }
export function flashBlock(id) { setIds('blocks-flash', id ? [id] : []); }

/* Keep the highlights above anything the fire layers draw, in the order the
   comment above states. Called after any layer is added late. */
export function raiseHighlights() {
  if (!map) return;
  for (const id of ['blocks-set-fill', ...HL_LAYERS]) {
    if (map.getLayer(id)) map.moveLayer(id);
  }
}
