/** @type {import('tailwindcss').Config} */
module.exports = {
  darkMode: "class",
  content: ["./app/web/templates/**/*.html", "./app/web/static/js/app/**/*.js"],
  theme: {
    extend: {
      colors: {
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
