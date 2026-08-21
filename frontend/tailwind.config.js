/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
  theme: {
    extend: {
      colors: {
        // Neutral slate scale for the shell, plus one brand accent.
        brand: {
          50: '#eef7ff',
          100: '#d9edff',
          200: '#bce0ff',
          300: '#8ecdff',
          400: '#59b0ff',
          500: '#328eff',
          600: '#1b6ef5',
          700: '#1458e1',
          800: '#1748b6',
          900: '#19408f',
        },
        // Semantic colours used consistently across charts, badges and states.
        risk: {
          low: '#0f9d58',
          medium: '#d9a300',
          high: '#e8710a',
          critical: '#d93025',
        },
      },
      fontFamily: {
        // System UI stack only: the app must render identically offline, so it
        // never depends on a webfont being fetchable.
        sans: [
          'Inter',
          'ui-sans-serif',
          'system-ui',
          '-apple-system',
          'BlinkMacSystemFont',
          'Segoe UI',
          'Roboto',
          'Helvetica Neue',
          'Arial',
          'sans-serif',
        ],
        mono: ['ui-monospace', 'SFMono-Regular', 'Menlo', 'Monaco', 'monospace'],
      },
      boxShadow: {
        card: '0 1px 2px 0 rgb(15 23 42 / 0.04), 0 1px 3px 0 rgb(15 23 42 / 0.06)',
      },
    },
  },
  plugins: [],
};
