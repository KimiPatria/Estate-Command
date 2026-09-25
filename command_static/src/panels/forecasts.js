/* Tomorrow's outlook: the four forecasts behind the plan, for people who run
 * the estate rather than people who build models.
 *
 * Every section leads with a sentence a manager can act on ("a 54% chance of
 * rain heavy enough to wash off spraying", "expect 19 to 23 of 24"), shows
 * the range as a bar rather than a table of numbers, says in one word how far
 * to trust it, and keeps the technical note folded away at the bottom.
 *
 *   Tomorrow   At a glance · Rain · Who turns up · How much gets done · Crew speeds
 *   Trust      How accurate they are · How they work
 *
 * The same pieces render inside each operation window: a strip on the plan
 * and a Forecast section. Every figure and every sentence comes from the
 * server (gis/forecasts.py); this module only lays them out.
 */
import { esc, fmt } from '../lib/fmt.js';
import { getJSON } from '../lib/api.js';
import { MAP_ICON } from '../shell/popup.js';
import { S } from '../state/store.js';
import { openPanel } from './registry.js';

const n0 = v => (v === null || v === undefined) ? '—' : fmt(v, 0);
const n1 = v => (v === null || v === undefined) ? '—' : fmt(v, 1);
const rate = v => (v === null || v === undefined) ? '—' : fmt(v, v < 10 ? 2 : v < 100 ? 1 : 0);

const OP_LABEL = { harvest: 'Harvesting', prune: 'Pruning', weed: 'Weeding', spray: 'Spraying', pest: 'Pest control' };
const OP_WINDOW = { harvest: 'ops_harvest', prune: 'ops_prune', weed: 'ops_weed', spray: 'ops_weed',
                    pest: 'ops_pest', dispatch: 'ops_dispatch' };
const TYPE_LABEL = { '': 'All crews', harvest: 'Harvest gangs', upkeep: 'Upkeep crews', spray: 'Spray teams', pest: 'Pest teams' };

/* ── small pieces ─────────────────────────────────────────────────────── */

export function trustPill(g) {
  if (!g) return '';
  return `<span class="fc-trust ${esc(g.tone || 'low')}" title="${esc(g.meaning || '')}">${esc(g.label)}</span>`;
}

export function trainedOn(t) {
  return t === 'real'
    ? '<span class="prov real" title="Learned from real forecasts and real recorded rainfall">trained on real data</span>'
    : `<span class="prov synthetic" title="Learned from the generated ledger: it shows how the method works, not yet what this estate's crews do">trained on generated data</span>`;
}

export function predictedBadge(title) {
  return `<span class="prov predicted" title="${esc(title || 'A forecast, not a record')}">predicted</span>`;
}

const tone = p => p >= 55 ? 'hi' : p >= 30 ? 'mid' : 'lo';

function meter(p, cls = '') {
  const w = Math.max(0, Math.min(100, p || 0));
  return `<span class="fc-meter ${cls}" role="img" aria-label="${Math.round(w)} percent"><i style="width:${w}%"></i></span>`;
}

function chanceRow(c) {
  return `<div class="fc-chance ${tone(c.pct)}">
    <div class="fc-chance-h">
      <span class="lbl">${esc(c.label)} <span class="fp-dim">${esc(c.threshold_mm)} mm or more</span></span>
      <b>${c.pct}%</b><span class="chip">${esc(c.words)}</span>
    </div>
    ${meter(c.pct, tone(c.pct))}
    <div class="fc-mean">${esc(c.meaning)}</div>
  </div>`;
}

/* A likely range on a track from zero to the roll: the band is where the
   real figure lands 8 days in 10, the tick is the most likely figure, and on
   a past date a second mark shows what actually happened. */
export function rangeBar({ low, high, most, max, actual }) {
  if (low === null || low === undefined || high === null || high === undefined || !max) return '';
  const x = v => Math.max(0, Math.min(100, 100 * v / max));
  return `<span class="fc-range" title="Likely ${low} to ${high}, most likely ${most}${actual !== null && actual !== undefined ? `; actual ${actual}` : ''}">
    <i class="band" style="left:${x(low)}%;width:${Math.max(1.5, x(high) - x(low))}%"></i>
    <i class="most" style="left:${x(most)}%"></i>
    ${actual !== null && actual !== undefined ? `<i class="act" style="left:${x(actual)}%"></i>` : ''}
  </span>`;
}

function speedBar(f) {
  const d = Math.max(-0.4, Math.min(0.4, (f || 1) - 1));
  const w = Math.abs(d) / 0.4 * 50;
  return `<span class="fc-speed ${d >= 0 ? 'up' : 'down'}" title="${Math.round(100 * (f - 1))}% against the average">
    <i style="left:${d >= 0 ? 50 : 50 - w}%;width:${w}%"></i></span>`;
}

export function mark(ok) {
  if (ok === null || ok === undefined) return '<span class="fc-mark na" title="Not testable here">–</span>';
  return ok ? '<span class="fc-mark ok" title="Passed">✓</span>' : '<span class="fc-mark no" title="Not met">✗</span>';
}

export function checksList(rows, empty = 'No checks for this one.') {
  if (!rows || !rows.length) return `<div class="empty">${esc(empty)}</div>`;
  return `<ul class="fc-checks">${rows.map(r => `<li>${mark(r.recovered)}<span>${esc(r.plain)}</span></li>`).join('')}</ul>`;
}

