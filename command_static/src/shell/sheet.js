import { renderReadinessSheet } from '../ai/readiness.js';
import { getJSON } from '../lib/api.js';
import { closeDrawer, drawerIsOpen } from './drawer.js';
import { closeWorkbench, workbenchIsOpen } from './workbench.js';
import { closePopup, popupIsOpen } from './popup.js';
import { renderReadinessButton } from './context-bar.js';
import { S } from '../state/store.js';

/* ── readiness sheet ────────────────────────────────────────────────── */
export const sheetBg = document.getElementById('sheet-bg');
document.getElementById('readiness-btn').onclick = async () => {
  const body = document.getElementById('sheet-body');
  document.querySelector('.sheet-h h2').textContent = 'Data readiness';
  document.querySelector('.sheet-h p').textContent =
    'What each capability needs, whether it exists, and what it degrades to without it. ' +
    'Measured against the two EPMS databases and the estate exports, not assumed. ' +
    'Each row can be turned into the questions worth asking the client, and their answer scored back against the measurement.';
  body.dataset.view = 'readiness';
  sheetBg.hidden = false;
  renderReadinessSheet();
  // Re-read in the background: a scene pulled or an answer recorded since boot
  // should show, and the panel must not wait on the round trip to open.
  try {
    S.readiness = await getJSON('/gis/readiness');
    renderReadinessButton();
    renderReadinessSheet();
  } catch (e) { /* the sheet already rendered from what boot loaded */ }
};
document.getElementById('sheet-x').onclick = () => sheetBg.hidden = true;
sheetBg.onclick = e => { if (e.target === sheetBg) sheetBg.hidden = true; };
/* Escape unwinds one surface at a time, outermost first: the modal sits over
   everything, then the block drawer, then the workbench. Closing all three at
   once would lose a panel the user spent a click opening. */
document.addEventListener('keydown', e => {
  if (e.key !== 'Escape') return;
  // A feature window sits over everything; a minimised one is out of the way
  // and is left for its own close button.
  if (popupIsOpen()) closePopup();
  else if (!sheetBg.hidden) sheetBg.hidden = true;
  else if (drawerIsOpen()) closeDrawer();
  else if (workbenchIsOpen()) closeWorkbench();
});
