import { loadHandover } from '../ai/briefs.js';
import { esc } from '../lib/fmt.js';
import { panelContract, panelVendors } from './commercial.js';
import { recordDecision } from './decisions.js';
import { panelCompare } from './compare.js';
import { panelFire, wireFirePanel } from './fire.js';
import { panelAudit, panelCanopy } from './governance.js';
import { panelLabour, panelRotation } from './harvesting.js';
import { panelClusters, panelForecast, panelProductivity, panelShrinkage } from './models.js';
import { panelPest } from './pest.js';
import { panelTransport } from './transport.js';
import { panelNutrition, panelReplant, panelRoads } from './upkeep.js';
import { assumptionsPopup, opsPopup, outcomesPopup } from './ops.js';
import { forecastsPopup } from './forecasts.js';
import { storesPopup } from './stores.js';
import { panelLooseFruit } from './loose_fruit.js';
import { panelHerbicide } from './herbicide.js';
import { panelFfa } from './ffa.js';
import { panelCuttingInterval } from './cutting_interval.js';
import { panelAbwTrend } from './abw_trend.js';
import { panelPestWarning } from './pest_warning.js';
import { panelCollection } from './collection.js';
import { blockList } from './_shared.js';
import { blockLabelFor } from '../map/blocks.js';
import { onPopupChange, openPopup, popupKey } from '../shell/popup.js';
import { wireDrawer } from '../shell/drawer.js';

import { WB, addTab } from '../shell/workbench.js';
import { refreshRail, setRailDomains } from '../shell/rail.js';
import { S } from '../state/store.js';

/* ── decision panels ────────────────────────────────────────────────────── */

/* Panels the sheet can render. The label and the domain now come from the
   feature manifest at /gis/features, so this map only says which renderer
   draws which panel. A panel with no renderer yet is listed by the manifest
   and marked planned; it is still worth showing, because the point of the
   catalogue is what the app could do with the client's data. */
export const PANEL_RENDER = {
  rotation:   () => panelRotation(),
  labour:     () => panelLabour(),
  replant:    () => panelReplant(),
  contract:   () => panelContract(),
  vendors:    () => panelVendors(),
  audit:      () => panelAudit(),
  shrinkage:  () => panelShrinkage(),
  turnaround: () => panelTransport('turnaround'),
  fuel:       () => panelTransport('fuel'),
  fleet:      () => panelTransport('fleet'),
  pest_census:    () => panelPest('pest_census'),
  pest_spread:    () => panelPest('pest_spread'),
  pest_damage:    () => panelPest('pest_damage'),
  pest_treatment: () => panelPest('pest_treatment'),
  nutrient:   () => panelNutrition(),
  roads:      () => panelRoads(),
  clusters:   () => panelClusters(),
  productivity: () => panelProductivity(),
  forecast:   () => panelForecast(),
  fire:       () => panelFire(),
  canopy:     () => panelCanopy(),
  loose_fruit:      () => panelLooseFruit(),
  herbicide:        () => panelHerbicide(),
  ffa:              () => panelFfa(),
  cutting_interval: () => panelCuttingInterval(),
  abw_trend:        () => panelAbwTrend(),
  pest_warning:     () => panelPestWarning(),
  collection:       () => panelCollection(),
};

/* Features that are a map layer rather than a panel. The manifest lists them
   beside everything else, so the rail offers them; clicking one paints the
   metric and docks the ranked list, which is what the choropleth cannot show
   on its own. Before this they fell through to "not yet built". */
const METRIC_PANELS = {
  peer_yield: 'peer_index',
  upkeep: 'upkeep_overdue',
  margin: 'margin_per_ha',
};

/* Features that are a drawer the header already opens. */
const DRAWER_PANELS = {
  readiness: 'readiness-btn',
  ask: 'ask-launch',
};