export function explainBlock(ex) {
  if (!ex) return '';
  return `<div class="fc-explain">
    <div class="fc-ex-col">
      <div class="fp-card-t">What it tells you</div><p>${esc(ex.what)}</p>
      <div class="fp-card-t">How it works</div>
      <ol class="fc-steps">${ex.how.map(h => `<li>${esc(h)}</li>`).join('')}</ol>
    </div>
    <div class="fc-ex-col">
      <div class="fp-card-t">How to read it</div>
      <ul class="fc-read">${ex.read.map(h => `<li>${esc(h)}</li>`).join('')}</ul>
      <div class="fp-card-t">What would make it real</div><p>${esc(ex.real)}</p>
      <details class="fc-tech"><summary>Technical note</summary><p>${esc(ex.technical)}</p></details>
    </div>
  </div>`;
}

export function hero(big, words, sentence, extra = '') {
  return `<div class="fc-hero">
    <div class="fc-hero-n">${big}</div>
    <div class="fc-hero-t">${words ? `<div class="fc-words">${words}</div>` : ''}
      <p>${sentence}</p>${extra}</div>
  </div>`;
}

function sprayCallout(call) {
  if (!call) return '';
  return `<div class="fc-call ${call.go ? 'go' : 'hold'}">
    <div class="fc-call-v">${esc(call.verdict)}</div>
    <div><p>${esc(call.reason || call.plain)}</p></div>
  </div>`;
}

export function chips(name, options, current) {
  return `<div class="fc-chips" role="group">${options.map(([v, l]) => `<button class="${String(v) === String(current) ? 'on' : ''}"
    data-${name}="${esc(v)}" aria-pressed="${String(v) === String(current)}">${esc(l)}</button>`).join('')}</div>`;
}

export const tbl = inner => `<div class="fp-tbl"><table class="tbl">${inner}</table></div>`;

/* ── the plan strip and the Forecast section in the operation windows ──── */

export function planForecastStrip(P) {
  const f = P && P.forecast;
  if (!f) return '';
  const out = [];
  if (f.rain && f.rain.in_use) {
    const c = f.rain.chances[P.operation === 'spray' ? 'washoff' : 'heavy'];
    out.push(`<span class="fc-pill ${tone(c.pct)}"><em>Rain</em> ${c.pct}% chance of ${P.operation === 'spray' ? 'wash-off' : 'heavy rain'}</span>`);
  }
  if (f.spray_call) out.push(`<span class="fc-pill ${f.spray_call.go ? 'lo' : 'hi'}"><em>Spraying</em> ${esc(f.spray_call.verdict)}</span>`);
  if (f.headcount && f.headcount.in_use) {
    out.push(`<span class="fc-pill"><em>People</em> ${n0(f.headcount.low)} to ${n0(f.headcount.high)} likely</span>`);
  }
  if (f.work_done && f.work_done.in_use) {
    out.push(`<span class="fc-pill ${f.work_done.expected_pct < 65 ? 'mid' : ''}"><em>Done</em> about ${f.work_done.expected_pct}%
      <span class="fp-dim">(${f.work_done.low_pct} to ${f.work_done.high_pct}%)</span></span>`);
  }
  if (f.speeds && f.speeds.in_use) out.push('<span class="fc-pill"><em>Speeds</em> learned</span>');
  if (!out.length) return '';
  return `<div class="fc-strip">${predictedBadge('These come from the forecasts, not from records')}
    ${out.join('')}<button class="ops-btn small" data-go="forecast">What the forecasts say</button></div>`;
}

