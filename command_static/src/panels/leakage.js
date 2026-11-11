/* Where money leaks: the cost window (buildplan_finance.md).
 *
 * Opens on the ten places the estate lost the most money this month, each
 * with the rule-based why and do, and the model's likely cause once it is
 * ready. Every figure comes from the server (gis/finance.py, gis/diagnosis.py);
 * this module only lays them out.
 *
 *   Now     Top leaks · At a glance · Actions
 *   Nodes   one section per function, and the open unit
 *   Ledger  Postings · Budget vs actual
 *   Trust   Planted against found · Against budget variance · Diagnosis accuracy · Benchmarks · How it works
 */
import { auditChip, genBadge } from '../ai/store.js';
import { getJSON } from '../lib/api.js';
import { esc, fmt } from '../lib/fmt.js';
import { S } from '../state/store.js';
import { recordDecision } from './decisions.js';
import { mark, tbl } from './forecasts.js';
import { rp } from './stores.js';

const FUNCTIONS = [
  ['harvesting', 'Harvesting'], ['transport', 'FFB transport'], ['fleet', 'Fleet maintenance'],
  ['fertiliser', 'Fertiliser'], ['upkeep', 'Field upkeep'], ['pest', 'Pest & disease'],
  ['workforce', 'Workforce'], ['stores', 'Stores'], ['roads', 'Roads'], ['overhead', 'Overhead'],
];
const FN_LABEL = Object.fromEntries(FUNCTIONS);
const n0 = v => (v === null || v === undefined) ? '—' : fmt(v, 0);
const pc = v => (v === null || v === undefined) ? '—' : `${fmt(v, 0)}%`;
const q = s => JSON.stringify(s).replace(/'/g, '&#39;');

function effBar(p) {
  if (p === null || p === undefined) return '<span class="fp-dim">—</span>';
  const w = Math.max(0, Math.min(100, p));
  const tone = w >= 90 ? 'ok' : w >= 75 ? 'mid' : 'lo';
  return `<span class="lk-eff ${tone}" title="${fmt(p, 1)}% efficiency"><i style="width:${w}%"></i><b>${fmt(p, 0)}%</b></span>`;
}

function decideSpec(row, month, action) {
  return q({
    use_case: 'Cost: action', title: `${row.label}: ${(action && action.text) || (row.do && row.do.text) || row.driver_label}`,
    subject: row.id, action: 'accepted', artifact_kind: 'cost_action',
    cost: { id: row.id, month, action: action ? action.id : (row.do && row.do.id) },
    evidence: [row.why, (action && action.text) || (row.do && row.do.text)].filter(Boolean),
  });
}

/* ── small charts ─────────────────────────────────────────────────────── */

function trendChart(trend) {
  if (!trend || !trend.length) return '';
  const W = 560, H = 150, L = 56, R = 46, T = 14, B = 24;
  const max = Math.max(1, ...trend.map(t => t.leakage_idr || 0));
  const bw = (W - L - R) / trend.length;
  const y = v => T + (H - T - B) * (1 - v / max);
  const ey = v => T + (H - T - B) * (1 - (v - 50) / 50);
  const bars = trend.map((t, i) => {
    const h = (H - T - B) * (t.leakage_idr || 0) / max;
    return `<rect class="lk-bar${t.partial ? ' partial' : ''}" x="${L + i * bw + bw * 0.2}" y="${H - B - h}" width="${bw * 0.6}" height="${h}">
      <title>${esc(t.label)}: ${esc(rp(t.leakage_idr))} leaked, ${pc(t.efficiency_pct)} efficiency${t.partial ? ' (to the 23rd)' : ''}</title></rect>
      <text class="lk-ax" x="${L + i * bw + bw / 2}" y="${H - 6}" text-anchor="middle">${esc(t.label)}</text>`;
  }).join('');
  const pts = trend.filter(t => t.efficiency_pct !== null).map((t, i) =>
    `${L + trend.indexOf(t) * bw + bw / 2},${ey(Math.max(50, t.efficiency_pct))}`).join(' ');
  return `<svg class="lk-chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="Leakage and efficiency by month">
    <text class="lk-ax" x="${L - 6}" y="${y(max) + 4}" text-anchor="end">${esc(rp(max))}</text>
    <text class="lk-ax" x="${L - 6}" y="${H - B}" text-anchor="end">0</text>
    ${bars}
    <polyline class="lk-line" points="${pts}"/>
    <text class="lk-ax eff" x="${W - R + 6}" y="${ey(100) + 4}">100%</text>
    <text class="lk-ax eff" x="${W - R + 6}" y="${ey(50) + 4}">50%</text>
  </svg>
  <div class="lk-legend"><span><i class="sw bar"></i>leaked</span><span><i class="sw line"></i>efficiency</span></div>`;
}

/* From necessary cost to what was spent and lost, one step per driver. */
function waterfall(n) {
  const steps = (n.drivers || []).filter(d => Math.abs(d.idr) >= 1);
  const total = (n.spend_idr || 0) + (n.lost_idr || 0);
  const bench = total - steps.reduce((a, d) => a + d.idr, 0);
  const pts = [{ label: 'Benchmark', from: 0, to: bench, kind: 'base' }];
  let run = bench;
  steps.forEach(d => { pts.push({ label: d.label, from: run, to: run + d.idr, kind: d.idr >= 0 ? 'up' : 'down', idr: d.idr }); run += d.idr; });
  pts.push({ label: 'Spent + lost', from: 0, to: total, kind: 'total' });
  const max = Math.max(1, ...pts.map(p => Math.max(p.from, p.to)));
  const W = 620, rowH = 26, L = 150, R = 96;
  const x = v => L + (W - L - R) * v / max;
  return `<svg class="lk-wf" viewBox="0 0 ${W} ${pts.length * rowH + 6}" role="img" aria-label="From the benchmark to what was spent and lost">
    ${pts.map((p, i) => {
      const a = Math.min(p.from, p.to), b = Math.max(p.from, p.to);
      const val = p.kind === 'up' || p.kind === 'down' ? `${p.idr > 0 ? '+' : '−'}${rp(Math.abs(p.idr))}` : rp(p.to);
      return `<text class="lk-wl" x="${L - 8}" y="${i * rowH + 17}" text-anchor="end">${esc(p.label)}</text>
        <rect class="lk-wb ${p.kind}" x="${x(a)}" y="${i * rowH + 5}" width="${Math.max(1.5, x(b) - x(a))}" height="${rowH - 10}"/>
        <text class="lk-wv ${p.kind}" x="${x(b) + 6}" y="${i * rowH + 17}">${esc(val)}</text>`;
    }).join('')}
  </svg>`;
}

/* The unit's rate among its peers, with the benchmark marked. */
function peerStrip(n) {
  const p = n.peer || {};
  const vals = p.values || [];
  if (vals.length < 3) return '';
  const mine = n.facts && (n.facts.rate_t || null);
  const lo = Math.min(...vals, mine || Infinity), hi = Math.max(...vals, mine || 0);
  const W = 560, H = 54, L = 12, R = 12;
  const x = v => L + (W - L - R) * (v - lo) / Math.max(1, hi - lo);
  return `<div class="sub-t">Among its ${vals.length} peers <span class="fp-dim">${esc(p.basis || '')}</span></div>
    <svg class="lk-peer" viewBox="0 0 ${W} ${H}" role="img" aria-label="Peers">
      <line class="axis" x1="${L}" x2="${W - R}" y1="28" y2="28"/>
      ${vals.map(v => `<circle class="pt" cx="${x(v)}" cy="28" r="3.5"><title>${esc(n0(v))}</title></circle>`).join('')}
      ${p.rate ? `<line class="bench" x1="${x(p.rate)}" x2="${x(p.rate)}" y1="12" y2="44"/><text class="lk-ax" x="${x(p.rate)}" y="10" text-anchor="middle">best quarter ${esc(n0(p.rate))}</text>` : ''}
      ${mine ? `<circle class="me" cx="${x(mine)}" cy="28" r="6"><title>This unit: ${esc(n0(mine))}</title></circle><text class="lk-ax me" x="${x(mine)}" y="52" text-anchor="middle">this one ${esc(n0(mine))}</text>` : ''}
    </svg>`;
}

/* ── the diagnosis card ──────────────────────────────────────────────── */

export function diagnosisCard(d, row, month) {
  if (!d) return '<div class="lk-diag wait"><div class="fp-card-t">Likely cause</div><div class="empty">Weighing the checks…</div></div>';
  if (!d.available) {
    const t = d.template || {};
    return `<div class="lk-diag off">
      <div class="fp-card-t">Likely cause</div>
      <p class="fp-dim">${esc(d.reason || 'No diagnosis.')}</p>
      ${t.why ? `<p><b>Why</b> ${esc(t.why)}</p>` : ''}
      ${t.do ? `<p><b>Do</b> ${esc(t.do.text)} <span class="fp-dim">${esc(t.do.owner)}</span></p>` : ''}
    </div>`;
  }
  const g = d.diagnosis;
  const c = g.cause;
  const chip = e => `<span class="chip lk-ev" data-check="${esc(e.id)}" title="Show the check">${esc(e.label)}</span>`;
  return `<div class="lk-diag">
    <div class="lk-diag-h"><div class="fp-card-t">Likely cause</div>
      <span class="lk-conf ${esc(c.confidence)}">${esc(c.confidence)}</span>${c.vetted ? '' : '<span class="chip">outside the list</span>'}
      <span class="grow"></span>${genBadge(d.model && d.model.model)}${auditChip(d.audit)}</div>
    <div class="lk-headline">${esc(g.headline)}</div>
    <p>${esc(c.sentence)}</p>
    ${c.evidence.length ? `<div class="lk-evs">${c.evidence.map(chip).join('')}</div>` : ''}
    ${g.ruled_out.length ? `<div class="sub-t">Ruled out</div><ul class="lk-ruled">${g.ruled_out.map(r =>
      `<li><b>${esc(r.label)}</b> ${esc(r.sentence)}</li>`).join('')}</ul>` : ''}
    <div class="sub-t">To confirm</div><p>${esc(g.confirm)}</p>
    <div class="sub-t">Do</div>
    <div class="lk-acts">${g.actions.map(a => `<div class="lk-act">
        <div><b>${esc(a.text)}</b><div class="fp-dim">${esc(a.owner)} · ${esc(a.when)}${a.saving_idr_month ? ` · up to ${esc(rp(a.saving_idr_month))} a month` : ''}${a.document ? ` · ${esc(a.document)}` : ''}</div></div>
        <span class="acts" data-act-scope><button class="accept" data-decide='${decideSpec(row, month, a)}'>Draft action</button></span>
        <div class="stub"></div>
      </div>`).join('')}</div>
  </div>`;
}

function checksBlock(checks) {
  if (!checks || !checks.length) return '';
  return `<div class="sub-t">What the records show</div>
    <ul class="lk-checks">${checks.map(c => `<li data-check-id="${esc(c.id)}"><b>${esc(c.label)}</b> ${esc(c.finding)}</li>`).join('')}</ul>`;
}

/* ── the window ───────────────────────────────────────────────────────── */

export function leakagePopup() {
  S.leak = S.leak || { section: 'top', month: null, unit: null, unitLabel: '', account: '', ledgerUnit: false };
  const F = S.leak;
  let O = null;
  const cache = {};
  const fetchView = async (key, url) => {
    if (!cache[key]) cache[key] = await getJSON(url);
    return cache[key];
  };
  const m = () => F.month || (O && O.month) || '';

  return {
    key: 'leakage',
    initial: F.section,
    loadingText: 'Pricing every block, vehicle, crew and material… the first time after a restart takes about twenty seconds.',
    async load() {
      Object.keys(cache).forEach(k => delete cache[k]);
      O = await getJSON(`/gis/finance/summary${F.month ? `?month=${F.month}` : ''}`);
      if (O.available) F.month = O.month;
    },
    header: () => O && O.available ? `
      <div class="fc-chips lk-months" role="group" aria-label="Month">${O.months.map(x =>
        `<button class="${x.month === O.month ? 'on' : ''}" data-month="${esc(x.month)}" aria-pressed="${x.month === O.month}"
          title="${esc(x.label)}">${esc(x.short)}</button>`).join('')}</div>
      <span class="prov synthetic" title="Generated SAP FI/CO records, posted from the generated operations ledgers">generated SAP records</span>` : '',
    wireHeader(el, api) {
      el.querySelectorAll('[data-month]').forEach(b => {
        b.onclick = () => { F.month = b.dataset.month; api.reload(); };
      });
    },
    menu: () => {
      if (!O || !O.available) return [];
      const f = Object.fromEntries(O.functions.map(x => [x.function, x]));
      return [
        { label: 'Now', items: [
          { id: 'top', label: 'Top leaks', count: O.top.length },
          { id: 'glance', label: 'At a glance' },
          { id: 'actions', label: 'Actions' },
        ] },
        { label: 'Nodes', items: [
          ...FUNCTIONS.filter(([k]) => f[k]).map(([k, l]) => ({
            id: `f:${k}`, label: l, count: f[k].scored ? rp(f[k].leakage_idr) : '', alert: f[k].flagged > 0 })),
          ...(F.unit ? [{ id: 'unit', label: `▸ ${F.unitLabel}` }] : []),
        ] },
        { label: 'Ledger', items: [
          { id: 'postings', label: 'Postings' },
          { id: 'budget', label: 'Budget vs actual' },
        ] },
        { label: 'Trust', items: [
          { id: 'planted', label: 'Planted against found' },
          { id: 'compare', label: 'Against budget variance' },
          { id: 'evals', label: 'Diagnosis accuracy' },
          { id: 'bench', label: 'Benchmarks' },
          { id: 'how', label: 'How it works' },
        ] },
      ];
    },
    onSection: id => { F.section = id; },
    async section(id) {
      if (!O.available) return { title: 'Where money leaks', html: `<div class="empty">${esc(O.reason)}</div>` };
      if (id === 'glance') return secGlance(O);
      if (id === 'actions') return secActions(await getJSON('/gis/finance/outcomes'));
      if (id.startsWith('f:')) {
        const fn = id.slice(2);
        return secFunction(await fetchView(`f:${fn}:${m()}`, `/gis/finance/nodes?function=${fn}&month=${m()}`), m());
      }
      if (id === 'unit' && F.unit) {
        return secUnit(await fetchView(`u:${F.unit}:${m()}`, `/gis/finance/node?id=${encodeURIComponent(F.unit)}&month=${m()}`), m());
      }
      if (id === 'postings') {
        const p = new URLSearchParams({ limit: '150' });
        if (F.account) p.set('account', F.account);
        if (F.ledgerUnit && F.unit) p.set('unit', F.unit);
        return secPostings(await fetchView(`l:${p}`, `/gis/finance/ledger?${p}`), F);
      }
      if (id === 'budget') return secBudget(await fetchView(`b:${m()}`, `/gis/finance/budget?month=${m()}`));
      if (['planted', 'compare', 'evals', 'bench', 'how'].includes(id)) {
        const A = await fetchView('acc', '/gis/finance/accuracy');
        if (id === 'planted') return secPlanted(A);
        if (id === 'compare') return secCompare(A);
        if (id === 'evals') return secEvals(A);
        if (id === 'bench') return secBench(A);
        return secHow(A);
      }
      return secTop(O);
    },
    wire(root, api, id) {
      const open = (uid, label) => {
        F.unit = uid;
        F.unitLabel = label;
        if (api.section() === 'unit') api.rerender(); else api.go('unit');
      };
      root.querySelectorAll('[data-unit]').forEach(b => {
        b.onclick = ev => {
          if (ev.target.closest('button[data-decide], [data-map]:not(tr), a')) return;
          open(b.dataset.unit, b.dataset.unitLabel || b.dataset.unit);
        };
      });
      root.querySelectorAll('[data-go]').forEach(b => { b.onclick = ev => { ev.stopPropagation(); api.go(b.dataset.go); }; });
      root.querySelectorAll('[data-decide]').forEach(b => {
        b.onclick = ev => { ev.stopPropagation(); recordDecision(JSON.parse(b.dataset.decide), b); };
      });
      root.querySelectorAll('[data-account]').forEach(b => { b.onclick = () => { F.account = b.dataset.account; api.rerender(); }; });
      root.querySelectorAll('[data-ledger-unit]').forEach(b => {
        b.onclick = () => { F.ledgerUnit = b.dataset.ledgerUnit === '1'; if (api.section() === 'postings') api.rerender(); else api.go('postings'); };
      });
      root.querySelectorAll('[data-close]').forEach(f => {
        f.onsubmit = async ev => {
          ev.preventDefault();
          const fd = new FormData(f);
          f.querySelector('button').disabled = true;
          await fetch('/gis/finance/actions/close', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ decision_id: f.dataset.close, cause_found: fd.get('cause'), note: fd.get('note') || null }),
          });
          api.rerender();
        };
      });
      // The unit's diagnosis arrives after the figures: a cold one takes a few seconds.
      const slot = root.querySelector('[data-diag]');
      if (slot && id === 'unit') {
        const uid = slot.dataset.diag;
        const row = JSON.parse(slot.dataset.row);
        const key = `d:${uid}:${m()}`;
        const paint = d => {
          if (F.unit !== uid || api.section() !== 'unit') return;
          slot.innerHTML = diagnosisCard(d, row, m());
          const cs = root.querySelector('[data-checks-slot]');
          if (cs && d.checks) cs.innerHTML = checksBlock(d.checks);
          else if (cs) getJSON(`/gis/finance/checks?id=${encodeURIComponent(uid)}&month=${m()}`)
            .then(c => { if (c.available) cs.innerHTML = checksBlock(c.checks); }).catch(() => {});
          slot.querySelectorAll('[data-decide]').forEach(b => {
            b.onclick = ev => { ev.stopPropagation(); recordDecision(JSON.parse(b.dataset.decide), b); };
          });
          slot.querySelectorAll('[data-check]').forEach(c => {
            c.onclick = () => {
              const li = root.querySelector(`[data-check-id="${c.dataset.check}"]`);
              if (li) { li.classList.add('hi'); li.scrollIntoView({ behavior: 'smooth', block: 'center' }); setTimeout(() => li.classList.remove('hi'), 1800); }
            };
          });
        };
        if (cache[key]) paint(cache[key]);
        else {
          getJSON(`/gis/finance/diagnosis?id=${encodeURIComponent(uid)}&month=${m()}`)
            .then(d => { cache[key] = d; paint(d); })
            .catch(e => paint({ available: false, reason: `The diagnosis could not be fetched: ${e.message}` }));
        }
      }
    },
  };
}

