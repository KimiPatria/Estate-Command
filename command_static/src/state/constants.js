export const SCALE_STEPS = [
  { step: 'group',    maxZoom: 9.5 },
  { step: 'estate',   maxZoom: 12.5 },
  { step: 'division', maxZoom: 14.2 },
  { step: 'block',    maxZoom: 99 },
];

/* Executive carries no zoom of its own. A fixed altitude cannot frame estates
   that sit 3,000 km apart, so the role fits the whole group instead and lets
   the bounds decide the height. */
export const ROLES = {
  master:    { label: 'Plantation Master', short: 'Master',    hint: 'Block detail, daily decisions', zoom: 14.5, metric: 'bunches_per_ha' },
  executive: { label: 'Executive',         short: 'Executive', hint: 'Every estate in one view',      fitAll: true, metric: 'bunches_total' },
};

/* Below this the estates are too small to aim at, so pins take over as the
   way to reach them. Above it they would only cover the blocks. */
export const PIN_MAX_ZOOM = 10.5;
/* Screen-space radius for grouping pins. Two estates whose dots would land
   this close get one dot between them. */
export const CLUSTER_PX = 64;

/* Sequential and diverging ramps, both readable over satellite imagery.
   peer_index diverges around 1.0 because the question it answers is
   "behind or ahead of cohort", which has a meaningful midpoint. */
export const RAMP_SEQ = ['#1a3a4a','#1e5c6b','#2d8a8a','#6ab08a','#b8c96e','#f0c44c'];
export const RAMP_DIV = ['#c0503c','#d98a52','#e8c179','#8fbf9f','#4f9d7e','#2d7d63'];
export const DIVERGING = new Set(['peer_index']);


export const PROV_LABEL = { real: 'real', derived: 'derived', synthetic: 'synth' };

