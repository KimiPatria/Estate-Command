/* The block list, shared by every panel that names blocks.
 *
 * This is the piece that makes the map worth its share of the screen. Every
 * panel payload carries an array of blocks - worst_blocks, hardest_blocks,
 * queue, worst_segments, block_labels - and until now each one rendered as
 * inert text. A panel saying "these thirty blocks are worst" while the map
 * showed something unrelated was asking the reader to hold thirty labels in
 * their head.
 *
 * A list built here instead:
 *   - paints its blocks on the map when the panel opens
 *   - flashes the polygon when a row is hovered
 *   - flies to the block and opens it when a row is clicked
 *
 * Panels keep returning HTML strings, so adopting this is a per-panel change
 * of a few lines rather than a rewrite of the rendering model.
 */
import { esc } from '../lib/fmt.js';
import { blockIdFor, blockIdsFor } from '../map/blocks.js';
import { clearPanelHighlight, flashBlock, highlightPanel } from '../map/highlight.js';
import { flyToBlock, selectBlockById } from '../map/select.js';
import { replaceSelection } from '../state/selection.js';

/* Rows render as a table; `cols` describes what to show beside the label.
 *
 *   blockList(d.worst_blocks, {
 *     cols: [{ key: 'ganoderma_pct', label: 'Ganoderma', num: true, fmt: pct }],
 *   })
 */
export function blockList(rows, opts = {}) {
  if (!rows || !rows.length) return '';
  const { cols = [], label = 'Block', caption = '' } = opts;
  const ids = blockIdsFor(rows);

  const head = cols.map(c =>
    `<th class="${c.num ? 'num' : ''}">${esc(c.label)}</th>`).join('');

  const body = rows.map((r, i) => {
    const id = ids.length ? blockIdsFor([r])[0] : null;
    const name = r.block_label || r.block || r.block_code || r.label || '—';
    const cells = cols.map(c => {
      const v = r[c.key];
      const shown = c.fmt ? c.fmt(v, r) : (v === null || v === undefined ? '—' : v);
      return `<td class="${c.num ? 'num' : ''}">${c.html ? shown : esc(String(shown))}</td>`;
    }).join('');
    // A row the map cannot resolve still renders; it just does not point.
    return `<tr class="blk-row${id ? '' : ' unmapped'}"${id ? ` data-block="${esc(id)}"` : ''}>
      <td class="blk">${esc(String(name))}</td>${cells}</tr>`;
  }).join('');

  return `
    <table class="tbl blk-tbl" data-blocks="${esc(ids.join(','))}">
      <thead><tr><th>${esc(label)}</th>${head}</tr></thead>
      <tbody>${body}</tbody>
    </table>
    ${caption ? `<div class="blk-cap">${esc(caption)}</div>` : ''}`;
}

/* A bare list of block labels, for payloads that carry nothing but names
   (cluster groups, the copilot's focus, weak-on-both). */
export function blockChips(labels, opts = {}) {
  if (!labels || !labels.length) return '';
  const ids = blockIdsFor(labels);
  const chips = labels.map(l => {
    const id = blockIdsFor([l])[0];
    return `<button class="blk-chip${id ? '' : ' unmapped'}"
      ${id ? `data-block="${esc(id)}"` : ''}>${esc(String(l))}</button>`;
  }).join('');
  return `<div class="blk-chips" data-blocks="${esc(ids.join(','))}"
            ${opts.title ? `aria-label="${esc(opts.title)}"` : ''}>${chips}</div>`;
}

/* ── wiring ────────────────────────────────────────────────────────────── */

/* Called by the workbench after any panel paints, so every panel inherits the
   behaviour without opting in - including the nineteen that were written
   before this existed and still hand-roll their own tables.
 *
 * Block references are found rather than declared: the first cell of every
 * table row is looked up in the estate's block index, and a row is treated as
 * pointing at a block only if that lookup succeeds. Resolution is therefore
 * its own filter - a cell reading "33-56" is a block because EC has a block
 * called 33-56, and a cell reading "G4-02" or "2041" simply is not.
 *
 * Three things then happen:
 *   1. Everything the panel named is outlined on the map, in amber.
 *   2. Hovering a row flashes that one polygon white.
 *   3. Clicking a row frames the block and opens its detail.
 */
export function wireBlockRefs(root) {
  if (!root) return [];

  const ids = [];
  const seen = new Set();
  const add = id => { if (id && !seen.has(id)) { seen.add(id); ids.push(id); } };

  // Explicit lists, from panels built on blockList/blockChips.
  root.querySelectorAll('[data-blocks]').forEach(host => {
    (host.dataset.blocks || '').split(',').forEach(add);
  });

  // Found lists, from every panel that predates it.
  root.querySelectorAll('table.tbl tr').forEach(tr => {
    if (tr.querySelector('th')) return;                 // a header row
    if (tr.dataset.block) { add(tr.dataset.block); return; }
    // The block is not always the first column: the shrinkage panel is a trip
    // ledger, so its block sits third behind trip id and date. Take the first
    // cell that resolves, and look no further than the leading few.
    const cells = [...tr.querySelectorAll('td')].slice(0, 4);
    for (const cell of cells) {
      const id = blockIdFor(cell.textContent.trim());
      if (!id) continue;
      tr.dataset.block = id;
      tr.classList.add('blk-row');
      cell.classList.add('blk');
      add(id);
      return;
    }
  });

  highlightPanel(ids);

  root.querySelectorAll('[data-block]').forEach(el => {
    const id = el.dataset.block;
    el.addEventListener('mouseenter', () => flashBlock(id));
    el.addEventListener('mouseleave', () => flashBlock(null));
    el.addEventListener('click', ev => {
      // Let a button inside the row do its own job.
      if (ev.target.closest('button, a, input, label')) return;
      flashBlock(null);
      selectBlockById(id);
      flyToBlock(id);
    });
  });

  // Say what the amber means, once, above the first list that has any - and
  // offer the whole list as a working set, which is the cheapest way there is
  // to turn a panel's finding into something a person can act on.
  if (ids.length) {
    const first = root.querySelector('.blk-row');
    const table = first && first.closest('table');
    if (table && !root.querySelector('.blk-hint')) {
      const hint = document.createElement('div');
      hint.className = 'blk-hint';
      hint.innerHTML =
        `<span>${ids.length} block${ids.length === 1 ? '' : 's'} outlined on the map`
        + ` · hover a row to find it, click to open it</span>`
        + `<button class="blk-take" type="button">Select all ${ids.length}</button>`;
      hint.querySelector('.blk-take').onclick = () => replaceSelection(ids);
      table.parentNode.insertBefore(hint, table);
    }
  }

  return ids;
}

export function clearBlockRefs() {
  clearPanelHighlight();
  flashBlock(null);
}