/* ── sections ─────────────────────────────────────────────────────────── */

function leakRow(r, month) {
  const why = r.diagnosis_headline || r.why;
  const doTxt = r.do ? `${r.do.text} <span class="fp-dim">${esc(r.do.owner)}</span>` : '';
  return `<tr data-unit="${esc(r.id)}" data-unit-label="${esc(r.label)}" title="Open ${esc(r.label)}">
    <td class="num lk-rank">${r.rank}</td>
    <td><b>${esc(r.label)}</b><div class="fp-dim">${esc(r.function_label)}</div></td>
    <td class="num lk-rp">${esc(rp(r.leakage_idr))}</td>
    <td class="lk-why">${r.diagnosis_headline ? '<span class="lk-ai" title="The model\'s reading">AI</span>' : ''}${esc(why)}</td>
    <td class="lk-do">${doTxt}</td>
    <td class="lk-row-acts">
      ${r.blocks && r.blocks.length ? `<button class="ops-btn" data-map="${esc(r.blocks.join(','))}" data-map-caption="${esc(r.label)}">Map</button>` : ''}
      <span class="acts" data-act-scope><button class="accept" data-decide='${decideSpec(r, month)}'>Draft action</button></span><div class="stub"></div>
    </td></tr>`;
}

function secTop(O) {
  const e = O.estate;
  return {
    title: `Top leaks, ${O.month_label}`,
    html: `
      <div class="fc-hero lk-hero">
        <div class="fc-hero-n">${esc(rp(e.leakage_idr))}</div>
        <div class="fc-hero-t"><p>${esc(O.headline)}</p></div>
      </div>
      <div class="kpis">
        <div class="kpi"><b>${pc(e.efficiency_pct)}</b><span>estate efficiency</span></div>
        <div class="kpi ${e.deferred_idr ? 'warn' : ''}"><b>${esc(rp(e.deferred_idr))}</b><span>deferred</span></div>
        <div class="kpi"><b>${esc(rp(e.spend_idr))}</b><span>spent</span></div>
        <div class="kpi"><b>${n0(O.leaks_listed)}</b><span>leaks over ${esc(rp(O.materiality_idr))}</span></div>
      </div>
      ${O.top.length ? tbl(`<tr><th class="num">#</th><th>Where</th><th class="num">Leak</th><th>Why</th><th>Do</th><th></th></tr>
        ${O.top.map(r => leakRow(r, O.month)).join('')}`) : '<div class="empty">No leak over the materiality line this month.</div>'}`,
    blocks: [...new Set(O.top.flatMap(r => r.blocks || []))],
    blocksCaption: `Top leaks, ${O.month_label}`,
  };
}

