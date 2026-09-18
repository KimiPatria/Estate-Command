/* Dragging a range on the legend selects every block in that band.
 *
 * This is the gesture that makes the legend a control rather than a caption.
 * "Show me the bottom fifth" is a thing an estate manager says constantly, and
 * before this the only way to answer it was to read 291 polygons by eye or ask
 * the copilot in a sentence. Here it is a drag across a colour ramp.
 *
 * The band is expressed in the metric's own units, on the same domain the
 * ramp is painted from, so what is selected is exactly what looks selected.
 */
import { fmt } from '../lib/fmt.js';
import { replaceSelection } from '../state/selection.js';
import { S } from '../state/store.js';

let band = null;   // { lo, hi } in metric units, or null

function el(id) { return document.getElementById(id); }

function decimalsFor(metric) {
  if (metric === 'deduction_rate') return 3;
  if (metric === 'peer_index' || metric === 'ripe_rate') return 2;
  return 0;
}

/* Every block whose value falls inside the band, in map ids. */
function idsInBand(lo, hi) {
  const d = S.metricData;
  if (!d || !d.values) return [];
  const out = [];
  for (const [id, v] of Object.entries(d.values)) {
    if (v === null || v === undefined) continue;
    if (v >= lo && v <= hi) out.push(id);
  }
  return out;
}

function paintBand() {
  const overlay = el('legend-band');
  const d = S.metricData;
  if (!overlay) return;
  if (!band || !d || !d.domain) { overlay.hidden = true; return; }
  const [d0, d1] = d.domain;
  const span = d1 - d0 || 1;
  overlay.hidden = false;
  overlay.style.left = `${((band.lo - d0) / span) * 100}%`;
  overlay.style.width = `${((band.hi - band.lo) / span) * 100}%`;
}

export function clearLegendBand() {
  band = null;
  paintBand();
  const note = el('legend-filter-note');
  if (note) note.hidden = true;
}

export function initLegendFilter() {
  const bar = el('legend-bar');
  if (!bar) return;

  // The band overlay and the readout live inside the legend, added here so
  // the markup stays about structure and this file owns its own furniture.
  const overlay = document.createElement('div');
  overlay.className = 'legend-band';
  overlay.id = 'legend-band';
  overlay.hidden = true;
  bar.appendChild(overlay);

  const note = document.createElement('div');
  note.className = 'legend-filter-note';
  note.id = 'legend-filter-note';
  note.hidden = true;
  bar.parentNode.appendChild(note);

  let dragging = false, startX = 0;

  const valueAt = clientX => {
    const d = S.metricData;
    if (!d || !d.domain) return null;
    const r = bar.getBoundingClientRect();
    const t = Math.max(0, Math.min(1, (clientX - r.left) / r.width));
    return d.domain[0] + t * (d.domain[1] - d.domain[0]);
  };

  const apply = () => {
    if (!band) return;
    const ids = idsInBand(band.lo, band.hi);
    replaceSelection(ids);
    const dec = decimalsFor(S.metricData && S.metricData.metric);
    note.hidden = false;
    note.textContent = `${ids.length} block${ids.length === 1 ? '' : 's'} between `
      + `${fmt(band.lo, dec)} and ${fmt(band.hi, dec)}`;
  };

  const move = e => {
    if (!dragging) return;
    const v = valueAt(e.clientX);
    if (v === null) return;
    const a = valueAt(startX);
    band = { lo: Math.min(a, v), hi: Math.max(a, v) };
    paintBand();
    apply();
  };

  const up = () => {
    if (!dragging) return;
    dragging = false;
    document.body.classList.remove('legend-dragging');
    window.removeEventListener('pointermove', move);
    window.removeEventListener('pointerup', up);
  };

  bar.addEventListener('pointerdown', e => {
    if (!S.metricData || !S.metricData.domain) return;
    dragging = true;
    startX = e.clientX;
    document.body.classList.add('legend-dragging');
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up);
    e.preventDefault();
  });

  // A plain click with no drag clears, which is the obvious way out.
  bar.addEventListener('click', e => {
    if (band && band.hi - band.lo < 1e-9) { clearLegendBand(); replaceSelection([]); }
  });
}
