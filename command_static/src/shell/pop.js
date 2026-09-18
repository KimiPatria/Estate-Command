/* A small popover, anchored under a button.
 *
 * Both context pickers and the metric menu want the same behaviour: open on
 * click, close on outside click or Escape, and never scroll off screen. One
 * implementation keeps them consistent and keeps the z-index story short.
 */
let openPop = null;

function place(pop, anchor) {
  const a = anchor.getBoundingClientRect();
  pop.style.visibility = 'hidden';
  pop.hidden = false;
  // Measure with the cap lifted, or a tall menu reports its clamped height and
  // the flip decision is made on the wrong number.
  pop.style.maxHeight = '';
  const p = pop.getBoundingClientRect();

  // Prefer left-aligned under the anchor, but keep it fully on screen.
  const left = Math.max(8, Math.min(a.left, window.innerWidth - p.width - 8));
  pop.style.left = `${left}px`;

  // Flip above when there is no room below. The selection tray sits at the
  // foot of the map, so its menu always opens upward.
  const below = window.innerHeight - a.bottom - 12;
  const above = a.top - 12;
  if (p.height <= below || below >= above) {
    pop.style.top = `${a.bottom + 6}px`;
    pop.style.maxHeight = `${below}px`;
  } else {
    pop.style.top = `${Math.max(8, a.top - 6 - Math.min(p.height, above))}px`;
    pop.style.maxHeight = `${above}px`;
  }
  pop.style.visibility = '';
}

export function closePop() {
  if (!openPop) return;
  const { pop, anchor } = openPop;
  pop.hidden = true;
  if (anchor) anchor.setAttribute('aria-expanded', 'false');
  openPop = null;
}

export function showPop(pop, anchor, html, wire) {
  const again = openPop && openPop.pop === pop;
  closePop();
  if (again) return;              // clicking the same button shuts it
  if (html !== undefined) pop.innerHTML = html;
  place(pop, anchor);
  if (anchor) anchor.setAttribute('aria-expanded', 'true');
  openPop = { pop, anchor };
  if (wire) wire(pop);
}

export function popIsOpen() { return !!openPop; }

document.addEventListener('pointerdown', e => {
  if (!openPop) return;
  if (openPop.pop.contains(e.target) || (openPop.anchor && openPop.anchor.contains(e.target))) return;
  closePop();
});
document.addEventListener('keydown', e => { if (e.key === 'Escape') closePop(); });
window.addEventListener('resize', closePop);
