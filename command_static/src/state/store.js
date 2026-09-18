/* ── state ──────────────────────────────────────────────────────────── */
export const S = {
  estates: [],          // /gis/estates rows
  estate: null,         // selected estate code
  blocks: null,         // GeoJSON for the selected estate, or null
  metric: 'bunches_per_ha',
  month: null,          // null = full window
  months: [],
  metricData: null,
  role: 'master',
  selected: null,       // selected feature id
  readiness: null,
  scenario: 'live',     // live | near_miss | severe
  fire: null,           // /gis/fire payload
  catalogue: null,      // /gis/metrics/catalogue
  forwardMonths: [],    // forecast months past the recorded window
  contract: null,       // /gis/contract, cached for the vendor panel
  panelHint: {},        // per-panel badge text in the rail
  estatePins: null,     // one point per estate, for the group-altitude pins
  rows: null,           // /gis/blocks/table keyed by block id, for the drawer
  rowsKey: null,        // estate|month the rows were fetched for
  satellite: null,      // the Sentinel-2 scene behind the canopy layer, if any
  features: null,       // /gis/features, the manifest the rail is built from
};

