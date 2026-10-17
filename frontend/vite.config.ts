import http from 'node:http';
import tailwindcss from '@tailwindcss/vite';
import react from '@vitejs/plugin-react';
import { configDefaults, defineConfig } from 'vitest/config';

export default defineConfig({
  plugins: [
    react(),
    tailwindcss(),
    {
      // Node closes idle keep-alive sockets after 5s. Chrome then reuses the
      // half-closed socket and every API call after a pause stalls ~6s — which
      // looked like an empty dashboard during local reviews. Keep sockets alive
      // longer than the browser does.
      name: 'keep-alive',
      configureServer(server) {
        const httpServer = server.httpServer;
        if (httpServer && 'keepAliveTimeout' in httpServer) {
          (httpServer as http.Server).keepAliveTimeout = 65_000;
        }
      },
    },
  ],
  server: {
    host: '0.0.0.0',
    allowedHosts: ['.e2b.app'],
    proxy: {
      // The browser always uses relative /api URLs, never a sandbox-local hostname.
      '/api': {
        target: process.env.API_PROXY_TARGET ?? 'http://127.0.0.1:8000',
        ws: true,
      },
    },
  },
  // `vite preview` serves the production bundle used for reviews; it needs the
  // same API proxy because the browser only ever calls same-origin `/api`.
  preview: {
    host: '0.0.0.0',
    allowedHosts: ['.e2b.app'],
    proxy: {
      '/api': {
        target: process.env.API_PROXY_TARGET ?? 'http://127.0.0.1:8000',
        ws: true,
      },
    },
  },
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test-setup.ts'],
    exclude: [...configDefaults.exclude, 'e2e/**'],
  },
});