export function opsForecastSection(P) {
  const f = P.forecast || {};
  const unit = P.unit;
  const parts = [];

  if (f.summary) parts.push(`<div class="fc-summary">${predictedBadge()} ${esc(f.summary)}</div>`);
  if (f.spray_call) parts.push(sprayCallout(f.spray_call));

  if (f.rain && f.rain.in_use) {
    const r = f.rain;
    parts.push(`<div class="fp-card fc-block">
      <div class="fc-block-h"><div class="fp-card-t">Rain</div>${trustPill(r.grade)}${trainedOn('real')}</div>
      <p class="fc-lead">${esc(r.headline)}</p>
      <div class="fc-chances">${['washoff', 'heavy', 'stop'].map(k => chanceRow(r.chances[k])).join('')}</div>
      <p class="fp-dim">${esc(r.amount.plain || '')} ${esc(r.forecast_plain || '')} ${esc(r.recorded_plain || '')}</p>
    </div>`);
  } else if (f.rain) {
    parts.push(`<div class="fp-card fc-block"><div class="fp-card-t">Rain</div><p class="fp-dim">${esc(f.rain.source || 'Not forecast.')}</p></div>`);
  }

  if (f.headcount && f.headcount.in_use) {
    const h = f.headcount;
    const rows = (P.crews || []).filter(c => c.present_low !== null && c.present_low !== undefined);
    parts.push(`<div class="fp-card fc-block">
      <div class="fc-block-h"><div class="fp-card-t">Who turns up</div>${trainedOn('synthetic')}</div>
      <p class="fc-lead">${esc(h.plain)}</p>
      ${h.recorded !== null && h.recorded !== undefined ? `<p class="fp-dim">On the day, ${n0(h.recorded)} actually came.</p>` : ''}
      ${tbl(`<tr><th>${esc(P.crew_label)}</th><th>Likely range</th><th class="num">Most likely</th><th class="num">Roll</th><th>Why</th></tr>
        ${rows.map(c => `<tr><td><b>${esc(c.crew_code)}</b></td>
          <td>${rangeBar({ low: c.present_low, high: c.present_high, most: c.present, max: c.on_roll, actual: c.recorded_present })}
            <span class="fp-dim">${c.present_low} to ${c.present_high}</span></td>
          <td class="num">${c.present}</td><td class="num">${c.on_roll}</td>
          <td class="fc-why">${esc((c.turnout_drivers || []).join(' '))}</td></tr>`).join('')}`)}
    </div>`);
  }

  if (f.work_done && f.work_done.in_use) {
    const w = f.work_done;
    const risk = w.at_risk || [];
    const blocks = Object.fromEntries((P.crews || []).flatMap(c => c.blocks.map(b => [b.block_label, b])));
    parts.push(`<div class="fp-card fc-block">
      <div class="fc-block-h"><div class="fp-card-t">How much gets done</div>${trainedOn('synthetic')}</div>
      ${hero(`${w.expected_pct}%`, 'expected done', esc(w.plain),
        `<div class="fc-band">${rangeBar({ low: w.low_pct, high: w.high_pct, most: w.expected_pct, max: 100 })}
          <span class="fp-dim">${w.low_pct}% to ${w.high_pct}% in 8 days out of 10</span></div>`)}
      <p>${esc(w.risk_plain)}</p>
      ${risk.length ? tbl(`<tr><th>Block</th><th>${esc(P.crew_label)}</th><th class="num">Needs another day</th>
          <th class="num">Likely done</th><th>Why</th><th></th></tr>
        ${risk.map(r => `<tr data-map="${esc(r.block_label)}" data-map-caption="${esc(`Likely to need another day: ${r.block_label}`)}">
          <td><b>${esc(r.block_label)}</b></td><td>${esc(r.crew_code)}</td>
          <td class="num">${Math.round(100 * r.p_carried)}%</td><td class="num">${Math.round(100 * r.expected_share)}%</td>
          <td class="fc-why">${esc(((blocks[r.block_label] || {}).risk_drivers || []).join(' '))}</td>
          <td><button class="fp-map-btn" data-map="${esc(r.block_label)}" data-map-caption="${esc(r.block_label)}" title="Show on map">${MAP_ICON}</button></td></tr>`).join('')}`) : ''}
    </div>`);
  } else if (f.work_done) {
    parts.push(`<div class="fp-card fc-block"><div class="fp-card-t">How much gets done</div>
      <p class="fp-dim">Not forecast for this plan: ${esc(f.work_done.reason || 'switched off')}.</p></div>`);
  }

  if (f.speeds) {
    const s = f.speeds;
    parts.push(`<div class="fp-card fc-block">
      <div class="fc-block-h"><div class="fp-card-t">Crew speeds</div>${trainedOn('synthetic')}</div>
      <p class="fc-lead">${esc(s.plain)}</p>
      ${s.faster && (s.faster.length || s.slower.length) ? `<p>${s.faster.map(c => `<span class="chip pos">${esc(c.crew_code)} +${Math.round(100 * (c.factor - 1))}%</span>`).join(' ')}
        ${s.slower.map(c => `<span class="chip neg">${esc(c.crew_code)} ${Math.round(100 * (c.factor - 1))}%</span>`).join(' ')}</p>` : ''}
    </div>`);
  }

  parts.push(`<div class="fc-inputs"><div class="sub-t">What this plan is built on</div>
    ${tbl(`<tr><th>Input</th><th>Taken from</th><th>What it says</th></tr>
      ${(f.inputs || []).map(i => `<tr><td><b>${esc(i.input)}</b></td>
        <td>${i.used === 'forecast' || i.used === 'learned' ? predictedBadge() : `<span class="chip">${esc(i.used)}</span>`}</td>
        <td class="fc-why">${esc(i.plain)}</td></tr>`).join('')}`)}
    <div class="fp-actbar"><button class="ops-btn" data-open-panel="forecasts">Open tomorrow's outlook</button>
      <button class="ops-btn" data-open-panel="assumptions">Open the assumption register</button></div>
  </div>`);

  const blockLabels = ((f.work_done && f.work_done.at_risk) || []).map(r => r.block_label);
  return {
    title: P.is_tomorrow ? `What the forecasts say for ${P.date_label}` : `What the forecasts said for ${P.date_label}`,
    blocks: blockLabels,
    blocksCaption: 'Blocks likely to need another day',
    html: parts.join(''),
  };
}

/* ── the outlook window ───────────────────────────────────────────────── */

