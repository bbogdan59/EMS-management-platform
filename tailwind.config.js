/** @type {import('tailwindcss').Config} */
module.exports = {
  darkMode: "class",
  content: ["./app/web/templates/**/*.html", "./app/web/static/js/**/*.js"],
  theme: {
    extend: {
      fontFamily: { sans: ["Manrope", "ui-sans-serif", "system-ui", "sans-serif"] },
      colors: {
        gray: { 50: "#f5f7f2", 100: "#edf1e9", 200: "#e0e7db", 300: "#cbd5c5", 400: "#96a38f", 500: "#74816e", 600: "#596c55", 700: "#3d543b", 800: "#2a3d2c", 900: "#1b2c23", 950: "#101e17" },
        brand: {
          50: "#eff9f1",
          100: "#d7f0dd",
          200: "#aee0bb",
          300: "#7ecb95",
          400: "#4fb072",
          500: "#2f9354",
          600: "#217642",
          700: "#1c5e37",
          800: "#194b2e",
          900: "#153f27",
        },
      },
    },
  },
  plugins: [],
};
