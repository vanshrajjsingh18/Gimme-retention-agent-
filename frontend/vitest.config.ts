import react from '@vitejs/plugin-react';
import { defineConfig } from 'vitest/config';

export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/tests/setup.ts'],
    css: false,
    include: ['src/**/*.test.{ts,tsx}'],
    // Run somewhere that is neither UTC nor New Zealand. The timestamp tests
    // assert NZ output for UTC input; in a UTC container they would pass even
    // if the parsing were wrong, which is how a timezone bug reaches Auckland
    // unnoticed.
    env: { TZ: 'America/New_York' },
  },
});