export function forecastsPopup() {
  S.fc = S.fc || { section: 'overview', date: null, crewType: '', op: 'harvest', speedOp: 'weed' };
  const F = S.fc;
  let O = null;
  const cache = {};
  const dq = () => (F.date ? `date=${F.date}` : '');
  const fetchView = async (key, url) => {
    if (!cache[key]) cache[key] = await getJSON(url);
    return cache[key];
  };

  return {
    key: 'forecasts',
    initial: F.section,
    loadingText: 'Checking the forecasts… the first time after a restart this can take half a minute.',
    async load() {
      Object.keys(cache).forEach(k => delete cache[k]);
      O = await getJSON(`/gis/forecast/outlook${F.date ? `?${dq()}` : ''}`);
    },
    header: () => O ? `
      ${predictedBadge('Forecasts, not records')}
      <span class="prov ${O.is_tomorrow ? 'scheduled' : 'synthetic'}">${O.is_tomorrow ? 'tomorrow' : 'replay'}: ${esc(O.date_label)}</span>
      <label class="fc-date" title="Pick a past day to see what the forecasts said, and what happened">
        <span>Day</span><input type="date" data-fc-date value="${esc(O.date)}" min="${esc(O.window.from)}" max="${esc(O.window.tomorrow)}"></label>` : '',
    wireHeader(host, api) {
      const inp = host.querySelector('[data-fc-date]');
      if (inp) inp.onchange = () => {
        if (!inp.value) return;
        F.date = inp.value === O.window.tomorrow ? null : inp.value;
        api.reload();
      };
    },
    menu: () => {
      if (!O) return [];
      const c = O.cards;
      const wd = (c.work_done.operations || []).find(w => w.operation === 'harvest');
      return [
        { label: O.is_tomorrow ? 'Tomorrow' : 'Replay', items: [
          { id: 'overview', label: 'At a glance' },
          { id: 'rain', label: 'Rain', count: c.rain.available ? `${c.rain.chances.washoff.pct}%` : 'off',
            alert: c.rain.available && c.rain.chances.washoff.pct >= 50 },
          { id: 'headcount', label: 'Who turns up',
            count: c.headcount.available ? `${fmt(c.headcount.low, 0)}–${fmt(c.headcount.high, 0)}` : 'off' },
          { id: 'work', label: 'How much gets done',
            count: wd && wd.in_use ? `${wd.expected_pct}%` : '' },
          { id: 'speeds', label: 'Crew speeds', count: c.speeds.available ? '' : 'off' },
        ] },
        { label: 'Trust', items: [
          { id: 'accuracy', label: 'How accurate they are',
            count: O.trust.filter(t => t.grade.passes).length + '/' + O.trust.length },
          { id: 'how', label: 'How they work' },
        ] },
      ];
    },
    onSection: id => { F.section = id; },
    async section(id) {
      if (id === 'rain') return secRain(await fetchView(`rain`, `/gis/forecast/rain?${dq()}`), O);
      if (id === 'headcount') {
        return secHeadcount(await fetchView(`hc:${F.crewType}`,
          `/gis/forecast/headcount?${dq()}${F.crewType ? `&crew_type=${F.crewType}` : ''}`), F);
      }
      if (id === 'work') {
        return secWork(await fetchView(`work:${F.op}`, `/gis/forecast/work-done?operation=${F.op}&${dq()}`), F);
      }
      if (id === 'speeds') {
        return secSpeeds(await fetchView(`speeds:${F.speedOp}`, `/gis/forecast/speeds?operation=${F.speedOp}&${dq()}`), F);
      }
      if (id === 'accuracy') return secAccuracy(await fetchView('accuracy', '/gis/forecast/accuracy'));
      if (id === 'how') return secHow(await fetchView('accuracy', '/gis/forecast/accuracy'), O);
      return secOverview(O);
    },
    wire(root, api) {
      root.querySelectorAll('[data-go]').forEach(b => {
        b.onclick = ev => { ev.stopPropagation(); api.go(b.dataset.go); };
        if (b.tagName !== 'BUTTON') {
          b.onkeydown = ev => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); api.go(b.dataset.go); } };
        }
      });
      root.querySelectorAll('[data-open-panel]').forEach(b => { b.onclick = () => openPanel(b.dataset.openPanel); });
      root.querySelectorAll('[data-crewtype]').forEach(b => {
        b.onclick = () => { F.crewType = b.dataset.crewtype; api.rerender(); };
      });
      root.querySelectorAll('[data-op]').forEach(b => {
        b.onclick = () => { F.op = b.dataset.op; api.rerender(); };
      });
      root.querySelectorAll('[data-speedop]').forEach(b => {
        b.onclick = () => { F.speedOp = b.dataset.speedop; api.rerender(); };
      });
      root.querySelectorAll('[data-work-op]').forEach(b => {
        b.onclick = () => { F.op = b.dataset.workOp; api.go('work'); };
      });
    },
  };
}

/* ── sections ─────────────────────────────────────────────────────────── */

