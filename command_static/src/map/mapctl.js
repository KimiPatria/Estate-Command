/* The metric picker and its legend, on the map.
 *
 * A legend is the key to what you are looking at. It spent this app's life in
 * a sidebar accordion two groups above the thing it keyed, which meant the
 * colours on screen were frequently unexplained. Here it sits on the imagery
 * it describes, with the metric name above it, and the full catalogue of 31
 * metrics one click away instead of permanently occupying a scrolling column.
 */
import { esc } from '../lib/fmt.js';
import { PROV_LABEL } from '../state/constants.js';
import { S } from '../state/store.js';
import { showPop, closePop } from '../shell/pop.js';
import { setMetric } from './choropleth.js';
import { initLegendFilter } from './legend-filter.js';
import { openCompare } from '../panels/registry.js';

function el(id) { return document.getElementById(id); }

/* The button: what the map is currently painted with. */
export function renderMetricButton() {
  const m = (S.catalogue || []).find(x => x.key === S.metric);
  el('metric-lbl').textContent = m ? m.label : 'No metric';
  const prov = el('metric-prov');
  if (m) {
    prov.className = `prov ${m.provenance}`;
    prov.textContent = PROV_LABEL[m.provenance] || m.provenance;
  } else {
    prov.textContent = '';
  }
  el('metric-btn').disabled = !S.blocks;
  el('mapctl').hidden = !S.catalogue || !S.catalogue.length;
}

/* The menu: the whole catalogue, with every provenance badge visible at once.
   That column of real / derived / synth is the pitch in miniature, and it
   reads better as a block than as a list you scroll past one row at a time. */
export function renderMetrics() {
  const on = !!S.blocks;
  // Synthetic and derived layers exist only for the estate the synthetic
  // feeds were generated for. Elsewhere they would paint an empty map.
  const row = (S.estates || []).find(e => e.estate_code === S.estate);
  const synth = !row || row.has_synthetic_feeds;
  el('metrics').innerHTML = (S.catalogue || []).map(m => {
    const noFeed = !synth && m.provenance !== 'real';
    return `
    <button class="metric ${m.key === S.metric ? 'on' : ''}" data-metric="${esc(m.key)}"
            ${on && !noFeed ? '' : 'disabled'}
            ${noFeed ? `title="No synthetic feeds for ${esc(S.estate)}: real metrics only"` : ''}>
      <span>${esc(m.label)}</span>
      <span class="prov ${m.provenance}">${PROV_LABEL[m.provenance] || m.provenance}</span>
    </button>`;
  }).join('');
  el('metrics').querySelectorAll('[data-metric]').forEach(b => {
    b.onclick = e => {
      closeMetricMenu();
      // Alt picks the second metric for a comparison rather than repainting.
      if (e.altKey) openCompare(b.dataset.metric);
      else setMetric(b.dataset.metric);
    };
    // A second, explicit way in: not everyone finds a modifier.
    const cmp = document.createElement('span');
    cmp.className = 'metric-cmp';
    cmp.title = 'Compare with the metric on the map';
    cmp.textContent = 'compare';
    cmp.onclick = e => { e.stopPropagation(); closeMetricMenu(); openCompare(b.dataset.metric); };
    b.appendChild(cmp);
  });
  renderMetricButton();
}

function closeMetricMenu() {
  el('metric-menu').hidden = true;
  el('metric-btn').setAttribute('aria-expanded', 'false');
}

export function initMapCtl() {
  initLegendFilter();
  const menu = el('metric-menu');
  el('metric-btn').onclick = e => {
    e.stopPropagation();
    closePop();
    const opening = menu.hidden;
    menu.hidden = !opening;
    el('metric-btn').setAttribute('aria-expanded', String(opening));
    if (opening) {
      const active = menu.querySelector('.metric.on');
      if (active) active.scrollIntoView({ block: 'nearest' });
    }
  };
  document.addEventListener('pointerdown', e => {
    if (menu.hidden) return;
    if (menu.contains(e.target) || el('metric-btn').contains(e.target)) return;
    closeMetricMenu();
  });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && !menu.hidden) closeMetricMenu();
  });
}
