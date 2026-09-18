/* The rail: the catalogue, and nothing else.
 *
 * It used to carry six unrelated jobs in one 252px column. Perspective and
 * estates went to the context bar, the metric picker and legend went onto the
 * map, readiness became a header button, and fire stopped being promoted above
 * the five domains it is a peer of. What is left is six icons, and the width
 * that freed went to the imagery, which is what the user came to look at.
 */
import { esc } from '../lib/fmt.js';
import { S } from '../state/store.js';
import { openPanel } from '../panels/registry.js';

/* One glyph per domain. Keyed by the manifest's domain id, so a new domain in
   gis/features.py shows up with a fallback rather than breaking the rail. */
const DOMAIN_ICON = {
  harvesting: '<path d="M3 21h18"/><path d="M7 21V9l5-6 5 6v12"/><path d="M12 21v-6"/>',
  upkeep: '<path d="M14.7 6.3a4 4 0 0 0 5 5l-9.4 9.4a2.1 2.1 0 0 1-3-3z"/><circle cx="17" cy="7" r="3.6"/>',
  pest: '<path d="M12 3v4"/><ellipse cx="12" cy="13" rx="5" ry="8"/><path d="M7 10H3m4 6H3m14-6h4m-4 6h4"/>',
  transport: '<path d="M1 3h13v13H1z"/><path d="M14 8h4l3 3v5h-7z"/><circle cx="5.5" cy="18.5" r="2"/><circle cx="17.5" cy="18.5" r="2"/>',
  commercial: '<path d="M12 1v22"/><path d="M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/>',
  governance: '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><polyline points="9 12 11 14 15 10"/>',
};
const FALLBACK_ICON = '<circle cx="12" cy="12" r="9"/>';

let DOMAINS = [];
let openDomain = null;

function el(id) { return document.getElementById(id); }

export function setRailDomains(domains) {
  DOMAINS = domains || [];
  renderRail();
}

/* ── the icon column ───────────────────────────────────────────────────── */

export function renderRail() {
  el('rail-doms').innerHTML = DOMAINS.map(d => {
    const flagged = flaggedCount(d);
    return `<button class="dom-ico ${d.domain === openDomain ? 'on' : ''}"
                    data-domain="${esc(d.domain)}" title="${esc(d.label)}"
                    aria-label="${esc(d.label)}">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"
           stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        ${DOMAIN_ICON[d.domain] || FALLBACK_ICON}
      </svg>
      ${flagged ? `<span class="badge">${flagged}</span>` : ''}
      <span class="tip">${esc(d.label)}</span>
    </button>`;
  }).join('');
  el('rail-doms').querySelectorAll('[data-domain]').forEach(b => {
    b.onclick = () => toggleDomain(b.dataset.domain);
  });
}

/* How many of this domain's panels have something to say. The badge is what
   turns the rail from a menu into a briefing: it says where to look first. */
function flaggedCount(d) {
  const seen = new Set();
  let n = 0;
  for (const f of d.features) {
    if (seen.has(f.panel)) continue;
    seen.add(f.panel);
    if (S.panelHint[f.panel]) n++;
  }
  return n;
}

/* ── the flyout ────────────────────────────────────────────────────────── */

export function toggleDomain(key) {
  if (openDomain === key) { closeDomain(); return; }
  const d = DOMAINS.find(x => x.domain === key);
  if (!d) return;
  openDomain = key;
  el('dom-fly-t').textContent = d.label;
  el('dom-fly-blurb').textContent = d.blurb || '';
  const cov = el('dom-fly-cov');
  cov.textContent = `${d.on_real_data}/${d.count}`;
  cov.className = 'cov ' + (d.on_real_data === d.count ? 'all' : d.on_real_data ? 'part' : '');
  el('dom-fly').hidden = false;
  // The flyout overlays the map's own bottom-left controls, so tell them how
  // far to step aside. Shifting beats reflowing: the canvas never resizes.
  document.documentElement.style.setProperty('--fly', '282px');
  renderRail();
  renderPanels();
}

export function closeDomain() {
  openDomain = null;
  el('dom-fly').hidden = true;
  document.documentElement.style.setProperty('--fly', '0px');
  renderRail();
}

export function railOpenDomain() { return openDomain; }

/* ── the panel list for the open domain ────────────────────────────────── */

export function renderPanels() {
  const host = el('panels');
  const d = DOMAINS.find(x => x.domain === openDomain);
  if (!d) { host.innerHTML = ''; return; }

  const seen = new Set();
  host.innerHTML = d.features.filter(f => {
    if (seen.has(f.panel)) return false;
    seen.add(f.panel);
    return true;
  }).map(f => {
    const hint = S.panelHint[f.panel] || (f.status === 'planned' ? 'needs data' : '');
    const cls = ['panel-btn',
                 S.panelHint[f.panel] ? 'alertish' : '',
                 f.status === 'planned' ? 'planned' : ''].filter(Boolean).join(' ');
    return `<button class="${cls}" data-panel="${esc(f.panel)}" title="${esc(f.question)}">
      <span>${esc(f.label)}</span>
      <span class="hint">${esc(hint)}</span>
    </button>`;
  }).join('');

  host.querySelectorAll('[data-panel]').forEach(b => {
    b.onclick = () => openPanel(b.dataset.panel);
  });
}

/* Repaint the badges and any open list after the hints land. */
export function refreshRail() {
  renderRail();
  if (openDomain) renderPanels();
}

export function initRail() {
  el('dom-fly-x').onclick = closeDomain;
}
