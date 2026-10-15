import tailwindcss from '@tailwindcss/vite';
import react from '@vitejs/plugin-react';
import { defineConfig } from 'vitest/config';

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    host: '0.0.0.0',
    allowedHosts: ['.e2b.app'],
    proxy: {
      // The browser always uses relative /api URLs, never a sandbox-local hostname.
      '/api': { target: process.env.API_PROXY_TARGET ?? 'http://127.0.0.1:8000' },
    },
  },
  test: { environment: 'jsdom' },
});
