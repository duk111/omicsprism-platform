import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const env = (globalThis as unknown as { process?: { env?: Record<string, string | undefined> } }).process?.env ?? {};

export default defineConfig({
  base: env.VITE_PUBLIC_BASE_PATH || "/",
  plugins: [react()],
  server: {
    proxy: {
      "/api": env.VITE_API_PROXY_TARGET || "http://127.0.0.1:8000"
    }
  }
});
