/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        bg: {
          primary: '#0f0f0f',
          secondary: '#1a1a1a',
          card: '#1e1e1e',
          elevated: '#242424',
        },
        accent: {
          DEFAULT: '#6366f1',
          hover: '#4f46e5',
          light: '#818cf8',
        },
        success: '#22c55e',
        warning: '#f59e0b',
        danger: '#ef4444',
        text: {
          primary: '#f5f5f5',
          secondary: '#a0a0a0',
          muted: '#6b6b6b',
        },
        border: {
          DEFAULT: '#2a2a2a',
          light: '#333333',
        },
      },
    },
  },
  plugins: [],
}
