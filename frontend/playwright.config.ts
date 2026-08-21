import { defineConfig, devices } from '@playwright/test';

/**
 * End-to-end tests run against an already-running stack:
 *   backend  http://127.0.0.1:8000
 *   frontend http://127.0.0.1:5173
 *
 * Start both with `npm run dev` and `uvicorn app.main:app` (or `make dev`)
 * before running `npm run test:e2e`.
 */
export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 60_000,
  expect: { timeout: 15_000 },
  reporter: [['list']],
  use: {
    baseURL: process.env.E2E_BASE_URL ?? 'http://127.0.0.1:5173',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    launchOptions: {
      executablePath: process.env.PLAYWRIGHT_CHROMIUM ?? '/opt/pw-browsers/chromium-1194/chrome-linux/chrome',
    },
  },
  projects: [
    { name: 'desktop', use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 900 } } },
  ],
});
