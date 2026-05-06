import path from 'node:path';

import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

const repoRoot = path.resolve(__dirname, '..');

export default defineConfig(({ command }) => ({
  root: __dirname,
  base: command === 'build' ? '/react/' : '/',
  plugins: [react()],
  publicDir: false,
  resolve: {
    alias: {
      '@': path.resolve(__dirname, 'src'),
    },
  },
  server: {
    port: 5174,
    fs: {
      allow: [repoRoot],
    },
    proxy: {
      '/api/proxy/sessions': {
        target: 'http://127.0.0.1:8787',
        changeOrigin: true,
      },
      '/api': {
        target: 'http://127.0.0.1:8765',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: path.resolve(__dirname, 'dist'),
    emptyOutDir: true,
  },
}));
