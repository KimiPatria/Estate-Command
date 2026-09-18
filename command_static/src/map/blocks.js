/* Resolving a panel's idea of a block to the map's idea of one.
 *
 * The endpoints do not agree on how to name a block. /gis/rotation returns a
 * full `block_id` ("block:EC:4|151"), /gis/pest, /gis/roads and /gis/nutrition
 * return a short `block_label` ("33-56"), /gis/productivity returns `block`,
 * and /gis/clusters returns a `block_labels` array on each group. The map keys
 * on the full id.
 *
 * Rather than change eight server payloads, resolve here. That keeps this a
 * UI concern, and it means a panel can paint its blocks without its endpoint
 * having to know the map exists.
 */
import { S } from '../state/store.js';

let index = null;      // label -> full id, plus id -> id
let indexedFor = null; // the estate the index was built for

function build() {
  index = new Map();
  const feats = (S.blocks && S.blocks.features) || [];
  for (const f of feats) {
    const id = f.id || f.properties._id;
    if (!id) continue;
    const p = f.properties;
    index.set(id, id);
    // Four spellings, because four endpoints disagree:
    //   block_label   "32-51"   pest, roads, nutrition, rotation, vegetation
    //   div-code      "1-1"     productivity
    //   div|code      "2|65"    vegetation's `key`
    //   block_code    "1"       last resort, ambiguous across divisions
    // Labels are unique inside an estate but not across them, and the map only
    // ever holds one estate's blocks, so this is safe here and nowhere else.
    if (p.block_label) index.set(String(p.block_label), id);
    if (p.division_code && p.block_code) {
      index.set(`${p.division_code}-${p.block_code}`, id);
      index.set(`${p.division_code}|${p.block_code}`, id);
    }
  }
  indexedFor = S.estate;
}

function ensure() {
  if (!index || indexedFor !== S.estate) build();
  return index;
}

/* Blocks change when the estate does, so let the loader say so explicitly
   rather than relying on the estate check alone. */
export function invalidateBlockIndex() { index = null; indexedFor = null; }

/* The keys a payload might use, in the order worth trying. */
const REF_KEYS = ['block_id', 'block_label', 'block', 'key', 'block_code', 'id', 'label'];

export function blockIdFor(ref) {
  if (ref === null || ref === undefined) return null;
  const idx = ensure();
  if (typeof ref === 'string' || typeof ref === 'number') {
    return idx.get(String(ref)) || null;
  }
  for (const k of REF_KEYS) {
    if (ref[k] !== undefined && ref[k] !== null) {
      const hit = idx.get(String(ref[k]));
      if (hit) return hit;
    }
  }
  return null;
}

/* Map a list of rows (or bare labels) to map ids, dropping what will not
   resolve. A panel naming a block the map does not carry highlights nothing,
   which is the correct outcome and never an exception. */
export function blockIdsFor(rows) {
  if (!rows) return [];
  const out = [];
  const seen = new Set();
  for (const r of rows) {
    const id = blockIdFor(r);
    if (id && !seen.has(id)) { seen.add(id); out.push(id); }
  }
  return out;
}

export function blockLabelFor(id) {
  const feats = (S.blocks && S.blocks.features) || [];
  const f = feats.find(x => (x.id || x.properties._id) === id);
  return f ? (f.properties.block_label || f.properties.block_code) : null;
}

export function blockFeature(id) {
  const feats = (S.blocks && S.blocks.features) || [];
  return feats.find(x => (x.id || x.properties._id) === id) || null;
}
