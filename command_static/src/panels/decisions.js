import { genBadge } from '../ai/store.js';
import { esc } from '../lib/fmt.js';
import { refreshRail } from '../shell/rail.js';
import { S } from '../state/store.js';

/* ── recording a decision ───────────────────────────────────────────────── */

export async function recordDecision(spec, btn) {
  // A window carries its own note beside its buttons; the dock panels use the
  // one #act-note. Scoped first, so a window never writes into a dock panel.
  const scope = btn && btn.closest('[data-act-scope]');
  const scoped = scope && scope.parentElement && scope.parentElement.parentElement
    ? scope.parentElement.parentElement.querySelector('.stub') : null;
  const note = scoped || document.getElementById('act-note');
  if (note) note.textContent = 'Recording…';

  const body = {
    estate: S.estate, use_case: spec.use_case, title: spec.title,
    subject: spec.subject, action: spec.action, evidence: spec.evidence || [],
    artifact_kind: spec.artifact_kind || null, artifact_payload: {},
  };

  // Payloads are fetched at accept time so the artifact carries the same
  // numbers the panel showed, not a stale copy.
  if (spec.artifact_kind === 'purchase_requisition') {
    const v = await (await fetch(`/gis/vendors?shortfall_t=${spec.shortfall_t}`)).json();
    body.artifact_payload = { month: spec.subject, allocation: v.allocation };
  } else if (spec.rotation) {
    const r = await (await fetch('/gis/rotation?estate=EC&top=20')).json();
    body.artifact_payload = { date: 'next round', blocks: r.queue };
  } else if (spec.fire) {
    body.artifact_payload = {
      mobilisation: S.fire.mobilisation,
      blocks: S.fire.exposure.blocks, ha: S.fire.exposure.planted_ha,
    };
  } else if (spec.inspection) {
    body.artifact_payload = spec.inspection;
  } else if (spec.stores) {
    // The store's document, from the same figures the window shows.
    const q = new URLSearchParams({ matnr: spec.stores.matnr, kind: spec.stores.kind });
    body.artifact_payload = await (await fetch(`/gis/stores/document?${q}`)).json();
  } else if (spec.ops_plan) {
    // The plan the Tomorrow tab is showing, edits applied. The artifact is
    // drafted from it, and the decision records when it is due, what the
    // scheduler expected, and the work orders it generates, so the
    // Did-it-work panel can read the ledger back against it.
    const plan = S.ops && S.ops[spec.ops_plan] && S.ops[spec.ops_plan].plan;
    if (plan && plan.available) {
      const t = plan.totals;
      body.artifact_payload = { plan };
      body.due_date = plan.date;
      body.expected_effect = {
        operation: plan.operation, date: plan.date, unit: t.unit,
        qty: t.qty, tonnes: t.tonnes, expected_qty: t.expected_qty,
        expected_tonnes: t.expected_tonnes, value_idr: t.value_idr,
        blocks: t.blocks, ha: t.ha, crews: t.crews_with_work,
        not_reached_blocks: plan.not_reached.blocks,
        block_keys: plan.crews.flatMap(c => c.blocks.map(b => b.block_key)),
        crew_codes: plan.crews.filter(c => c.blocks.length).map(c => c.crew_code),
      };
      // What the forecasts said when the plan was accepted, so Did it work can
      // set each one against what happened.
      const f = plan.forecast || {};
      body.expected_effect.forecast = {
        rain_washoff_pct: f.rain && f.rain.in_use ? f.rain.chances.washoff.pct : null,
        rain_heavy_pct: f.rain && f.rain.in_use ? f.rain.chances.heavy.pct : null,
        present_low: f.headcount && f.headcount.in_use ? f.headcount.low : null,
        present_high: f.headcount && f.headcount.in_use ? f.headcount.high : null,
        present_most_likely: f.headcount && f.headcount.in_use ? f.headcount.most_likely : null,
        done_pct: f.work_done && f.work_done.in_use ? f.work_done.expected_pct : null,
        done_low_pct: f.work_done && f.work_done.in_use ? f.work_done.low_pct : null,
        done_high_pct: f.work_done && f.work_done.in_use ? f.work_done.high_pct : null,
        spray_verdict: f.spray_call ? f.spray_call.verdict : null,
      };
      body.order_ref = plan.order_refs;
    }
  }

  const res = await fetch('/gis/decisions', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  const out = await res.json();
  S.panelHint.audit = `${(await (await fetch('/gis/decisions')).json()).total} logged`;
  if (spec.ops_plan && spec.action === 'accepted') {
    S.panelHint.outcomes = `${(S.panelHint.outcomesCount = (S.panelHint.outcomesCount || 0) + 1)} plans`;
  }
  refreshRail();

  if (btn) btn.disabled = true;
  if (out.artifact) {
    const holder = document.createElement('div');
    holder.innerHTML = renderArtifact(out.artifact);
    if (scoped) {
      scoped.textContent = '';
      scoped.appendChild(holder);
      holder.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    } else {
      (btn ? btn.closest('.acts').parentElement : document.body).appendChild(holder);
    }
  } else if (note) {
    note.textContent = `Recorded: ${spec.action}. Nothing was written to EPMS.`;
  }
}

export function renderArtifact(a) {
  const cols = a.lines.length ? Object.keys(a.lines[0]) : [];
  return `<div class="artifact">
    <div class="artifact-h"><b>${a.label} drafted</b>
      <span class="prov synthetic">${a.status}</span>
      <span class="ref">${a.reference}</span></div>
    <div class="artifact-b">
      <div style="margin-bottom:8px">${a.title} — ${a.summary}</div>
      ${cols.length ? `<table><tr>${cols.map(c => `<th>${c.replace(/_/g, ' ')}</th>`).join('')}</tr>
        ${a.lines.slice(0, 8).map(l => `<tr>${cols.map(c => `<td>${l[c] ?? '—'}</td>`).join('')}</tr>`).join('')}
      </table>${a.lines.length > 8 ? `<div style="margin-top:6px;color:var(--muted)">
        and ${a.lines.length - 8} more lines</div>` : ''}` : ''}
    </div>
    ${a.prose ? `<div class="artifact-b" style="border-top:1px solid var(--border)">
      <div style="margin-bottom:7px">${esc(a.prose.purpose)}</div>
      ${a.prose.instructions && a.prose.instructions.length ? `<ul class="ev">
        ${a.prose.instructions.map(i => `<li>${esc(i)}</li>`).join('')}</ul>` : ''}
      ${a.prose.acceptance ? `<div style="margin-top:7px"><b>Done when.</b>
        ${esc(a.prose.acceptance)}</div>` : ''}
      ${a.prose.caveat ? `<div style="margin-top:7px;color:var(--gold)">
        ${esc(a.prose.caveat)}</div>` : ''}
      <div class="brief-meta" style="padding:7px 0 0">${genBadge(a.prose.model)}
        <span>instruction text only; every line above is computed</span></div>
    </div>` : ''}
    <div class="artifact-route">
      Would enter <b>${a.would_route_to}</b>, creating <b>${a.would_create}</b>,
      for approval by the <b>${a.approver_role}</b>.<br>${a.disclaimer}
    </div>
  </div>`;
}