function secGlance(O) {
  const e = O.estate;
  return {
    title: `At a glance, ${O.month_label}`,
    html: `
      <div class="kpis">
        <div class="kpi"><b>${esc(rp(e.leakage_idr))}</b><span>leaked</span></div>
        <div class="kpi"><b>${pc(e.efficiency_pct)}</b><span>efficiency</span></div>
        <div class="kpi ${e.deferred_idr ? 'warn' : ''}"><b>${esc(rp(e.deferred_idr))}</b><span>deferred</span></div>
        <div class="kpi"><b>${esc(rp(e.lost_idr))}</b><span>crop lost</span></div>
        <div class="kpi"><b>${esc(rp(e.overhead_idr))}</b><span>overhead, not scored</span></div>
      </div>
      <div class="sub-t">By month</div>
      ${trendChart(O.trend)}
      <div class="sub-t">By function</div>
      ${tbl(`<tr><th>Function</th><th class="num">Spent</th><th class="num">Crop lost</th><th class="num">Necessary</th>
          <th class="num">Leaked</th><th>Efficiency</th><th class="num">Deferred</th><th class="num">Units over the line</th></tr>
        ${O.functions.map(f => `<tr class="fp-row-map" data-go="f:${esc(f.function)}" role="button" tabindex="0">
          <td><b>${esc(f.label)}</b>${f.rolls_up_as === 'variance' ? ' <span class="fp-dim" title="The material itself is in the blocks at standard">variance only</span>' : ''}</td>
          <td class="num">${esc(rp(f.spend_idr))}</td><td class="num">${esc(rp(f.lost_idr))}</td>
          <td class="num">${esc(rp(f.necessary_idr))}</td><td class="num"><b>${f.scored ? esc(rp(f.leakage_idr)) : '—'}</b></td>
          <td>${f.scored ? effBar(f.efficiency_pct) : '<span class="fp-dim">not scored</span>'}</td>
          <td class="num">${f.deferred_idr ? esc(rp(f.deferred_idr)) : '—'}</td>
          <td class="num">${f.scored ? `${f.flagged} of ${f.units}` : '—'}</td></tr>`).join('')}`)}`,
  };
}

