import { map } from './instance.js';
import { fmt } from '../lib/fmt.js';
import { selectBlockById } from './select.js';
import { renderMetricButton } from './mapctl.js';
import { clearLegendBand } from './legend-filter.js';
import { DIVERGING, RAMP_DIV, RAMP_SEQ } from '../state/constants.js';
import { S } from '../state/store.js';

/* ── metric + choropleth ────────────────────────────────────────────── */
export async function setMetric(metric) {
  if (!S.blocks) return;
  S.metric = metric;
  // A band drawn on the old ramp means nothing on the new one.
  clearLegendBand();
  renderMetricButton();
  const qs = new URLSearchParams({ estate: S.estate, metric });
  if (S.month) qs.set('month', S.month);
  const res = await fetch(`/gis/metrics?${qs}`);
  if (!res.ok) return;
  S.metricData = await res.json();
  paintChoropleth();
  renderLegend();
  if (S.selected) selectBlockById(S.selected);
}

export function paintChoropleth() {
  const d = S.metricData;
  if (!d || !d.domain) return;
  const [lo, hi] = d.domain;
  const diverging = DIVERGING.has(d.metric);
  const ramp = diverging ? RAMP_DIV : RAMP_SEQ;

  // Anchor a diverging ramp on 1.0 (parity with cohort) and make the two
  // arms symmetric, so equal distance from parity reads as equal colour.
  let stops;
  if (diverging) {
    const reach = Math.max(Math.abs(1 - lo), Math.abs(hi - 1)) || 0.001;
    stops = ramp.map((c, i) => [1 - reach + (2 * reach * i) / (ramp.length - 1), c]);
  } else {
    const span = (hi - lo) || 1;
    stops = ramp.map((c, i) => [lo + (span * i) / (ramp.length - 1), c]);
  }

  // Values ride in the feature properties and the source is re-set. With 291
  // features that costs nothing and cannot fail the way feature-state can.
  // NO_VALUE marks a block the metric could not be computed for, so a missing
  // reading renders as a visible hole rather than as the bottom of the ramp.
  const NO_VALUE = -999999;
  S.blocks.features.forEach(f => {
    const v = d.values[f.id];
    f.properties._v = (v === null || v === undefined) ? NO_VALUE : v;
    f.properties._id = f.id;
  });
  map.getSource('blocks').setData(S.blocks);

  const expr = ['interpolate', ['linear'], ['get', '_v']];
  stops.forEach(([v, c]) => expr.push(v, c));
  map.setPaintProperty('blocks-fill', 'fill-color',
    ['case', ['==', ['get', '_v'], NO_VALUE], 'rgba(140,160,155,.5)', expr]);
  map.setPaintProperty('blocks-fill', 'fill-opacity',
    ['case', ['==', ['get', '_v'], NO_VALUE], 0.18, 0.66]);
}

export function renderLegend() {
  const d = S.metricData;
  const grp = document.getElementById('legend-grp');
  if (!d || !d.domain) { grp.hidden = true; return; }
  grp.hidden = false;
  const ramp = DIVERGING.has(d.metric) ? RAMP_DIV : RAMP_SEQ;
  document.getElementById('legend-bar').style.background = `linear-gradient(90deg, ${ramp.join(',')})`;
  const dec = d.metric === 'deduction_rate' ? 3 : (d.metric === 'peer_index' ? 2 : 0);
  document.getElementById('legend-lo').textContent = fmt(d.domain[0], dec);
  document.getElementById('legend-hi').textContent = fmt(d.domain[1], dec);
  document.getElementById('legend-note').textContent =
    `${d.n} blocks · median ${fmt(d.median, dec)}${S.month ? ` · ${S.month}` : ' · full window'}`;
}

