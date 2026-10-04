import react from '@vitejs/plugin-react';
import { defineConfig, loadEnv } from 'vite';
import { defineConfig as defineTestConfig } from 'vitest/config';

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), 'LLMAUTOTEL_');
  return defineTestConfig({
    plugins: [react()],
    server: {
      host: '127.0.0.1',
      proxy: {
        '/api': { target: env.LLMAUTOTEL_API_URL || 'http://127.0.0.1:8765' },
      },
    },
    test: {
      environment: 'jsdom',
      setupFiles: './src/test/setup.ts',
      clearMocks: true,
    },
  });
});
