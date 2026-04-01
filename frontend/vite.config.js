import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

const ENV_PREFIXES = ['VITE_', 'SMART_FIND_'];

export default defineConfig(({ mode }) => {
  const rootDir = path.join(__dirname, '..');
  const env = {
    ...loadEnv(mode, rootDir, ENV_PREFIXES),
    ...loadEnv(mode, __dirname, ENV_PREFIXES),
  };
  const backendPort =
    env.VITE_BACKEND_PORT || env.SMART_FIND_API_PORT || '8000';

  /** true = listen on 0.0.0.0 (LAN, VPN, tunnels). Set VITE_DEV_SERVER_HOST=localhost to bind loopback only. */
  const devHostRaw = (env.VITE_DEV_SERVER_HOST || '').trim().toLowerCase();
  const devServerHost =
    devHostRaw === 'localhost' || devHostRaw === '127.0.0.1'
      ? 'localhost'
      : devHostRaw === 'false' || devHostRaw === '0'
        ? false
        : true;

  const devPort = Number(env.VITE_DEV_SERVER_PORT || env.VITE_PORT || 5173) || 5173;

  const apiProxy = {
    '/api': {
      target: `http://127.0.0.1:${backendPort}`,
      changeOrigin: true,
      rewrite: (p) => p.replace(/^\/api/, ''),
      timeout: 600_000,
      proxyTimeout: 600_000,
    },
    '/share': {
      target: `http://127.0.0.1:${backendPort}`,
      changeOrigin: true,
      timeout: 600_000,
      proxyTimeout: 600_000,
    },
  };

  return {
    plugins: [react()],
    define: {
      'import.meta.env.VITE_BACKEND_PORT': JSON.stringify(backendPort),
    },
    server: {
      host: devServerHost,
      port: devPort,
      strictPort: false,
      proxy: apiProxy,
    },
    preview: {
      host: devServerHost,
      port: Number(env.VITE_PREVIEW_PORT || 4173) || 4173,
      strictPort: false,
      proxy: apiProxy,
    },
  };
});