function secFunction(N, month) {
  if (!N.available) return { title: 'Function', html: `<div class="empty">${esc(N.reason)}</div>` };
  const t = N.totals || {};
  const active = N.rows.filter(r => r.leakage_idr > 0);
  const quiet = N.rows.length - active.length;
  const drivers = (N.drivers || []).filter(d => d.idr > 0);
  const dmax = Math.max(1, ...drivers.map(d => d.idr));
  const scored = N.function !== 'overhead';
  return {
    title: `${N.label}, ${N.month_label}`,
    html: `
      <div class="kpis">
        <div class="kpi"><b>${scored ? esc(rp(t.leakage_idr)) : '—'}</b><span>leaked</span></div>
        <div class="kpi"><b>${scored ? pc(t.efficiency_pct) : '—'}</b><span>efficiency</span></div>
        <div class="kpi"><b>${esc(rp(t.spend_idr))}</b><span>spent</span></div>
        ${t.lost_idr ? `<div class="kpi"><b>${esc(rp(t.lost_idr))}</b><span>crop lost</span></div>` : ''}
        ${t.deferred_idr ? `<div class="kpi warn"><b>${esc(rp(t.deferred_idr))}</b><span>deferred</span></div>` : ''}
      </div>
      ${scored && drivers.length ? `<div class="sub-t">Where it goes</div>
        <div class="lk-drivers">${drivers.map(d => `<div class="lk-drv"><span class="l">${esc(d.label)}</span>
          <span class="b"><i style="width:${100 * d.idr / dmax}%"></i></span><span class="v">${esc(rp(d.idr))}</span></div>`).join('')}</div>` : ''}
      ${N.groups && N.groups.length ? `<div class="sub-t">Divisions that stand out</div>
        ${tbl(`<tr><th>Division</th><th class="num">Leak</th><th>Why</th><th></th></tr>${N.groups.map(g => `
          <tr data-unit="${esc(g.id)}" data-unit-label="${esc(g.label)}"><td><b>${esc(g.label)}</b></td><td class="num">${esc(rp(g.leakage_idr))}</td>
          <td>${esc(g.why)}</td><td>${g.blocks.length ? `<button class="ops-btn" data-map="${esc(g.blocks.join(','))}" data-map-caption="${esc(g.label)}">Map</button>` : ''}</td></tr>`).join('')}`)}` : ''}
      <div class="sub-t">${scored ? 'Units, largest leak first' : 'Divisions'}</div>
      ${tbl(`<tr><th>Unit</th><th class="num">Spent</th><th class="num">Necessary</th><th class="num">Leaked</th><th>Efficiency</th>
          <th class="num">Deferred</th><th>Largest driver</th></tr>
        ${(scored ? active.slice(0, 80) : N.rows).map(r => `<tr class="fp-row-map" data-unit="${esc(r.id)}" data-unit-label="${esc(r.label)}">
          <td><b>${esc(r.label)}</b>${r.division && N.unit !== 'division' ? ` <span class="fp-dim">Div ${esc(r.division)}</span>` : ''}</td>
          <td class="num">${esc(rp(r.spend_idr))}</td><td class="num">${esc(rp(r.necessary_idr))}</td>
          <td class="num"><b>${scored ? esc(rp(r.leakage_idr)) : '—'}</b></td>
          <td>${scored ? effBar(r.efficiency_pct) : '—'}</td>
          <td class="num">${r.deferred_idr ? esc(rp(r.deferred_idr)) : '—'}</td>
          <td class="fc-why">${r.driver ? esc(r.why) : '<span class="fp-dim">at benchmark</span>'}</td></tr>`).join('')}`)}
      ${scored && quiet ? `<p class="fp-dim lk-quiet">${quiet} more unit${quiet === 1 ? '' : 's'} at or better than benchmark.</p>` : ''}`,
    blocks: [...new Set(active.flatMap(r => r.blocks || []))].slice(0, 300),
    blocksCaption: `${N.label}: leaking units`,
  };
}