function secOverview(O) {
  const c = O.cards;
  const r = c.rain, h = c.headcount, w = c.work_done, sp = c.speeds;
  const trustOf = m => (O.trust.find(t => t.model === m) || {}).grade;

  const rainCard = r.available ? `
    <div class="fc-card" data-go="rain" role="button" tabindex="0">
      <div class="fc-card-h"><span>Rain</span>${trustPill(trustOf('rain'))}</div>
      <div class="fc-card-big ${tone(r.chances.washoff.pct)}">${r.chances.washoff.pct}%</div>
      <div class="fc-card-sub">chance of rain that washes off spraying (15 mm or more): <b>${esc(r.chances.washoff.words)}</b></div>
      <div class="fc-mini">${meter(r.chances.heavy.pct, tone(r.chances.heavy.pct))}<span>Heavy rain (25 mm+): ${r.chances.heavy.pct}%, ${esc(r.chances.heavy.words)}</span></div>
      <div class="fc-mini">${meter(r.chances.stop.pct, tone(r.chances.stop.pct))}<span>Work-stopping rain: ${r.chances.stop.pct}%, ${esc(r.chances.stop.words)}</span></div>
      <p class="fp-dim">${esc(r.amount.plain || '')}${r.recorded_plain ? ` ${esc(r.recorded_plain)}` : ''}</p>
      <span class="fc-more">Rain in detail →</span>
    </div>` : `<div class="fc-card"><div class="fc-card-h"><span>Rain</span></div><p class="fp-dim">${esc(r.reason)}</p></div>`;

  const hcCard = h.available ? `
    <div class="fc-card" data-go="headcount" role="button" tabindex="0">
      <div class="fc-card-h"><span>Who turns up</span>${trustPill(trustOf('headcount'))}</div>
      <div class="fc-card-big">${n0(h.low)}–${n0(h.high)}</div>
      <div class="fc-card-sub">of ${n0(h.on_roll)} people on the roll are likely to turn up (most likely <b>${n0(h.most_likely)}</b>)</div>
      <div class="fc-band">${rangeBar({ low: h.low, high: h.high, most: h.most_likely, max: h.on_roll, actual: h.recorded })}</div>
      <ul class="fc-types">${h.by_type.map(t => `<li>${esc(t.plain)}</li>`).join('')}</ul>
      ${h.recorded !== null && h.recorded !== undefined ? `<p class="fp-dim">On the day, ${n0(h.recorded)} actually came.</p>` : ''}
      <span class="fc-more">Every crew →</span>
    </div>` : `<div class="fc-card"><div class="fc-card-h"><span>Who turns up</span></div><p class="fp-dim">${esc(h.reason || '')}</p></div>`;

  const workCard = `
    <div class="fc-card">
      <div class="fc-card-h"><span>How much gets done</span>${trustPill(trustOf('work_done'))}</div>
      <table class="fc-worktbl">${(w.operations || []).map(o => `
        <tr data-work-op="${esc(o.operation)}" title="Open ${esc(o.label)}">
          <td><b>${esc(o.label)}</b></td>
          <td>${o.in_use ? `${rangeBar({ low: o.low_pct, high: o.high_pct, most: o.expected_pct, max: 100 })}` :
            o.spray_call ? `<span class="chip ${o.spray_call.go ? 'pos' : 'neg'}">${esc(o.spray_call.verdict)}</span>` :
            o.stops ? '<span class="chip neg">called off</span>' : '<span class="fp-dim">not forecast</span>'}</td>
          <td class="num">${o.in_use ? `<b>${o.expected_pct}%</b> <span class="fp-dim">${o.low_pct}–${o.high_pct}</span>` : ''}</td>
        </tr>`).join('')}</table>
      <span class="fc-more" data-go="work" role="button" tabindex="0">Blocks at risk →</span>
    </div>`;

  const speedCard = `
    <div class="fc-card" data-go="speeds" role="button" tabindex="0">
      <div class="fc-card-h"><span>Crew speeds</span>${trustPill(trustOf('speeds'))}</div>
      ${sp.available ? `<ul class="fc-types">${sp.operations.map(o => `<li><b>${esc(o.label)}:</b> ${o.faster} faster, ${o.slower} slower than average, of ${o.total} ${esc(o.unit)}s</li>`).join('')}</ul>
        <p class="fp-dim">${esc((sp.operations.find(o => o.operation === 'weed') || sp.operations[0] || { examples: [''] }).examples[0] || '')}</p>`
        : `<p class="fp-dim">${esc(sp.reason || '')}</p>`}
      <span class="fc-more">Every crew →</span>
    </div>`;

  const spray = (w.operations || []).find(o => o.operation === 'spray' && o.spray_call);

  return {
    title: O.is_tomorrow ? `Tomorrow at a glance: ${O.date_label}` : `At a glance: ${O.date_label}`,
    html: `
      <div class="fc-summary-box">
        <div class="fp-card-t">In short</div>
        <ul>${O.summary.map(l => `<li>${esc(l)}</li>`).join('')}</ul>
      </div>
      ${spray ? sprayCallout(spray.spray_call) : ''}
      <div class="fc-cards">${rainCard}${hcCard}${workCard}${speedCard}</div>
      <div class="sub-t">How far to trust them</div>
      <div class="fc-trustrow">${O.trust.map(t => `<div class="fc-trustcard">
          <div class="fc-card-h"><span>${esc(t.title)}</span>${trustPill(t.grade)}</div>
          ${trainedOn(t.trained_on)}
          ${t.in_use ? '' : '<span class="chip">not used in the plan</span>'}
        </div>`).join('')}</div>`,
  };
}