async function metricPanel(metric) {
  // Dynamic: choropleth -> mapctl -> registry would be a static cycle.
  const { setMetric } = await import('../map/choropleth.js');
  await setMetric(metric);
  const d = S.metricData;
  if (!d || d.metric !== metric || !d.values) {
    return `<div class="empty">The map could not load ${esc(metric)}.</div>`;
  }
  const rows = Object.entries(d.values)
    .filter(([, v]) => v !== null && v !== undefined)
    .map(([id, v]) => ({ block_label: blockLabelFor(id) || id, value: v }));
  const worstFirst = d.inverted ? (a, b) => b.value - a.value : (a, b) => a.value - b.value;
  rows.sort(worstFirst);
  const cols = [{ key: 'value', label: d.label, num: true,
                  fmt: v => Number(v).toLocaleString('en-US', { maximumFractionDigits: 2 }) }];
  return `<div class="kpis">
      <div class="kpi"><b>${rows.length}</b><span>blocks measured</span></div>
      <div class="kpi"><b>${d.median === null || d.median === undefined ? '—'
        : Number(d.median).toLocaleString('en-US', { maximumFractionDigits: 2 })}</b><span>median</span></div>
      <div class="kpi"><b>${esc(d.provenance)}</b><span>provenance</span></div>
    </div>
    <div class="sub-t">Weakest twenty</div>
    ${blockList(rows.slice(0, 20), { cols })}
    <div class="sub-t">Strongest ten</div>
    ${blockList(rows.slice(-10).reverse(), { cols })}`;
}

/* Features a person works in rather than glances at open in their own
   window instead of the dock: a section menu down the left, one section at a
   time, and Show on map to hand blocks to the full map. The window is titled
   with the same label the rail shows; the manifest's question is its
   subtitle. */
const POPUPS = {
  ops_harvest:  () => opsPopup('ops_harvest'),
  ops_prune:    () => opsPopup('ops_prune'),
  ops_weed:     () => opsPopup('ops_weed'),
  ops_pest:     () => opsPopup('ops_pest'),
  ops_dispatch: () => opsPopup('ops_dispatch'),
  assumptions:  () => assumptionsPopup(),
  outcomes:     () => outcomesPopup(),
  forecasts:    () => forecastsPopup(),
  stores:       () => storesPopup(),
};
export const POPUP_PANELS = Object.keys(POPUPS);

/* Filled from /gis/features on boot: panel key -> {label, domain, ...}. */
export let PANELS = {};
export let DOMAINS = [];

/* Pull the headline number for each panel once, so the rail shows what is
   wrong before anyone opens anything. A panel list that just names use cases
   is a menu; one that says "6 months short" is a briefing. */
export async function primePanelHints() {
  try {
    const [contract, rotation, labour, replant, log] = await Promise.all([
      fetch('/gis/contract?estate=EC').then(r => r.json()),
      fetch('/gis/rotation?estate=EC&top=1').then(r => r.json()),
      fetch('/gis/labour?estate=EC').then(r => r.json()),
      fetch('/gis/replant?estate=EC').then(r => r.json()),
      fetch('/gis/decisions?limit=1').then(r => r.json()),
    ]);
    S.contract = contract;
    S.panelHint = {
      contract: `${contract.months_in_p50_deficit} months short`,
      vendors: `${Math.round(Math.abs(contract.worst_forward_month.gap_t))} t to cover`,
      rotation: `${rotation.overdue_blocks} overdue`,
      labour: `${labour.worst_deficit} short`,
      replant: `${replant.peak_pct}% in ${replant.peak_year}`,
      audit: log.total ? `${log.total} logged` : '',
    };
    refreshRail();
  } catch (e) {
    // A panel hint is a nicety; losing it must never block the map.
  }
  // The operations hints ride separately: the ledger is a second fetch and
  // the rail should not wait on it to show the six domains.
  try {
    const [o, a, w] = await Promise.all([
      fetch('/gis/ops').then(r => r.json()),
      fetch('/gis/assumptions').then(r => r.json()),
      fetch('/gis/ops/outcomes?estate=EC').then(r => r.json()),
    ]);
    const s = o.summary || {};
    const adh = k => (s[k] && s[k].available && s[k].adherence_pct !== null)
      ? `${Math.round(s[k].adherence_pct)}% adherence` : '';
    Object.assign(S.panelHint, {
      ops_harvest: adh('harvest'), ops_prune: adh('prune'),
      ops_weed: adh('weed'), ops_pest: adh('pest'), ops_dispatch: adh('dispatch'),
      assumptions: a.overridden ? `${a.overridden} edited` : `${a.total} to agree`,
      outcomes: w.counts && w.counts.accepted ? `${w.counts.accepted} plans` : '',
    });
    S.panelHint.assumptionsCount = a.overridden || 0;
    refreshRail();
  } catch (e) {
    // As above.
  }
  // The outlook waits on the forecasting models, which fit in the background
  // when the server starts; the rail must never wait on them.
  // The store's hint waits on its own replay, on its own thread server-side.
  try {
    const st = await fetch('/gis/stores').then(x => x.json());
    if (st.available) {
      S.panelHint.stores = st.to_order ? `${st.to_order} to order` : 'nothing due';
      refreshRail();
    }
  } catch (e) {
    // As above.
  }
  try {
    const r = await fetch('/gis/forecast/rain').then(x => x.json());
    const c = r.forecast && r.forecast.available ? r.forecast.chances.washoff : null;
    if (c) {
      S.panelHint.forecasts = `${c.pct}% rain chance`;
      refreshRail();
    }
  } catch (e) {
    // As above.
  }
}

