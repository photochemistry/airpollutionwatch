import { defineConfig, loadEnv } from 'vite'
import { svelte } from '@sveltejs/vite-plugin-svelte'

// https://vite.dev/config/
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  const apiBase = env.VITE_API_BASE_URL || 'https://andersan.net:8089'

  return {
    plugins: [svelte()],
    build: {
      rollupOptions: {
        output: {
          // Plotly.js を別チャンクに分離（地図・時系列で使用。本体が 2MB 超のため）
          manualChunks: (id) => {
            if (id.includes('plotly.js-dist-min')) return 'plotly'
            return undefined
          },
        },
      },
      chunkSizeWarningLimit: 2500, // Plotly チャンクが 500kB 超のため引き上げ（意図的な大型依存）
    },
    server: {
      proxy: {
        // 開発時: /api へのリクエストを API に転送（CORS を回避）
        '/api': {
          target: apiBase,
          changeOrigin: true,
          rewrite: (path) => path.replace(/^\/api/, ''),
        },
      },
    },
  }
})