function secRain(V, O) {
  const fc = V.forecast || {};
  if (!fc.available) return { title: 'Rain', html: `<div class="empty">${esc(fc.reason || 'No rain forecast.')}</div>` };
  const bt = V.backtest;
  const w = bt.thresholds['15'];
  const spray = ((O.cards.work_done.operations || []).find(o => o.operation === 'spray') || {}).spray_call;
  return {
    title: `Rain: ${V.date_label}`,
    html: `
      ${hero(`${fc.chances.washoff.pct}%`, `${esc(fc.chances.washoff.words)}`, esc(fc.chances.washoff.meaning),
        `<p class="fp-dim">${esc(fc.amount.plain || '')}</p>`)}
      ${spray ? sprayCallout(spray) : ''}
      <div class="fc-chances wide">${['washoff', 'heavy', 'stop'].map(k => chanceRow(fc.chances[k])).join('')}</div>
      <div class="fp-grid3">
        <div class="fp-card"><div class="fp-card-t">What last night's forecast said</div><p>${esc(fc.forecast_plain || 'No archived forecast for this day.')}</p></div>
        <div class="fp-card"><div class="fp-card-t">Usual for this month</div>
          <p>${fc.month_average_pct.washoff}% for 15 mm, ${fc.month_average_pct.heavy}% for 25 mm</p></div>
        ${fc.recorded_plain ? `<div class="fp-card"><div class="fp-card-t">What happened</div><p class="fc-lead">${esc(fc.recorded_plain)}</p></div>` : ''}
      </div>
      ${V.recent.length ? `<div class="sub-t">The days before: what it said, what fell</div>
        ${tbl(`<tr><th>Day</th><th class="num">Chance it said</th><th></th><th class="num">Rain that fell</th><th>15 mm or more?</th></tr>
          ${V.recent.map(d => `<tr><td>${esc(d.label)}</td><td class="num"><b>${d.said_pct}%</b></td>
            <td>${meter(d.said_pct, tone(d.said_pct))}</td><td class="num">${n0(d.fell_mm)} mm</td>
            <td>${d.happened ? '<span class="chip neg">yes</span>' : '<span class="chip">no</span>'}</td></tr>`).join('')}`)}` : ''}
      <div class="sub-t">How accurate is it? ${trustPill(bt.grade)} ${trainedOn('real')}</div>
      <ul class="fc-read">
        <li>${esc(bt.plain.headline)}</li><li>${esc(bt.plain.caught)}</li><li>${esc(bt.plain.raw)}</li>
      </ul>
      ${tbl(`<tr><th>When it said</th><th class="num">Days</th><th class="num">It happened on</th><th>In words</th></tr>
        ${w.reliability.map(x => `<tr><td>about ${x.said_pct}%</td><td class="num">${n0(x.days)}</td>
          <td class="num">${x.happened_pct}%</td><td class="fc-why">${esc(x.plain)}</td></tr>`).join('')}`)}`,
  };
}

function secHeadcount(V, F) {
  if (!V.available) return { title: 'Who turns up', html: '<div class="empty">No headcount forecast.</div>' };
  const e = V.estate, bt = V.backtest, seg = bt.segments;
  const segRow = (label, s) => s ? `<tr><td>${esc(label)}</td><td class="num"><b>${n1(s.model_error)}</b></td>
    <td class="num">${n1(s.old_error)}</td><td class="num">${s.improvement_pct > 0 ? `<span class="pos">${n0(s.improvement_pct)}% better</span>` : `<span class="neg">${n0(-s.improvement_pct)}% worse</span>`}</td></tr>` : '';
  return {
    title: `Who turns up: ${V.date_label}`,
    html: `
      ${chips('crewtype', Object.entries(TYPE_LABEL), F.crewType)}
      ${hero(`${n0(e.low)}–${n0(e.high)}`, `of ${n0(e.on_roll)} on the roll`, esc(e.plain),
        `<div class="fc-band">${rangeBar({ low: e.low, high: e.high, most: e.most_likely, max: e.on_roll, actual: e.recorded })}</div>
         ${e.recorded !== null && e.recorded !== undefined ? `<p class="fp-dim">On the day, ${n0(e.recorded)} actually came.</p>` : ''}`)}
      <div class="sub-t">Crew by crew</div>
      ${tbl(`<tr><th>Crew</th><th>Div</th><th>Likely range</th><th class="num">Most likely</th><th class="num">Roll</th>
          <th class="num">Recent turnout</th>${V.crews.some(c => c.recorded !== null) ? '<th class="num">Came</th>' : ''}<th>Why this day differs</th></tr>
        ${V.crews.map(c => `<tr><td><b>${esc(c.crew_code)}</b></td><td>${esc(c.division_code || '')}</td>
          <td>${rangeBar({ low: c.low, high: c.high, most: c.most_likely, max: c.on_roll, actual: c.recorded })}
            <span class="fp-dim">${c.low} to ${c.high}</span></td>
          <td class="num"><b>${c.most_likely}</b></td><td class="num">${c.on_roll}</td>
          <td class="num">${c.recent_pct === null ? '—' : `${n0(c.recent_pct)}%`}</td>
          ${V.crews.some(x => x.recorded !== null) ? `<td class="num">${c.recorded === null ? '—' : c.recorded}</td>` : ''}
          <td class="fc-why">${esc(c.drivers.slice(1).join(' ') || 'An ordinary day for this crew.')}</td></tr>`).join('')}`)}
      ${V.recent.length ? `<div class="sub-t">The days before: expected against who came</div>
        ${tbl(`<tr><th>Day</th><th>Expected</th><th class="num">Came</th><th>Inside the range?</th></tr>
          ${V.recent.map(d => `<tr><td>${esc(d.label)}</td><td>${n0(d.low)} to ${n0(d.high)} <span class="fp-dim">(most likely ${n0(d.most_likely)})</span></td>
            <td class="num"><b>${n0(d.recorded)}</b></td><td>${mark(d.inside)}</td></tr>`).join('')}`)}` : ''}
      <div class="sub-t">How accurate is it? ${trustPill(bt.grade)} ${trainedOn('synthetic')}</div>
      <ul class="fc-read">${Object.values(bt.plain).filter(Boolean).map(l => `<li>${esc(l)}</li>`).join('')}</ul>
      ${tbl(`<tr><th>Days</th><th class="num">Off by, this forecast</th><th class="num">Off by, the old average</th><th class="num">Result</th></tr>
        ${segRow('Every day', seg.all)}${segRow('Sundays', seg.sundays)}${segRow('Lebaran leave', seg.lebaran)}
        ${segRow('The week after Lebaran', seg.after_lebaran)}${segRow('Ordinary days', seg.ordinary)}`)}
      <div class="sub-t">Does it find what is really there?</div>
      ${checksList(V.recovery.rows)}`,
  };
}

