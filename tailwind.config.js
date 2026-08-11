/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ["./index.html", "./assets/js/**/*.js"],
  theme: {
    extend: {
      fontFamily: {
        sans: ["Inter", "ui-sans-serif", "system-ui", "sans-serif"],
        mono: ["JetBrains Mono", "ui-monospace", "monospace"]
      },
      colors: {
        night: "#050810",
        panel: "#0a101d",
        kaspa: {
          300: "#7ee2d0",
          400: "#47cdb8",
          500: "#22b3a2",
          600: "#178f86"
        }
      },
      boxShadow: {
        glow: "0 0 45px rgba(34, 211, 238, 0.12)",
        card: "0 18px 55px rgba(0, 0, 0, 0.35)"
      }
    }
  },
  plugins: []
};
