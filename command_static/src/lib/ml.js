/* The machine-learning mark: a small brain beside any feature whose output
 * comes out of a fitted model (the `ml` flag in gis/features.py). One glyph,
 * one colour, so it reads the same in the rail and in a panel's title. The
 * outline is Lucide's brain (ISC), trimmed to stay legible at 14px.
 *
 * The tooltip is a title attribute on the wrapper rather than an SVG <title>,
 * so the words stay out of the label's text when it is read or copied. */
export const ML_ICON = `<span class="ml-ico" title="Machine learning" role="img"
  aria-label="Machine learning"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
  stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
  <path d="M12 5a3 3 0 1 0-5.997.125 4 4 0 0 0-2.526 5.77 4 4 0 0 0 .556 6.588A4 4 0 1 0 12 18Z"/>
  <path d="M12 5a3 3 0 1 1 5.997.125 4 4 0 0 1 2.526 5.77 4 4 0 0 1-.556 6.588A4 4 0 1 1 12 18Z"/>
  <path d="M15 13a4.5 4.5 0 0 1-3-4 4.5 4.5 0 0 1-3 4"/></svg></span>`;
