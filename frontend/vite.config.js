import { defineConfig } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

export default defineConfig({
  plugins: [svelte()],
  // Path relatif: build bisa dipasang di root maupun sub-path (/sentiment-analysis/)
  base: './',
  server: {
    // npm run dev: /api diteruskan ke container frontend (docker compose) yang meneruskannya ke backend
    proxy: {
      '/api': process.env.VITE_BACKEND_URL || 'http://localhost:8000',
    },
  },
});
