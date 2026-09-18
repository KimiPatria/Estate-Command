/* Fire and force majeure, as a panel.
 *
 * Fire used to have its own rail group, sitting above the five operational
 * domains it is a peer of. It got that promotion because there was nowhere
 * else to put a scenario switch, not because it outranks harvesting. In the
 * manifest it is a Governance feature like any other, so it is one here too,
 * and its controls ride in the panel header where the rest of its context is.
 */
import { esc } from '../lib/fmt.js';
import { openFireBrief } from '../ai/briefs.js';
import { loadFire, renderFireStat, renderScenarios } from '../map/fire.js';
import { map } from '../map/instance.js';
import { S } from '../state/store.js';

/* Wiring, run by the workbench once this markup is in the document. */
export function wireFirePanel() {
  renderScenarios();
  if (S.fire) renderFireStat();
  const tog = document.getElementById('assets-toggle');
  if (tog) tog.onchange = e => {
    const v = e.target.checked ? 'visible' : 'none';
    ['assets-point', 'assets-water-line', 'route-line'].forEach(id => {
      if (map.getLayer(id)) map.setLayoutProperty(id, 'visibility', v);
    });
  };
  const brief = document.getElementById('fire-brief-btn');
  if (brief) brief.onclick = openFireBrief;
  if (!S.fire) loadFire();
}

export function panelFire() {
  const assetsOn = !map || !map.getLayer('assets-point')
    || map.getLayoutProperty('assets-point', 'visibility') !== 'none';

  const html = `
    <div class="fire-panel">
      <div class="sub-t" style="padding-top:0;border-top:none">Scenario</div>
      <div class="scen" id="scenarios"></div>

      <div class="sub-t">The assessment</div>
      <div class="fire-stat" id="fire-stat">Loading</div>
      <div class="bands" id="fire-bands"></div>

      <label class="toggle">
        <input type="checkbox" id="assets-toggle" ${assetsOn ? 'checked' : ''}>
        Show response assets
      </label>

      <button class="gen-btn" id="fire-brief-btn" style="margin-top:11px">
        <svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"
             stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
          <polyline points="14 2 14 8 20 8"/><line x1="8" y1="13" x2="16" y2="13"/>
          <line x1="8" y1="17" x2="13" y2="17"/>
        </svg>
        Duty officer brief
      </button>

      <div class="sheet-note"><b>What is real here.</b> The detection is real:
        NASA FIRMS hotspots over this estate, with live wind from Open-Meteo,
        against the client's own block geometry and palm counts. Every post,
        crew, water source and travel time is synthetic, because EPMS has no
        fire module to carry them.</div>
    </div>`;

  return html;
}