function secUnit(n, month) {
  if (!n.available) return { title: 'Unit', html: `<div class="empty">${esc(n.reason)}</div>` };
  if (!n.active) {
    return { title: n.label, html: `<div class="fc-summary">${esc(n.sentence)}</div>${trendChart((n.trend || []).map(t => ({ ...t, partial: false })))}` };
  }
  const rowForDecide = { id: n.id, label: n.label, why: n.why, do: n.do, driver_label: n.driver_label };
  const members = n.members || [];
  return {
    title: `${n.label}, ${n.month_label}`,
    html: `
      <div class="fc-hero lk-hero">
        <div class="fc-hero-n">${esc(rp(n.leakage_idr))}</div>
        <div class="fc-hero-t"><p>${esc(n.sentence)}</p>
          <div class="kpis lk-mini">
            <div class="kpi"><b>${pc(n.efficiency_pct)}</b><span>efficiency</span></div>
            <div class="kpi"><b>${esc(rp(n.spend_idr))}</b><span>spent</span></div>
            ${n.lost_idr ? `<div class="kpi"><b>${esc(rp(n.lost_idr))}</b><span>crop lost</span></div>` : ''}
            <div class="kpi"><b>${esc(rp(n.necessary_idr))}</b><span>necessary</span></div>
            ${n.deferred_idr ? `<div class="kpi warn"><b>${esc(rp(n.deferred_idr))}</b><span>deferred</span></div>` : ''}
          </div></div>
      </div>
      <div class="lk-unit-grid">
        <div>
          <div class="sub-t">From benchmark to actual</div>
          ${waterfall(n)}
          ${peerStrip(n)}
          <div class="sub-t">By month</div>
          ${trendChart((n.trend || []).map(t => ({ ...t, partial: false })))}
        </div>
        <div data-diag="${esc(n.id)}" data-row='${q(rowForDecide)}'>${diagnosisCard(null)}</div>
      </div>
      <div data-checks-slot></div>
      ${members.length ? `<div class="sub-t">Its ${members.length} units</div>${tbl(`<tr><th>Unit</th><th class="num">Leak</th></tr>
        ${members.map(x => `<tr data-unit="${esc(x.id)}" data-unit-label="${esc(x.label)}"><td>${esc(x.label)}</td><td class="num">${esc(rp(x.leakage_idr))}</td></tr>`).join('')}`)}` : ''}
      ${n.documents && n.documents.length ? `<div class="sub-t">Postings <span class="fp-dim">${n.documents_total} in all</span>
          <button class="ops-btn lk-inline" data-ledger-unit="1">Open in the ledger</button></div>
        ${tbl(`<tr><th>Document</th><th>Date</th><th>Account</th><th class="num">IDR</th><th class="num">Qty</th><th>Source</th></tr>
          ${n.documents.slice(0, 25).map(p => `<tr><td>${esc(p.belnr)}</td><td>${esc(p.budat)}</td><td>${esc(p.account)}</td>
            <td class="num">${n0(p.wsl)}</td><td class="num">${p.quantity ? `${fmt(p.quantity, 1)} ${esc(p.unit)}` : ''}</td>
            <td><span class="fp-dim">${esc(p.src_type)}</span> ${esc(p.src_id)}</td></tr>`).join('')}`)}` : ''}`,
    blocks: n.blocks || [],
    blocksCaption: n.label,
  };
}

