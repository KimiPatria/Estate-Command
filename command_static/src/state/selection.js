/* The working set: the blocks a person has gathered, on purpose.
 *
 * This is the verb the app was missing. Everything before it was reading -
 * pick an estate, pick a metric, pick a month, click one block, open a panel,
 * read it. `S.selected` held a single id and there was no way to say "these
 * thirty" to anything.
 *
 * A set can be built five ways, and every one of them ends in the same place:
 *   - shift-click blocks on the map
 *   - drag a range on the legend, taking every block in that band
 *   - take everything an open panel names
 *   - take what the copilot just pointed at
 *   - clear it and start again
 *
 * What makes it worth having is the other end: a set can be briefed, compared,
 * or drafted into one of the six approval documents the estate already uses.
 */
import { S } from './store.js';

const ids = new Set();
const listeners = new Set();

export function selectionIds() { return [...ids]; }
export function selectionSize() { return ids.size; }
export function selectionHas(id) { return ids.has(id); }

export function onSelectionChange(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

function changed() { for (const fn of listeners) fn(selectionIds()); }

export function addToSelection(next) {
  let touched = false;
  for (const id of next || []) {
    if (id && !ids.has(id)) { ids.add(id); touched = true; }
  }
  if (touched) changed();
}

export function removeFromSelection(next) {
  let touched = false;
  for (const id of next || []) if (ids.delete(id)) touched = true;
  if (touched) changed();
}

export function toggleSelection(id) {
  if (!id) return;
  if (ids.has(id)) ids.delete(id); else ids.add(id);
  changed();
}

export function replaceSelection(next) {
  ids.clear();
  for (const id of next || []) if (id) ids.add(id);
  changed();
}

export function clearSelection() {
  if (!ids.size) return;
  ids.clear();
  changed();
}

/* Total planted area of the set, because "thirty blocks" means nothing to an
   estate manager and "812 ha" does. Read off the real ArcGIS polygons. */
export function selectionArea() {
  const feats = (S.blocks && S.blocks.features) || [];
  let ha = 0;
  for (const f of feats) {
    if (ids.has(f.id || f.properties._id)) ha += f.properties.planted_ha || 0;
  }
  return ha;
}

/* The rows the artifact drafters expect. Block identity and planted area are
   real here; anything an artifact adds on top says so for itself. */
export function selectionRows() {
  const feats = (S.blocks && S.blocks.features) || [];
  return feats
    .filter(f => ids.has(f.id || f.properties._id))
    .map(f => ({
      block_id: f.id || f.properties._id,
      block_label: f.properties.block_label,
      division_code: f.properties.division_code,
      planted_ha: f.properties.planted_ha,
      palms: f.properties.palms,
      planted_year: f.properties.planted_year,
    }))
    .sort((a, b) => String(a.block_label).localeCompare(String(b.block_label)));
}

/* An estate change invalidates every id in the set. */
export function resetSelectionForEstate() { clearSelection(); }
