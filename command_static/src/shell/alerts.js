/* ── map messages ───────────────────────────────────────────────────── */
export function showMapMessage(title, body, ask) {
  const el = document.getElementById('map-msg');
  el.innerHTML = `<h4>${title}</h4><p>${body}</p>${ask ? `<p class="ask">${ask}</p>` : ''}`;
  el.hidden = false;
}
export function hideMapMessage() { document.getElementById('map-msg').hidden = true; }

/* ── alerts ─────────────────────────────────────────────────────────── */
/* The strip carries live operational alerts only (the fire). Data gaps live
   behind the Readiness button, not in a banner over the map. */
export function clearAlerts() {
  document.getElementById('alerts').innerHTML = '';
}

