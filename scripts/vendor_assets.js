// Copiaza asset-urile JS din node_modules in app/web/static/js/vendor, ca sa
// fie servite local si sa nu depindem de niciun CDN in productie.
const fs = require("fs");
const path = require("path");

const root = path.resolve(__dirname, "..");
const destDir = path.join(root, "app", "web", "static", "js", "vendor");
fs.mkdirSync(destDir, { recursive: true });

const files = [
  ["node_modules/htmx.org/dist/htmx.min.js", "htmx.min.js"],
  ["node_modules/echarts/dist/echarts.min.js", "echarts.min.js"],
];

for (const [src, destName] of files) {
  const srcPath = path.join(root, src);
  const destPath = path.join(destDir, destName);
  fs.copyFileSync(srcPath, destPath);
  console.log(`vendored ${src} -> ${path.relative(root, destPath)}`);
}