/* Load the feature manifest and build the panel registry from it. The rail is
   grouped by operational domain because that is how the estate divides the
   work; the old UC-numbered list was consultant shorthand. */
export async function loadFeatures() {
  const cat = await (await fetch('/gis/features')).json();
  S.features = cat;
  DOMAINS = cat.domains;
  PANELS = {};
  for (const d of cat.domains) {
    for (const f of d.features) {
      // Several features can share a panel; the first one names it, and the
      // panel's own requirements strip unions what they all need.
      if (!PANELS[f.panel]) {
        PANELS[f.panel] = {
          label: f.label, domain: d.domain, domainLabel: d.label,
          status: f.status, runsOn: f.runs_on, question: f.question, ml: !!f.ml,
        };
      } else if (f.ml) {
        PANELS[f.panel].ml = true;
      }
    }
  }
  // The rail renders from the manifest, so hand it over rather than having it
  // fetch the same document a second time.
  setRailDomains(cat.domains);
  return cat;
}

/* ── the requirements strip ──────────────────────────────────────────────
   What this panel would need to run on the client's own numbers. Rendered at
   the top of every sheet from the manifest, so the ask is attached to the
   feature at the moment the client is looking at it.

   In the dock it is a disclosure, closed when the panel opens: the summary
   line still says how many inputs are satisfied, and the panel's own content
   is what the reader came for. A window gives it a section of its own, where
   it is the whole page, so there it renders open (collapsible: false). */
const CHEVRON = `<svg class="needs-chev" viewBox="0 0 24 24" fill="none" stroke="currentColor"
  stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
  <polyline points="9 6 15 12 9 18"/></svg>`;

export async function needsStrip(panel, { collapsible = true } = {}) {
  let v;
  try {
    const r = await fetch(`/gis/features/panel?panel=${encodeURIComponent(panel)}`);
    if (!r.ok) return '';
    v = await r.json();
  } catch (e) { return ''; }
  if (!v.requires.length) return '';

  const have = v.requirements_satisfied, tot = v.requirements_total;
  const items = v.requires.map(r => `
    <li class="${r.satisfied ? 'have' : 'want'}">
      <span class="dot"></span>
      <span><span class="ent">${esc(r.entity)}</span>
        <span class="src">· ${esc(r.system_label)}${r.table ? ' · ' + esc(r.table) : ''}</span>
        ${r.note ? `<br><span class="src">${esc(r.note)}</span>` : ''}</span>
    </li>`).join('');

  const head = `
      <b>What this needs</b>
      <span class="prov ${v.runs_on === 'real' ? 'real'
        : v.runs_on === 'mixed' ? 'derived' : 'synthetic'}">${esc(v.runs_on)}</span>
      <span class="sum">${have} of ${tot} satisfied</span>`;
  const body = `<ul>${items}</ul>`;

  if (!collapsible) {
    return `<div class="needs"><div class="needs-h">${head}</div>${body}</div>`;
  }
  return `<details class="needs">
    <summary class="needs-h">${CHEVRON}${head}</summary>
    <div class="needs-b">${body}</div>
  </details>`;
}

