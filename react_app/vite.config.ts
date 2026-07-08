import path from 'node:path';

import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

const repoRoot = path.resolve(__dirname, '..');
const devTargetHost = process.env.HASH_CONTEXT_HOST || 'localhost';

function readPort(name: string, fallback: number): number {
  const value = Number(process.env[name] || fallback);
  return Number.isInteger(value) && value > 0 && value < 65536 ? value : fallback;
}

const backendPort = readPort('HASH_WEB_PORT', 8765);
const proxyPort = readPort('HASH_CONTEXT_PROXY_PORT', 8787);

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
        target: `http://${devTargetHost}:${proxyPort}`,
        changeOrigin: true,
      },
      '/api': {
        target: `http://${devTargetHost}:${backendPort}`,
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: path.resolve(__dirname, 'dist'),
    emptyOutDir: true,
  },
}));
