import { pct } from '../lib/fmt.js';

/* ── intro curtain ──────────────────────────────────────────────────── */
/* Two clocks race each other: the real one (map style, estate index,
   readiness scan, fire feed) and a floor of INTRO_MIN, so a warm cache
   cannot flash the sequence and blink out mid-word. The later one lifts it. */
export const INTRO_MIN = 2700;
export const INTRO_MAX = 9000;
export const introStart = performance.now();
export let introLifted = false;

export function introStep(pct, label) {
  const fill = document.getElementById('intro-fill');
  const status = document.getElementById('intro-status');
  if (fill) fill.style.width = Math.max(4, Math.min(100, pct)) + '%';
  if (status && label) status.textContent = label;
}

export function dismissIntro() {
  if (introLifted) return;
  introLifted = true;
  introStep(100, 'Ready');
  const wait = Math.max(0, INTRO_MIN - (performance.now() - introStart));
  setTimeout(() => {
    const el = document.getElementById('intro');
    if (!el) return;
    el.classList.add('done');
    setTimeout(() => { el.hidden = true; }, 1000);
  }, wait);
}

/* A curtain that never lifts is worse than a half-drawn map, so boot gets a
   hard ceiling no matter what is still in flight behind it. */
setTimeout(dismissIntro, INTRO_MAX);

/* Zoom-as-scale-change. The unit of analysis shifts with altitude, which is
   how one product serves a plantation master and a CEO without a config
   engine: same map, different height. */