function secActions(D) {
  const rows = D.actions || [];
  return {
    title: 'Actions drafted',
    html: rows.length ? rows.map(a => `<div class="fp-card lk-action">
        <div class="lk-diag-h"><div class="fp-card-t">${esc(a.title)}</div><span class="grow"></span>
          <span class="fp-dim">${esc(a.reference || '')} · drafted on ${esc(a.month)}</span></div>
        <p>${esc(a.driver_label)}: ${esc(rp(a.before_idr))} when drafted. ${esc(a.words)}</p>
        <div class="lk-after">${a.after.map(x => `<span class="chip">${esc(x.label)} ${esc(rp(x.driver_idr))}</span>`).join('')}</div>
        ${a.closed ? `<p><b>Cause found:</b> ${esc(a.cause_found)}</p>` : `
          <form class="lk-close" data-close="${esc(a.decision_id)}">
            <label>Cause found <select name="cause">${a.causes.map(c => `<option value="${esc(c.label)}">${esc(c.label)}</option>`).join('')}
              <option value="Something else">Something else</option></select></label>
            <input name="note" placeholder="Note (optional)" maxlength="200">
            <button class="ops-btn primary" type="submit">Close the action</button>
          </form>`}
      </div>`).join('') : '<div class="empty">No cost action drafted yet. Draft one from a leak.</div>',
  };
}

function secPostings(L, F) {
  const groups = [['', 'All'], ['6101000,6101100,6101200', 'Harvest'], ['6102000,6202000', 'Upkeep'],
    ['6103000,6202100', 'Pest'], ['6104000,6201000', 'Fertiliser'], ['6105000,6106000', 'Labour'],
    ['6203000', 'Diesel'], ['6204000,6301000', 'Fleet'], ['6302000', 'Roads'], ['6501000,6502000', 'Store variance']];
  return {
    title: 'Postings',
    html: `
      <div class="fc-chips" role="group">${groups.map(([v, l]) => `<button class="${F.account === v ? 'on' : ''}" data-account="${esc(v)}">${esc(l)}</button>`).join('')}</div>
      ${F.unit ? `<div class="fc-chips" role="group"><button class="${F.ledgerUnit ? '' : 'on'}" data-ledger-unit="0">Whole estate</button>
        <button class="${F.ledgerUnit ? 'on' : ''}" data-ledger-unit="1">${esc(F.unitLabel)}</button></div>` : ''}
      <p class="fp-dim">${n0(L.total)} postings, ${esc(rp(L.total_idr))}; newest 150 shown.</p>
      ${tbl(`<tr><th>Document</th><th>Date</th><th>Account</th><th>Cost centre</th><th>Order</th><th>Object</th>
          <th class="num">IDR</th><th class="num">Qty</th><th>Source</th></tr>
        ${L.rows.map(p => `<tr><td>${esc(p.belnr)}</td><td>${esc(p.budat)}</td><td>${esc(p.account)}</td><td>${esc(p.rcntr)}</td>
          <td>${esc(p.aufnr)}</td><td>${esc(p.vehicle_id || p.driver_id || p.crew_code || p.matnr || p.segment_id || '')}</td>
          <td class="num">${n0(p.wsl)}</td><td class="num">${p.quantity !== '' && p.quantity !== null ? `${fmt(p.quantity, 1)} ${esc(p.unit)}` : ''}</td>
          <td><span class="fp-dim">${esc(p.src_type)}</span> ${esc(p.src_id)}</td></tr>`).join('')}`)}`,
  };
}

