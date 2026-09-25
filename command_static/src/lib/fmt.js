export const fmt = (n, d = 0) => n === null || n === undefined || Number.isNaN(n)
  ? null : Number(n).toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d });


export const idr = n => n === null || n === undefined ? '—' : Number(n).toLocaleString('en-US');
export const sign = n => n > 0 ? `<span class="pos">+${idr(Math.round(n))}</span>`
                        : `<span class="neg">${idr(Math.round(n))}</span>`;


export const pct = n => n === null || n === undefined ? '—' : Number(n).toFixed(2) + '%';

/* Field-to-mill shrinkage. The validation block is not a footnote here: a
   detector run over generated data is worth nothing unless it can be scored
   against a known truth, so the score leads. */

export function row(label, value, unit) {
  const v = value === null || value === undefined
    ? '<dd class="na">not recorded</dd>'
    : `<dd>${value}${unit ? ` <span style="color:var(--muted);font-weight:400">${unit}</span>` : ''}</dd>`;
  return `<dt>${label}</dt>${v}`;
}


export const esc = s => String(s === null || s === undefined ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;');


/* The first sentence of a server summary: the finding, without the working.
   A full stop only ends a sentence when a space and a capital or digit
   follow, so "1.59" and "3.0%" stay whole. */
export const firstSentence = s => {
  const t = String(s === null || s === undefined ? '' : s).trim();
  const m = t.match(/^.*?[.!?](?=\s+[A-Z0-9]|$)/);
  return m ? m[0] : t;
};
