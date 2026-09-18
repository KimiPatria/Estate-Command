import { S } from '../state/store.js';

/* ── map messages ───────────────────────────────────────────────────── */
export function showMapMessage(title, body, ask) {
  const el = document.getElementById('map-msg');
  el.innerHTML = `<h4>${title}</h4><p>${body}</p>${ask ? `<p class="ask">${ask}</p>` : ''}`;
  el.hidden = false;
}
export function hideMapMessage() { document.getElementById('map-msg').hidden = true; }

/* ── alerts ─────────────────────────────────────────────────────────── */
/* Sourced from the readiness scan, not invented. The alert strip earns its
   place by carrying the things that would otherwise be a footnote. */
export function raiseAlerts() {
  const missing = S.readiness.capabilities.filter(c => c.status === 'unavailable');
  const synth = S.readiness.capabilities.filter(c => c.status === 'synthetic').length;
  const el = document.getElementById('alerts');
  el.innerHTML = `<div class="alert">
    <span class="sev">Gap</span>
    <div class="body">
      <b>${missing.length} capabilities have no data behind them, and ${synth} run on synthetic feeds.</b>
      <span class="meta">Including ${missing.slice(0, 2).map(m => m.capability.split(' (')[0]).join(' and ')}.
      Open the readiness panel to see what each one needs.</span>
    </div>
    <button class="x" aria-label="Dismiss">&times;</button>
  </div>`;
  el.querySelector('.x').onclick = () => el.innerHTML = '';
}

