import { AI, aiFailure, genBadge } from './store.js';
import { getJSON } from '../lib/api.js';
import { esc } from '../lib/fmt.js';
import { renderReadinessButton } from '../shell/context-bar.js';
import { S } from '../state/store.js';

/* ── the readiness interview ────────────────────────────────────────── */

export function interviewSection(cap) {
  const iv = AI.interviews[cap.id];
  const answered = cap.interview;
  let inner;

  if (iv && iv.pending) {
    inner = '<div class="thinking">Working out what to ask…</div>';
  } else if (iv && iv.available === false) {
    inner = aiFailure(iv, 'The interview');
  } else if (iv) {
    inner = `
      <div style="font-size:12.5px;margin-bottom:4px"><b>${esc(iv.opening)}</b></div>
      <ul class="iv-q">
        ${iv.questions.map(q => `<li>
          <b>${esc(q.ask)}</b>
          <span>${esc(q.why_it_matters)}</span><br>
          <span>Listen for: <em>${esc(q.listen_for)}</em></span>
        </li>`).join('')}
      </ul>
      <div class="sheet-note" style="margin:0">
        <b>Cheapest unlock.</b> ${esc(iv.cheapest_unlock)}<br>
        <b>If every answer is no.</b> ${esc(iv.if_unavailable)}
      </div>
      <div class="iv-form">
        <textarea id="iv-${esc(cap.id)}" placeholder="What did they say? Type it as they said it."></textarea>
        <div class="row">
          <button class="gen-btn" data-answer="${esc(cap.id)}">Score this answer against the measurement</button>
        </div>
      </div>
      <div class="brief-meta" style="border:none;padding:7px 0 0">${genBadge(iv.model)}</div>`;
  } else {
    inner = `<button class="gen-btn" data-interview="${esc(cap.id)}">
      Prepare the questions for this row</button>`;
  }

  return `<div class="iv">
    ${inner}
    ${answered ? renderVerdict(cap, answered) : ''}
  </div>`;
}

export function renderVerdict(cap, iv) {
  const moved = iv.proposed_status && iv.proposed_status !== cap.status;
  return `<div class="verdict">
    <div class="verdict-h">
      <span class="status ${esc(cap.status)}">${esc(cap.status)}</span>
      ${iv.proposed_status ? `<span class="arrow">→</span>
        <span class="status ${esc(iv.proposed_status)}">${esc(iv.proposed_status)}</span>` : ''}
      ${moved ? '' : '<span style="color:var(--muted);font-size:11px">no change proposed</span>'}
      <span style="margin-left:auto;color:var(--muted);font-size:10.5px">
        ${esc((iv.answered_at || '').slice(0, 16).replace('T', ' '))}</span>
    </div>
    <div class="verdict-b">
      <b>They said.</b> ${esc(iv.answer)}<br><br>
      ${esc(iv.rationale || '')}
      ${iv.unlocks && iv.unlocks.length ? `<br><br><b>Unlocks.</b> ${iv.unlocks.map(esc).join('; ')}` : ''}
      ${iv.next_ask ? `<br><br><b>Next ask.</b> ${esc(iv.next_ask)}` : ''}
    </div>
  </div>`;
}

export async function loadInterview(capId) {
  AI.interviews[capId] = { pending: true };
  renderReadinessSheet();
  try {
    AI.interviews[capId] = await getJSON(
      '/gis/readiness/interview?capability=' + encodeURIComponent(capId));
  } catch (e) {
    AI.interviews[capId] = { available: false, reason: e.message };
  }
  renderReadinessSheet();
}

export async function submitAnswer(capId, btn) {
  const box = document.getElementById('iv-' + capId);
  const answer = box && box.value.trim();
  if (!answer) { if (box) box.focus(); return; }
  if (btn) { btn.disabled = true; btn.textContent = 'Scoring…'; }
  try {
    const out = await getJSON('/gis/readiness/interview', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ capability: capId, answer }),
    });
    // The register is the source of truth for what was recorded, including
    // whether the proposal was withheld, so re-read it rather than trusting
    // the response we just posted.
    S.readiness = await getJSON('/gis/readiness');
    renderReadinessButton();
    if (out.available === false && !out.recorded) {
      alert('The answer was not recorded: ' + (out.reason || 'unknown error'));
    }
  } catch (e) {
    alert('Could not record that answer: ' + e.message);
  }
  renderReadinessSheet();
}

export function renderReadinessSheet() {
  const body = document.getElementById('sheet-body');
  if (!body || body.dataset.view !== 'readiness') return;
  body.innerHTML = S.readiness.capabilities.map(c => `
    <div class="cap">
      <div class="cap-h">
        <h4>${esc(c.capability)}</h4>
        <span class="status ${esc(c.status)}">${esc(c.status)}</span>
      </div>
      <div class="needs">Needs: ${esc(c.needs)}</div>
      <dl>
        <dt>Evidence</dt><dd>${esc(c.evidence)}</dd>
        <dt>Without it</dt><dd>${esc(c.degrades_to)}</dd>
        <dt>The ask</dt><dd class="ask">${esc(c.ask)}</dd>
      </dl>
      ${interviewSection(c)}
    </div>`).join('');

  body.querySelectorAll('[data-interview]').forEach(b => {
    b.onclick = () => loadInterview(b.dataset.interview);
  });
  body.querySelectorAll('[data-answer]').forEach(b => {
    b.onclick = () => submitAnswer(b.dataset.answer, b);
  });
}