/* Where a panel opens: 'window' for a full-screen feature window, 'side' for
   everything else (the workbench dock, or the drawer, both beside the map). */
export function panelOpensIn(kind) {
  return POPUPS[kind] ? 'window' : 'side';
}

export async function openPanel(kind) {
  const meta = PANELS[kind] || { label: kind, question: '' };
  if (POPUPS[kind]) {
    const opening = openPopup({
      ...POPUPS[kind](),
      title: meta.label,
      ml: !!meta.ml,
      subtitle: meta.question || '',
      needs: () => needsStrip(kind, { collapsible: false }),
    });
    markActivePanel();
    await opening;
    markActivePanel();
    return;
  }
  if (DRAWER_PANELS[kind]) {
    const btn = document.getElementById(DRAWER_PANELS[kind]);
    if (btn) btn.click();
    return;
  }
  // The requirements strip and the panel body are independent: a panel with
  // no renderer yet still shows what it would need. The strip goes last so
  // the panel's own title and subtitle are the first thing read.
  const body = await addTab({
    kind: 'panel', key: kind,
    label: meta.label, question: meta.question || '', ml: !!meta.ml,
    after: PANEL_AFTER[kind],
    render: async () => {
      const needs = await needsStrip(kind);
      const render = PANEL_RENDER[kind]
        || (METRIC_PANELS[kind] ? () => metricPanel(METRIC_PANELS[kind]) : null);
      const main = render
        ? await render()
        : `<div class="sheet-note">Not built yet.</div>`;
      return main + needs;
    },
  });
  // A tab the user switched away from mid-fetch paints nothing and returns
  // nothing; there is then no body to wire.
  if (!body) return;
  wireDrawer(body);
  const hb = body.querySelector('#handover-btn');
  if (hb) hb.onclick = () => loadHandover(hb);
  body.querySelectorAll('[data-decide]').forEach(b => {
    b.onclick = () => recordDecision(JSON.parse(b.dataset.decide), b);
  });
  // A dock panel can hand off to a window (the nutrition panel to the store).
  body.querySelectorAll('[data-open-panel]').forEach(b => {
    b.onclick = () => openPanel(b.dataset.openPanel);
  });
  markActivePanel();
}

/* Metric agreement, opened against whatever the map is currently painted
   with. Not in PANEL_RENDER because it takes an argument and has no manifest
   entry: it is a tool over the metrics, not a feature in the catalogue. */
export function openCompare(metricB) {
  const cat = S.catalogue || [];
  const name = k => (cat.find(m => m.key === k) || {}).label || k;
  return addTab({
    kind: 'compare', key: `${S.metric}:${metricB}`,
    label: `${name(S.metric)} vs ${name(metricB)}`,
    question: 'Do two independent measurements agree about this estate?',
    render: () => panelCompare(metricB),
  });
}

/* Panels whose markup hosts live controls, wired after it is in the DOM. */
const PANEL_AFTER = { fire: wireFirePanel };

/* Mirror the open tabs back onto the rail, so the list shows what is already
   on screen rather than behaving like a menu that forgets. */
export function markActivePanel() {
  const open = new Set(WB.tabs.filter(t => t.kind === 'panel').map(t => t.key));
  const win = popupKey();
  if (win) open.add(win);
  document.querySelectorAll('#panels [data-panel]').forEach(b => {
    b.classList.toggle('open', open.has(b.dataset.panel));
    b.classList.toggle('on', WB.active === `panel:${b.dataset.panel}` || win === b.dataset.panel);
  });
}

onPopupChange(() => markActivePanel());

