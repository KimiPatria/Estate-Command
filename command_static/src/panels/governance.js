import { esc } from '../lib/fmt.js';

export async function panelAudit() {
  const d = await (await fetch('/gis/decisions?limit=100')).json();
  if (!d.total) return `<div class="empty">No decisions recorded yet.
    Accept, reject or defer a recommendation and it lands here.</div>`;
  return `<table class="tbl">
    <tr><th>When</th><th>Action</th><th>Use case</th><th>Subject</th>
        <th>Artifact</th><th>Would route to</th></tr>
    ${d.decisions.map(r => `<tr>
      <td>${r.created_at.slice(0, 16).replace('T', ' ')}</td>
      <td><b>${r.action}</b></td><td>${r.use_case}</td><td>${r.subject}</td>
      <td>${r.artifact ? r.artifact.reference : '—'}</td>
      <td>${r.epms_table || '—'}</td>
    </tr>`).join('')}
  </table>
  <div class="sheet-note">
    ${d.total} recorded: ${Object.entries(d.by_action).map(([k, v]) => `${v} ${k}`).join(', ')}.
  </div>
  <div style="margin-top:16px">
    <button class="gen-btn" id="handover-btn">Write the shift handover</button>
    <div id="handover" style="margin-top:11px"></div>
  </div>`;
}

/* ── transport ──────────────────────────────────────────────────────────── */


/* ── canopy vigour ──────────────────────────────────────────────────────── */
/* This is the strongest real-data panel in the app and it had no renderer at
   all: opening it said "specified and not yet built" while sitting on real
   Sentinel-2 NDRE for 291 of 291 blocks. The scene id and cloud figure are on
   the panel because the point is that this was measured, not modelled, and
   the client supplied none of it. */
export async function panelCanopy() {
  const d = await (await fetch('/gis/vegetation?estate=EC')).json();
  if (!d.available) return `<div class="empty">${esc(d.reason || 'No scene available')}</div>`;

  const s = d.scene, c = d.coverage, n = d.ndre;

  return `
    <div class="kpis">
      <div class="kpi"><b>${c.measured}/${c.blocks}</b><span>blocks measured</span></div>
      <div class="kpi"><b>${n.median}</b><span>median NDRE</span></div>
      <div class="kpi"><b>${n.min} – ${n.max}</b><span>range</span></div>
      <div class="kpi"><b>${s.cloud_cover_pct}%</b><span>scene cloud</span></div>
    </div>

    <div class="sub-t">Weakest canopy</div>
    <table class="tbl">
      <tr><th>Block</th><th class="num">NDRE</th><th class="num">NDVI</th>
          <th class="num">Valid pixels</th></tr>
      ${(d.weakest_blocks || []).map((r, i) => `<tr class="${i < 3 ? 'hi' : ''}">
        <td>${esc(r.block)}</td>
        <td class="num"><b>${r.ndre}</b></td>
        <td class="num">${r.ndvi}</td>
        <td class="num">${r.valid_pct}%</td>
      </tr>`).join('')}
    </table>

    ${(d.clouded_blocks || []).length
      ? `<div class="blk-cap">${d.clouded_blocks.length} block(s) under cloud, no reading.</div>`
      : ''}

    <div class="blk-cap">${esc(s.platform)} · ${esc(s.date)} · ${s.cloud_cover_pct}% cloud</div>`;
}
