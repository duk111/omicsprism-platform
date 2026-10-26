import { defineConfig, devices } from "@playwright/test";
const python = process.platform === "win32" ? ".venv\\Scripts\\python.exe" : ".venv/bin/python";

export default defineConfig({
  testDir: "./e2e",
  testMatch: "result-copilot.spec.ts",
  outputDir: "./test-results/pr10",
  reporter: "line",
  workers: 1,
  use: { baseURL: "http://127.0.0.1:4179", trace: "retain-on-failure" },
  webServer: [
    { command: `${python} -m uvicorn backend.tests.pr10_server:app --host 127.0.0.1 --port 8011`, cwd: "..", url: "http://127.0.0.1:8011/openapi.json", timeout: 60000 },
    { command: "npm run dev -- --host 127.0.0.1 --port 4179", url: "http://127.0.0.1:4179", env: { VITE_API_PROXY_TARGET: "http://127.0.0.1:8011" } },
  ],
  projects: [
    { name: "desktop", use: { ...devices["Desktop Chrome"], viewport: { width: 1500, height: 1000 } } },
    { name: "mobile", use: { ...devices["Pixel 7"] } },
  ],
});