function secWork(V, F) {
  const opChips = chips('op', Object.entries(OP_LABEL), F.op);
  if (!V.available) return { title: 'How much gets done', html: `${opChips}<div class="empty">${esc(V.reason || 'No plan.')}</div>` };
  const w = V.work_done || {}, bt = V.backtest, six = bt.this_operation, know = bt.this_operation_knowing;
  let body;
  if (w.in_use) {
    body = `
      ${hero(`${w.expected_pct}%`, 'of the plan expected to get done', esc(w.plain),
        `<div class="fc-band">${rangeBar({ low: w.low_pct, high: w.high_pct, most: w.expected_pct, max: 100 })}
          <span class="fp-dim">${w.low_pct}% to ${w.high_pct}% in 8 days out of 10</span></div>`)}
      <div class="sub-t">Blocks likely to need another day</div>
      <p>${esc(w.risk_plain)}</p>
      ${V.at_risk.length ? tbl(`<tr><th>Block</th><th>Crew</th><th class="num">Chance it needs another day</th><th class="num">Likely done</th><th>Why</th><th></th></tr>
        ${V.at_risk.map(r => `<tr data-map="${esc(r.block_label)}" data-map-caption="${esc(`Likely to need another day: ${r.block_label}`)}">
          <td><b>${esc(r.block_label)}</b></td><td>${esc(r.crew_code)}</td>
          <td class="num"><b>${Math.round(100 * r.p_carried)}%</b></td><td class="num">${Math.round(100 * r.expected_share)}%</td>
          <td class="fc-why">${esc(r.drivers.join(' ') || 'The usual shortfall for this work.')}</td>
          <td><button class="fp-map-btn" data-map="${esc(r.block_label)}" data-map-caption="${esc(r.block_label)}" title="Show on map">${MAP_ICON}</button></td></tr>`).join('')}`) : ''}
      <div class="sub-t">Crew by crew</div>
      ${tbl(`<tr><th>Crew</th><th class="num">Planned</th><th class="num">Expected</th><th>Likely range</th><th></th></tr>
        ${V.crews.map(c => `<tr data-map="${esc(c.block_labels.join(','))}" data-map-caption="${esc(c.crew_code)}">
          <td><b>${esc(c.crew_code)}</b></td><td class="num">${n0(c.planned_qty)} ${esc(c.unit)}</td>
          <td class="num"><b>${n0(c.expected_qty)}</b></td>
          <td>${rangeBar({ low: c.low_qty, high: c.high_qty, most: c.expected_qty, max: c.planned_qty * 1.1 })}
            <span class="fp-dim">${n0(c.low_qty)} to ${n0(c.high_qty)}</span></td>
          <td><button class="fp-map-btn" data-map="${esc(c.block_labels.join(','))}" data-map-caption="${esc(c.crew_code)}" title="Show on map">${MAP_ICON}</button></td></tr>`).join('')}`)}`;
  } else {
    body = V.spray_call
      ? `${sprayCallout(V.spray_call)}
         <p class="fp-dim">${V.spray_call.go ? '' : 'Spraying is held, so nothing is assigned and there is no work to forecast.'}</p>`
      : `<div class="fp-callout">${esc(V.headline || '')}</div>
         <p class="fp-dim">${V.stops ? 'Nothing is assigned, so there is nothing to forecast.' : esc(w.reason || 'Work done is not forecast for this plan.')}</p>`;
  }
  return {
    title: `How much gets done: ${OP_LABEL[F.op]}, ${V.date_label}`,
    blocks: V.at_risk.map(r => r.block_label),
    blocksCaption: 'Blocks likely to need another day',
    html: `${opChips}${body}
      <div class="fp-actbar"><button class="ops-btn" data-open-panel="${esc(OP_WINDOW[F.op])}">Open the ${esc(OP_LABEL[F.op].toLowerCase())} plan</button></div>
      <div class="sub-t">What makes work fall short</div>
      <ul class="fc-read">${V.effects.map(e => `<li>${esc(e)}</li>`).join('')}</ul>
      <div class="sub-t">How accurate is it? ${trustPill(bt.grade)} ${trainedOn('synthetic')}</div>
      <ul class="fc-read"><li>${esc(bt.plain.headline)}</li><li>${esc(bt.plain.daily)}</li><li>${esc(bt.plain.knowing)}</li></ul>
      ${six ? tbl(`<tr><th>${esc(OP_LABEL[F.op])}</th><th class="num">Off by, this forecast</th><th class="num">Off by, the old average</th><th class="num">Result</th></tr>
        <tr><td>As at six in the morning, per order</td><td class="num"><b>${n1(six.order_error_pts)} pts</b></td><td class="num">${n1(six.old_order_error_pts)} pts</td>
          <td class="num">${six.order_improvement_pct > 0 ? `<span class="pos">${n0(six.order_improvement_pct)}% better</span>` : `<span class="neg">${n0(-six.order_improvement_pct)}% worse</span>`}</td></tr>
        <tr><td>As at six in the morning, the day's total</td><td class="num"><b>${n1(six.daily_total_error_pct)}%</b></td><td class="num">${n1(six.old_daily_total_error_pct)}%</td>
          <td class="num">${six.daily_improvement_pct > 0 ? `<span class="pos">${n0(six.daily_improvement_pct)}% better</span>` : `<span class="neg">${n0(-six.daily_improvement_pct)}% worse</span>`}</td></tr>
        ${know ? `<tr><td>Knowing the rain that fell, per order</td><td class="num"><b>${n1(know.order_error_pts)} pts</b></td><td class="num">${n1(know.old_order_error_pts)} pts</td>
          <td class="num">${know.order_improvement_pct > 0 ? `<span class="pos">${n0(know.order_improvement_pct)}% better</span>` : `<span class="neg">${n0(-know.order_improvement_pct)}% worse</span>`}</td></tr>` : ''}`) : ''}
      <div class="sub-t">Does it find what is really there?</div>
      ${checksList(V.recovery.rows)}`,
  };
}

