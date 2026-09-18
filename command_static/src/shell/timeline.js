import { loadBlockRows } from '../ai/briefs.js';
import { setMetric } from '../map/choropleth.js';
import { S } from '../state/store.js';

/* ── time scrubber ──────────────────────────────────────────────────── */
export function renderMonths() {
  const el = document.getElementById('months');
  const hz = document.getElementById('horizon');
  if (!S.months.length) {
    el.innerHTML = '';
    hz.textContent = 'No harvest history for this estate.';
    return;
  }
  const fwd = S.forwardMonths || [];
  // Solid months are recorded harvest, dashed months are forecast. The divider
  // marks where the client's data stops and the projection starts.
  el.innerHTML =
    `<button class="month all ${S.month === null ? 'on' : ''}" data-month="">Full window</button>` +
    S.months.map(m => `<button class="month ${m === S.month ? 'on' : ''}" data-month="${m}">${m.slice(2)}</button>`).join('') +
    (fwd.length ? '<span class="divider"></span>' : '') +
    fwd.map(m => `<button class="month fcst ${m === S.month ? 'on' : ''}" data-month="${m}">${m.slice(2)}</button>`).join('');
  el.querySelectorAll('[data-month]').forEach(b => {
    b.onclick = () => {
      S.month = b.dataset.month || null;
      renderMonths();
      setMetric(S.metric);
      loadBlockRows();
    };
  });
  const forecasting = S.month && fwd.includes(S.month);
  hz.innerHTML = forecasting
    ? `<span style="color:var(--gold)">Forecast month.</span> The recorded export ends 2025-05-23; everything past the divider is generated.`
    : `${S.months.length} months recorded, ${fwd.length} forecast. Solid is the client's data, dashed is projection.`;
}