function secBudget(B) {
  return {
    title: `Budget vs actual, ${B.month_label}`,
    html: `
      <div class="fc-summary">${n0(B.flagged_total)} of ${n0(B.lines)} budget lines are ${esc(B.rule)}.</div>
      ${tbl(`<tr><th>Cost-element group</th><th class="num">Budget</th><th class="num">Actual</th><th class="num">Variance</th><th class="num">Lines over</th></tr>
        ${B.groups.map(g => `<tr><td><b>${esc(g.group.toLowerCase())}</b></td><td class="num">${esc(rp(g.budget_idr))}</td>
          <td class="num">${esc(rp(g.actual_idr))}</td><td class="num ${g.variance_idr > 0 ? 'neg' : 'pos'}">${esc(rp(g.variance_idr))}</td>
          <td class="num">${g.flagged} of ${g.lines}</td></tr>`).join('')}`)}
      <div class="sub-t">Over budget</div>
      ${B.flagged.length ? tbl(`<tr><th>Object</th><th>Group</th><th class="num">Budget</th><th class="num">Actual</th><th class="num">Over</th></tr>
        ${B.flagged.slice(0, 60).map(l => `<tr><td>${esc(l.object)}</td><td>${esc(l.group.toLowerCase())}</td><td class="num">${esc(rp(l.budget_idr))}</td>
          <td class="num">${esc(rp(l.actual_idr))}</td><td class="num">${l.variance_pct === null ? 'no budget' : `${fmt(l.variance_pct, 0)}%`}</td></tr>`).join('')}`)
        : '<div class="empty">Nothing over budget this month.</div>'}`,
  };
}

function secPlanted(A) {
  const rows = A.checks;
  return {
    title: `Planted against found: ${A.passed} of ${A.total}`,
    html: tbl(`<tr><th></th><th>Check</th><th>Kind</th><th>Must</th><th>Got</th></tr>
      ${rows.map(c => `<tr><td>${mark(c.passes)}</td><td><b>${esc(c.id.replace(/_/g, ' '))}</b><div class="fp-dim">${esc(FN_LABEL[c.function] || c.function)}${c.unit ? ` · ${esc(c.unit)}` : ''}</div></td>
        <td>${esc(c.kind)}</td><td>${esc(c.want)}</td><td>${esc(c.got)}</td></tr>`).join('')}`),
  };
}

function secCompare(A) {
  const c = A.comparison;
  const yes = v => v ? '<span class="fc-mark ok">✓</span>' : '<span class="fc-mark no">✗</span>';
  const alarm = v => v ? '<span class="fc-mark no">raised</span>' : '<span class="fp-dim">none</span>';
  return {
    title: 'Against budget variance',
    html: `
      <div class="fp-grid3">
        <div class="fp-card"><div class="fp-card-t">Planted leaks found</div>
          <div class="fp-big">${c.found.leak_engine} <span class="fp-dim">of ${c.found.of}</span></div>
          <div class="fp-dim">budget variance found ${c.found.budget_variance}</div></div>
        <div class="fp-card"><div class="fp-card-t">False alarms on the decoys</div>
          <div class="fp-big">${c.false_alarms.leak_engine} <span class="fp-dim">of ${c.false_alarms.of}</span></div>
          <div class="fp-dim">budget variance raised ${c.false_alarms.budget_variance}</div></div>
        <div class="fp-card"><div class="fp-card-t">Planted rupiah located to the unit</div>
          <div class="fp-big">${esc(rp(c.located_idr.leak_engine))}</div>
          <div class="fp-dim">of ${esc(rp(c.located_idr.planted))}; budget variance ${esc(rp(c.located_idr.budget_variance))}</div></div>
      </div>
      ${tbl(`<tr><th>Planted leak</th><th>Function</th><th class="num">Planted</th><th>Budget variance</th><th>Leak engine</th></tr>
        ${c.planted.map(r => `<tr><td><b>${esc(r.id.replace(/_/g, ' '))}</b> <span class="fp-dim">${esc(r.unit)}</span></td><td>${esc(FN_LABEL[r.function] || r.function)}</td>
          <td class="num">${r.planted_idr ? esc(rp(r.planted_idr)) : '—'}</td><td>${yes(r.budget_variance)}</td><td>${yes(r.leak_engine)}</td></tr>`).join('')}`)}
      ${tbl(`<tr><th>Decoy</th><th>Budget variance</th><th>Leak engine</th><th></th></tr>
        ${c.decoys.map(r => `<tr><td><b>${esc(r.id.replace(/^decoy_/, '').replace(/_/g, ' '))}</b></td><td>${alarm(r.budget_variance)}</td>
          <td>${alarm(r.leak_engine)}</td><td class="fp-dim">${esc(r.note)}</td></tr>`).join('')}`)}`,
  };
}

