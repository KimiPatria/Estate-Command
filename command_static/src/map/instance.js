/* The MapLibre instance, as a live binding.
 *
 * Almost every module needs the map and exactly one assigns it, so it lives
 * alone here rather than being threaded through every call. Importers see the
 * assignment because ES module bindings are live; a plain export of the value
 * would freeze `null` into every consumer.
 */
export let map = null;

export function setMap(m) { map = m; }
