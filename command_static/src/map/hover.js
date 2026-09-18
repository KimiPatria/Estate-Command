/* A readout under the cursor.
 *
 * The choropleth had no hover affordance of any kind: the only way to learn
 * what a colour meant was to click, which opened a 372px panel and moved the
 * camera. That is an expensive way to ask "what is this one". Thirty lines of
 * chip make the whole map readable without committing to anything.
 */
import { fmt } from '../lib/fmt.js';
import { PROV_LABEL } from '../state/constants.js';
import { S } from '../state/store.js';
import { map } from './instance.js';

let chip = null;
let frame = 0;
let pending = null;

function ensureChip() {
  if (chip) return chip;
  chip = document.createElement('div');
  chip.className = 'hover-chip';
  chip.hidden = true;
  document.querySelector('.map-wrap').appendChild(chip);
  return chip;
}

function decimalsFor(metric) {
  if (metric === 'deduction_rate') return 3;
  if (metric === 'peer_index' || metric === 'ripe_rate') return 2;
  return 0;
}

function paint({ props, point }) {
  const el = ensureChip();
  if (!props) { el.hidden = true; return; }

  const p = props;
  const d = S.metricData;
  const id = p._id;
  const v = d && d.values ? d.values[id] : undefined;
  const label = p.block_label || p.block_code || '—';

  const value = v === undefined || v === null
    ? '<span class="na">not recorded</span>'
    : `<b>${fmt(v, decimalsFor(d.metric))}</b>`;

  el.innerHTML = `
    <span class="blk">${label}</span>
    <span class="sep"></span>
    <span class="val">${value}</span>
    ${d ? `<span class="mx">${d.label}</span>
           <span class="prov ${d.provenance}">${PROV_LABEL[d.provenance] || d.provenance}</span>`
        : ''}`;

  // Flip before the cursor near the right edge, so the chip never leaves the
  // canvas and never sits under the pointer.
  const wrap = document.querySelector('.map-wrap').getBoundingClientRect();
  const flip = point.x > wrap.width - 240;
  el.style.left = `${point.x + (flip ? -14 : 14)}px`;
  el.style.top = `${point.y + 14}px`;
  el.style.transform = flip ? 'translateX(-100%)' : '';
  el.hidden = false;
}

export function initHover() {
  if (!map) return;
  map.on('mousemove', 'blocks-fill', e => {
    // Read the features here, synchronously. MapLibre attaches `e.features`
    // only for the duration of the layer-scoped dispatch and deletes it on the
    // way out, so anything deferred to a later frame sees an empty event.
    const f = e.features && e.features[0];
    pending = f ? { props: f.properties, point: e.point } : { props: null, point: e.point };
    // Coalesced into one frame: MapLibre fires mousemove far faster than the
    // page repaints, and this writes to the DOM.
    if (frame) return;
    frame = requestAnimationFrame(() => { frame = 0; if (pending) paint(pending); });
  });
  map.on('mouseleave', 'blocks-fill', () => {
    pending = null;
    if (chip) chip.hidden = true;
  });
}