function secEvals(A) {
  const e = A.diagnosis_eval;
  if (!e) return { title: 'Diagnosis accuracy', html: '<div class="empty">No eval run yet. Run python evals/run_leak_diagnosis.py.</div>' };
  return {
    title: 'Diagnosis accuracy',
    html: `
      <div class="fp-grid3">${e.settings.map(s => `<div class="fp-card"><div class="fp-card-t">${esc(s.label)}</div>
          <div class="fp-big">${s.median_planted_correct} <span class="fp-dim">of ${s.planted_cases} planted</span></div>
          <div class="fp-dim">likely cause right, median of ${s.runs} runs; decoys read as normal ${s.median_decoys_ok} of ${s.decoy_cases};
            true cause ruled out ${s.median_true_ruled_out}; unverified figures ${s.unverified}; invalid after retry ${s.invalid}</div></div>`).join('')}</div>
      ${tbl(`<tr><th>Case</th><th>Planted cause</th>${e.settings.map(s => `<th>${esc(s.label)}</th>`).join('')}</tr>
        ${e.cases.map(c => `<tr><td><b>${esc(c.id.replace(/_/g, ' '))}</b></td><td>${esc(c.true_cause)}</td>
          ${e.settings.map(s => `<td>${((c.runs || {})[s.key] || []).map(r => r.correct
            ? `<span class="fc-mark ok" title="${esc(r.cause)}">✓</span>` : `<span class="fc-mark no" title="${esc(r.cause || 'unavailable')}">✗</span>`).join(' ')}</td>`).join('')}</tr>`).join('')}`)}
      <p class="fp-dim">${esc(e.model)} · run ${esc(e.run_at)} · ${esc(e.note || '')}</p>`,
  };
}

function secBench(A) {
  const c = A.calibration, v = A.coverage;
  return {
    title: 'Benchmarks',
    html: `
      <div class="kpis">
        <div class="kpi ${c.in_band ? '' : 'warn'}"><b>${pc(c.median_unit_efficiency_pct)}</b><span>median unit efficiency (band ${c.band[0]}-${c.band[1]}%)</span></div>
        <div class="kpi"><b>${pc(v.share_pct)}</b><span>of ledger cost on a node</span></div>
      </div>
      ${tbl(`<tr><th>Function</th><th>Necessary cost is</th><th class="num">Unit-months with a peer dimension dropped</th></tr>
        ${A.benchmarks.map(x => `<tr><td><b>${esc(x.label)}</b></td><td>${esc(x.basis)}${x.quantile ? ` <span class="fp-dim">(quantile ${fmt(x.quantile, 2)})</span>` : ''}</td>
          <td class="num">${n0(x.unit_months_with_dimension_dropped)}</td></tr>`).join('')}`)}`,
  };
}

function secHow(A) {
  return {
    title: 'How it works',
    html: `<div class="fc-explain"><div class="fc-ex-col">
        <div class="fp-card-t">The four figures</div>
        <p>Every block, vehicle, driver, crew, material and road segment carries, each month: what was spent on it (the CO ledger),
          the crop it lost without paying for it (loose fruit, late rounds, shrinkage, mill-queue FFA, downtime), and what it
          needed to spend (its volume at the rate the best quarter of comparable units achieve, or a hard spec such as litres per
          km or the fertiliser prescription). Leakage is what ran over, driver by driver; a saving on one driver never hides waste
          on another. Efficiency is necessary cost over spend plus crop lost.</p>
        <div class="fp-card-t">Rules</div>
        <ul class="fc-read">${A.rules.map(r => `<li>${esc(r)}</li>`).join('')}</ul>
      </div><div class="fc-ex-col">
        <div class="fp-card-t">The likely cause</div>
        <p>For a leak's largest driver the server runs fixed checks on the records, chosen so each candidate cause leaves a
          different trace. Amazon Nova weighs those findings against what each cause would look like and chooses one, citing the
          checks, and picks actions from a vetted list. Every cause, check and action it names must exist, and every figure it
          writes is matched against what it was given; otherwise the rule-based why and do stand. A likely cause is a hypothesis
          until its confirm step is done.</p>
        <div class="fp-card-t">What would make it real</div>
        <p>KSB1/KOB1 or ACDOCA line items, the KP06 budget, the cost centre and order masters with their block and vehicle
          mapping, payroll by wage type per gang, and the fuel card extract, for plant EC over 24 months.</p>
      </div></div>`,
  };
}

/* ── the side panel: budget vs actual ────────────────────────────────── */

export async function panelBudget() {
  const B = await getJSON('/gis/finance/budget');
  if (!B.available) return `<div class="empty">${esc(B.reason)}</div>`;
  return `<div class="fc-summary">${n0(B.flagged_total)} of ${n0(B.lines)} budget lines over in ${esc(B.month_label)}.</div>
    ${tbl(`<tr><th>Group</th><th class="num">Budget</th><th class="num">Actual</th><th class="num">Lines over</th></tr>
      ${B.groups.map(g => `<tr><td>${esc(g.group.toLowerCase())}</td><td class="num">${esc(rp(g.budget_idr))}</td>
        <td class="num">${esc(rp(g.actual_idr))}</td><td class="num">${g.flagged}</td></tr>`).join('')}`)}
    <div class="fp-actbar"><button class="ops-btn" data-open-panel="leakage">Where money leaks</button></div>`;
}
