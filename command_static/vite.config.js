/* Estate Command build.
 *
 * The page is served by FastAPI at /command and its assets from the
 * /command-static mount, so `base` has to match the mount or every hashed
 * asset URL resolves against the site root and 404s.
 *
 * In dev, Vite serves the page itself and proxies the API to the FastAPI
 * process on 8001, so `npm run dev` needs no change to the Python side.
 */
import { defineConfig } from 'vite';

const API = 'http://127.0.0.1:8001';

export default defineConfig({
  base: '/command-static/',
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    // A client demo is not the place to be reading minified frames, and the
    // bundle is small enough that the map tiles dominate load either way.
    sourcemap: true,
    rollupOptions: {
      output: {
        // maplibre is ~800 KB and changes only when we bump it; keeping it
        // out of the app chunk means editing a panel does not re-download it.
        manualChunks: { maplibre: ['maplibre-gl'] },
      },
    },
  },
  server: {
    port: 5173,
    proxy: Object.fromEntries(
      ['/gis', '/health'].map(p => [p, { target: API, changeOrigin: true }])
    ),
  },
});
