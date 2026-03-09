// vite.config.ts
import { defineConfig, loadEnv } from "file:///home/ubuntu/github/airpollutionwatch-api/dashboard/node_modules/vite/dist/node/index.js";
import { svelte } from "file:///home/ubuntu/github/airpollutionwatch-api/dashboard/node_modules/@sveltejs/vite-plugin-svelte/src/index.js";
var vite_config_default = defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const apiBase = env.VITE_API_BASE_URL || "https://andersan.net:8089";
  return {
    plugins: [svelte()],
    build: {
      rollupOptions: {
        output: {
          // Plotly.js を別チャンクに分離（地図・時系列で使用。本体が 2MB 超のため）
          manualChunks: (id) => {
            if (id.includes("plotly.js-dist-min")) return "plotly";
            return void 0;
          }
        }
      },
      chunkSizeWarningLimit: 2500
      // Plotly チャンクが 500kB 超のため引き上げ（意図的な大型依存）
    },
    server: {
      proxy: {
        // 開発時: /api へのリクエストを API に転送（CORS を回避）
        "/api": {
          target: apiBase,
          changeOrigin: true,
          rewrite: (path) => path.replace(/^\/api/, "")
        }
      }
    }
  };
});
export {
  vite_config_default as default
};
//# sourceMappingURL=data:application/json;base64,ewogICJ2ZXJzaW9uIjogMywKICAic291cmNlcyI6IFsidml0ZS5jb25maWcudHMiXSwKICAic291cmNlc0NvbnRlbnQiOiBbImNvbnN0IF9fdml0ZV9pbmplY3RlZF9vcmlnaW5hbF9kaXJuYW1lID0gXCIvaG9tZS91YnVudHUvZ2l0aHViL2FpcnBvbGx1dGlvbndhdGNoLWFwaS9kYXNoYm9hcmRcIjtjb25zdCBfX3ZpdGVfaW5qZWN0ZWRfb3JpZ2luYWxfZmlsZW5hbWUgPSBcIi9ob21lL3VidW50dS9naXRodWIvYWlycG9sbHV0aW9ud2F0Y2gtYXBpL2Rhc2hib2FyZC92aXRlLmNvbmZpZy50c1wiO2NvbnN0IF9fdml0ZV9pbmplY3RlZF9vcmlnaW5hbF9pbXBvcnRfbWV0YV91cmwgPSBcImZpbGU6Ly8vaG9tZS91YnVudHUvZ2l0aHViL2FpcnBvbGx1dGlvbndhdGNoLWFwaS9kYXNoYm9hcmQvdml0ZS5jb25maWcudHNcIjtpbXBvcnQgeyBkZWZpbmVDb25maWcsIGxvYWRFbnYgfSBmcm9tICd2aXRlJ1xuaW1wb3J0IHsgc3ZlbHRlIH0gZnJvbSAnQHN2ZWx0ZWpzL3ZpdGUtcGx1Z2luLXN2ZWx0ZSdcblxuLy8gaHR0cHM6Ly92aXRlLmRldi9jb25maWcvXG5leHBvcnQgZGVmYXVsdCBkZWZpbmVDb25maWcoKHsgbW9kZSB9KSA9PiB7XG4gIGNvbnN0IGVudiA9IGxvYWRFbnYobW9kZSwgcHJvY2Vzcy5jd2QoKSwgJycpXG4gIGNvbnN0IGFwaUJhc2UgPSBlbnYuVklURV9BUElfQkFTRV9VUkwgfHwgJ2h0dHBzOi8vYW5kZXJzYW4ubmV0OjgwODknXG5cbiAgcmV0dXJuIHtcbiAgICBwbHVnaW5zOiBbc3ZlbHRlKCldLFxuICAgIGJ1aWxkOiB7XG4gICAgICByb2xsdXBPcHRpb25zOiB7XG4gICAgICAgIG91dHB1dDoge1xuICAgICAgICAgIC8vIFBsb3RseS5qcyBcdTMwOTJcdTUyMjVcdTMwQzFcdTMwRTNcdTMwRjNcdTMwQUZcdTMwNkJcdTUyMDZcdTk2RTJcdUZGMDhcdTU3MzBcdTU2RjNcdTMwRkJcdTY2NDJcdTdDRkJcdTUyMTdcdTMwNjdcdTRGN0ZcdTc1MjhcdTMwMDJcdTY3MkNcdTRGNTNcdTMwNEMgMk1CIFx1OEQ4NVx1MzA2RVx1MzA1Rlx1MzA4MVx1RkYwOVxuICAgICAgICAgIG1hbnVhbENodW5rczogKGlkKSA9PiB7XG4gICAgICAgICAgICBpZiAoaWQuaW5jbHVkZXMoJ3Bsb3RseS5qcy1kaXN0LW1pbicpKSByZXR1cm4gJ3Bsb3RseSdcbiAgICAgICAgICAgIHJldHVybiB1bmRlZmluZWRcbiAgICAgICAgICB9LFxuICAgICAgICB9LFxuICAgICAgfSxcbiAgICAgIGNodW5rU2l6ZVdhcm5pbmdMaW1pdDogMjUwMCwgLy8gUGxvdGx5IFx1MzBDMVx1MzBFM1x1MzBGM1x1MzBBRlx1MzA0QyA1MDBrQiBcdThEODVcdTMwNkVcdTMwNUZcdTMwODFcdTVGMTVcdTMwNERcdTRFMEFcdTMwNTJcdUZGMDhcdTYxMEZcdTU2RjNcdTc2ODRcdTMwNkFcdTU5MjdcdTU3OEJcdTRGOURcdTVCNThcdUZGMDlcbiAgICB9LFxuICAgIHNlcnZlcjoge1xuICAgICAgcHJveHk6IHtcbiAgICAgICAgLy8gXHU5NThCXHU3NjdBXHU2NjQyOiAvYXBpIFx1MzA3OFx1MzA2RVx1MzBFQVx1MzBBRlx1MzBBOFx1MzBCOVx1MzBDOFx1MzA5MiBBUEkgXHUzMDZCXHU4RUUyXHU5MDAxXHVGRjA4Q09SUyBcdTMwOTJcdTU2REVcdTkwN0ZcdUZGMDlcbiAgICAgICAgJy9hcGknOiB7XG4gICAgICAgICAgdGFyZ2V0OiBhcGlCYXNlLFxuICAgICAgICAgIGNoYW5nZU9yaWdpbjogdHJ1ZSxcbiAgICAgICAgICByZXdyaXRlOiAocGF0aCkgPT4gcGF0aC5yZXBsYWNlKC9eXFwvYXBpLywgJycpLFxuICAgICAgICB9LFxuICAgICAgfSxcbiAgICB9LFxuICB9XG59KVxuIl0sCiAgIm1hcHBpbmdzIjogIjtBQUEyVSxTQUFTLGNBQWMsZUFBZTtBQUNqWCxTQUFTLGNBQWM7QUFHdkIsSUFBTyxzQkFBUSxhQUFhLENBQUMsRUFBRSxLQUFLLE1BQU07QUFDeEMsUUFBTSxNQUFNLFFBQVEsTUFBTSxRQUFRLElBQUksR0FBRyxFQUFFO0FBQzNDLFFBQU0sVUFBVSxJQUFJLHFCQUFxQjtBQUV6QyxTQUFPO0FBQUEsSUFDTCxTQUFTLENBQUMsT0FBTyxDQUFDO0FBQUEsSUFDbEIsT0FBTztBQUFBLE1BQ0wsZUFBZTtBQUFBLFFBQ2IsUUFBUTtBQUFBO0FBQUEsVUFFTixjQUFjLENBQUMsT0FBTztBQUNwQixnQkFBSSxHQUFHLFNBQVMsb0JBQW9CLEVBQUcsUUFBTztBQUM5QyxtQkFBTztBQUFBLFVBQ1Q7QUFBQSxRQUNGO0FBQUEsTUFDRjtBQUFBLE1BQ0EsdUJBQXVCO0FBQUE7QUFBQSxJQUN6QjtBQUFBLElBQ0EsUUFBUTtBQUFBLE1BQ04sT0FBTztBQUFBO0FBQUEsUUFFTCxRQUFRO0FBQUEsVUFDTixRQUFRO0FBQUEsVUFDUixjQUFjO0FBQUEsVUFDZCxTQUFTLENBQUMsU0FBUyxLQUFLLFFBQVEsVUFBVSxFQUFFO0FBQUEsUUFDOUM7QUFBQSxNQUNGO0FBQUEsSUFDRjtBQUFBLEVBQ0Y7QUFDRixDQUFDOyIsCiAgIm5hbWVzIjogW10KfQo=