function secSpeeds(V, F) {
  const opChips = chips('speedop', [['harvest', 'Harvest blocks'], ['prune', 'Pruning'], ['weed', 'Weeding'], ['spray', 'Spraying']], F.speedOp);
  if (!V.available) return { title: 'Crew speeds', html: `${opChips}<div class="empty">${esc(V.reason || 'No speeds.')}</div>` };
  const bt = V.backtest || {};
  const harvest = V.operation === 'harvest';
  const unitWord = harvest ? 'block' : V.unit;
  return {
    title: harvest ? 'Harvest pace by block' : `Crew speeds: ${OP_LABEL[V.operation]}`,
    blocks: harvest ? V.rows.filter(r => Math.abs(r.factor - 1) >= 0.1).map(r => r.label) : [],
    blocksCaption: 'Blocks at least 10% off their target',
    html: `${opChips}
      ${hero(`${V.faster} <span class="fc-slash">/</span> ${V.slower}`, `faster / slower of ${V.total} ${esc(unitWord)}s`,
        V.in_use ? 'Used in the plan.' : 'Not used in the plan.')}
      ${tbl(`<tr><th>${harvest ? 'Block' : 'Crew'}</th><th>Against average</th><th class="num">Learned rate</th><th class="num">Book rate</th>
          <th class="num">Records</th><th>Last 4 weeks</th><th>In words</th></tr>
        ${V.rows.map(r => `<tr${harvest ? ` data-map="${esc(r.label)}" data-map-caption="${esc(`Block ${r.label}`)}"` : ''}>
          <td><b>${esc(r.label)}</b></td>
          <td>${speedBar(r.factor)} <b class="${r.factor >= 1.03 ? 'pos' : r.factor <= 0.97 ? 'neg' : ''}">${r.factor >= 1 ? '+' : ''}${Math.round(100 * (r.factor - 1))}%</b></td>
          <td class="num">${rate(r.learned_rate)}</td><td class="num">${rate(r.book_rate)}</td>
          <td class="num">${n0(r.man_days)} md</td>
          <td class="fp-dim">${r.history.map(h => `${h >= 1 ? '+' : ''}${Math.round(100 * (h - 1))}%`).join(' → ')}</td>
          <td class="fc-why">${esc(r.plain)}</td></tr>`).join('')}`)}
      <div class="sub-t">How accurate is it? ${trustPill(bt.grade)} ${trainedOn('synthetic')}</div>
      ${bt.plain ? `<ul class="fc-read"><li>${esc(bt.plain)}</li></ul>` : ''}
      <div class="sub-t">Does it find what is really there?</div>
      ${checksList(V.recovery)}
      <div class="fp-actbar"><button class="ops-btn" data-open-panel="assumptions">Open the assumption register</button></div>`,
  };
}

function secAccuracy(A) {
  const rows = A.trust;
  const all = [
    ['Who turns up', A.headcount.recovery.rows],
    ['How much gets done', A.work_done.recovery.rows],
    ['Crew speeds', A.speeds.recovery.rows],
  ];
  return {
    title: 'How accurate the forecasts are',
    html: `
      <div class="fc-trustrow">${rows.map(t => `<div class="fc-trustcard wide">
          <div class="fc-card-h"><span>${esc(t.title)}</span>${trustPill(t.grade)}</div>
          <p class="fc-lead">${esc(t.headline)}</p><p>${esc(t.detail)}</p>
          ${trainedOn(t.trained_on)} ${t.in_use ? '<span class="chip pos">used in the plan</span>' : '<span class="chip">not used in the plan</span>'}
        </div>`).join('')}</div>
      <div class="sub-t">Do they find what is really there?</div>
      ${all.map(([t, r]) => `<div class="fc-checkgroup"><div class="fp-card-t">${esc(t)}</div>${checksList(r)}</div>`).join('')}`,
  };
}

function secHow(A, O) {
  const order = ['rain', 'headcount', 'work_done', 'speeds'];
  return {
    title: 'How the forecasts work',
    html: `
      <div class="fc-glossary">${O.glossary.map(g => `<span><b>${esc(g.term)}</b> ${esc(g.plain)}</span>`).join('')}</div>
      ${order.map(k => `<div class="fc-howblock"><h4>${esc(A.explain[k].title)} <span class="fp-dim">${esc(A.explain[k].question)}</span></h4>
        ${explainBlock(A.explain[k])}</div>`).join('')}
      <div class="sheet-note">${esc(O.note)}</div>`,
  };
}
