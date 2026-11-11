/* Shared by the forecast pages: icons, the light/dark switch (same storage key
   as the chat page, so the choice carries across pages), chart colours read
   from the theme, and the database picker. Plain script, no build. */
(function () {
  'use strict';
  const root = document.documentElement;

  const ICONS = {
    spark: '<path d="m12 3 1.9 5.6L19.5 10l-5.6 1.9L12 17.5l-1.9-5.6L4.5 10l5.6-1.4Z" fill="currentColor"/>',
    sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
    moon: '<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8Z"/>',
    database: '<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v14c0 1.7 3.6 3 8 3s8-1.3 8-3V5"/><path d="M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/>',
    check: '<path d="m5 12.5 4.5 4.5L19 7.5"/>',
    chevron: '<path d="m6 9 6 6 6-6"/>',
  };
  function ico(name) {
    return '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" '
      + 'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + (ICONS[name] || '') + '</svg>';
  }
  function store(key, value) {
    try {
      if (value === undefined) return localStorage.getItem(key);
      localStorage.setItem(key, value);
    } catch (e) { /* storage unavailable: the page works without it */ }
    return null;
  }
  function esc(s) {
    const d = document.createElement('div');
    d.textContent = s == null ? '' : String(s);
    return d.innerHTML;
  }

  /* ── colours ──────────────────────────────────────────────────────── */
  function css(name) { return getComputedStyle(root).getPropertyValue(name).trim(); }
  function rgb(hex) {
    let h = String(hex).trim().replace('#', '');
    if (h.length === 3) h = h.split('').map(c => c + c).join('');
    const n = parseInt(h, 16);
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
  }
  function alpha(hex, a) { const c = rgb(hex); return `rgba(${c[0]},${c[1]},${c[2]},${a})`; }
  function lum(c) {
    const f = c.map(v => { v /= 255; return v <= .03928 ? v / 12.92 : Math.pow((v + .055) / 1.055, 2.4); });
    return .2126 * f[0] + .7152 * f[1] + .0722 * f[2];
  }
  function contrast(a, b) {
    const la = lum(rgb(a)), lb = lum(rgb(b));
    return (Math.max(la, lb) + .05) / (Math.min(la, lb) + .05);
  }
  function toHex(c) { return '#' + c.map(v => Math.round(v).toString(16).padStart(2, '0')).join(''); }
  /** A line colour that stays readable on the current card surface: the same
      hue, moved toward the text colour only as far as 3:1 contrast needs.
      On the light theme the lab's colours pass through unchanged. */
  function legible(hex, min) {
    const surface = css('--surface') || '#ffffff';
    if (root.dataset.theme !== 'dark') return hex;
    const target = rgb(css('--text') || '#ffffff');
    let c = rgb(hex);
    for (let i = 0; i < 20 && contrast(toHex(c), surface) < (min || 3); i++) {
      c = c.map((v, k) => v + (target[k] - v) * .12);
    }
    return toHex(c);
  }
  /** Colours for Chart.js, read from the tokens each time a chart is drawn. */
  function chart() {
    const actual = css('--accent'), fc = css('--s4');
    return {
      actual, actualFill: alpha(actual, .08),
      fc, fcFill: alpha(fc, .14),
      under: css('--s2'),
      grid: css('--grid'), tick: css('--muted'),
      text: css('--text'), text2: css('--text-2'), muted: css('--muted'),
      surface: css('--surface'), border: css('--border'), divider: css('--border-2'),
    };
  }
  /** Tooltip colours shared by every chart on these pages. */
  function tooltip() {
    const c = chart();
    return { backgroundColor: c.surface, borderColor: c.border, borderWidth: 1,
             titleColor: c.text, bodyColor: c.text2, footerColor: c.text2, padding: 10 };
  }

  /* ── theme switch ─────────────────────────────────────────────────── */
  const listeners = [];
  function paintThemeBtn() {
    const b = document.getElementById('theme-btn');
    if (b) b.innerHTML = ico(root.dataset.theme === 'dark' ? 'sun' : 'moon');
  }
  function setTheme(t) {
    root.dataset.theme = t;
    store('chat-theme', t);
    paintThemeBtn();
    listeners.forEach(fn => { try { fn(t); } catch (e) { console.error('[theme]', e); } });
  }

  /* ── database picker ──────────────────────────────────────────────── */
  const DB_KEY = 'chat-db';              // shared with the chat page
  const db = { list: [], current: 'live', listeners: [] };
  function dbLabel() {
    const d = db.list.find(x => x.id === db.current);
    return d ? d.label : (db.current === 'staging' ? 'Staging' : 'Live estates');
  }
  function paintDbChip() {
    const chip = document.getElementById('db-chip');
    if (!chip) return;
    chip.innerHTML = ico('database') + '<span>' + esc(dbLabel()) + '</span><span class="chev">' + ico('chevron') + '</span>';
  }
  function closeDbPop() {
    const pop = document.getElementById('db-pop'), chip = document.getElementById('db-chip');
    if (pop) pop.hidden = true;
    if (chip) chip.setAttribute('aria-expanded', 'false');
  }
  function openDbPop() {
    const pop = document.getElementById('db-pop'), chip = document.getElementById('db-chip');
    if (!pop) return;
    pop.innerHTML = '<div class="pop-title">Database</div>' + db.list.map(d => {
      const desc = d.available
        ? (d.description || '') + (d.data_through ? (d.description ? ' · ' : '') + 'data to ' + d.data_through : '')
        : (d.reason || 'Not available on this server');
      return `<button type="button" class="opt${d.id === db.current ? ' on' : ''}" data-db="${esc(d.id)}"${d.available ? '' : ' disabled'}>
        <span class="opt-main"><span class="opt-label">${esc(d.label)}</span><span class="opt-desc">${esc(desc)}</span></span>
        <span class="tickmark">${ico('check')}</span></button>`;
    }).join('');
    pop.hidden = false;
    chip.setAttribute('aria-expanded', 'true');
    pop.querySelectorAll('.opt').forEach(b => b.addEventListener('click', () => {
      closeDbPop();
      setDb(b.dataset.db);
    }));
  }
  function setDb(id, silent) {
    if (!db.list.some(d => d.id === id && d.available) || id === db.current) return;
    db.current = id;
    store(DB_KEY, id);
    paintDbChip();
    if (!silent) db.listeners.forEach(fn => { try { fn(id); } catch (e) { console.error('[db]', e); } });
  }
  /** Load the sources, settle the starting one (?db= first, then the choice
      remembered from chat or these pages, then the server default) and wire
      the chip. Resolves to the starting source id. */
  async function initDb(url) {
    let wanted = null;
    try { wanted = new URLSearchParams(location.search).get('db'); } catch (e) { /* ignore */ }
    wanted = wanted || store(DB_KEY);
    try {
      const res = await fetch(url || '/forecast/sources');
      const body = await res.json();
      db.list = body.sources || [];
      const ok = id => db.list.some(d => d.id === id && d.available);
      db.current = ok(wanted) ? wanted : (ok(body.default) ? body.default : 'live');
    } catch (e) {
      db.list = [{ id: 'live', label: 'Live estates', available: true, description: '' }];
      db.current = 'live';
    }
    paintDbChip();
    const chip = document.getElementById('db-chip');
    if (chip) {
      chip.hidden = db.list.length < 2;
      chip.addEventListener('click', (e) => {
        e.stopPropagation();
        const pop = document.getElementById('db-pop');
        if (pop && pop.hidden) openDbPop(); else closeDbPop();
      });
      document.addEventListener('click', (e) => {
        const pop = document.getElementById('db-pop');
        if (pop && !pop.hidden && !pop.contains(e.target)) closeDbPop();
      });
      document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeDbPop(); });
    }
    return db.current;
  }

  function boot() {
    document.querySelectorAll('[data-ico]').forEach(n => { n.innerHTML = ico(n.dataset.ico) + n.innerHTML; });
    paintThemeBtn();
    const b = document.getElementById('theme-btn');
    if (b) b.addEventListener('click', () => setTheme(root.dataset.theme === 'dark' ? 'light' : 'dark'));
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();

  window.Theme = {
    ico, css, alpha, legible, chart, tooltip,
    get name() { return root.dataset.theme; },
    onChange(fn) { listeners.push(fn); },
    db: {
      init: initDb,
      get current() { return db.current; },
      get label() { return dbLabel(); },
      get list() { return db.list; },
      onChange(fn) { db.listeners.push(fn); },
    },
  };
})();
