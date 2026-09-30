// Renders the PNG app icons from inline SVG using Playwright's Chromium.
// Usage: node tools/render_icons.js   (needs the playwright package)
const path = require("path");
const { chromium } = require(process.env.PW_PATH || "playwright");

const pulse = (stroke) =>
  `<path d="M72 276h96l44-112 80 224 44-112h104" fill="none" stroke="#fff" stroke-width="${stroke}" stroke-linecap="round" stroke-linejoin="round"/>`;
const rounded = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512"><rect width="512" height="512" rx="112" fill="#2a78d6"/>${pulse(44)}</svg>`;
// Maskable / Apple: full-bleed square, artwork inside the 80 % safe zone.
const square = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512"><rect width="512" height="512" fill="#2a78d6"/><g transform="translate(76.8 76.8) scale(0.7)">${pulse(52)}</g></svg>`;

const outputs = [
  ["icon-192.png", 192, rounded],
  ["icon-512.png", 512, rounded],
  ["maskable-512.png", 512, square],
  ["apple-touch-icon.png", 180, square],
  ["badge-96.png", 96, `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">${pulse(56)}</svg>`],
];

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage();
  for (const [name, size, svg] of outputs) {
    await page.setViewportSize({ width: size, height: size });
    await page.setContent(`<html><body style="margin:0;background:transparent">${svg.replace("<svg ", `<svg width="${size}" height="${size}" `)}</body></html>`);
    await page.screenshot({ path: path.join(__dirname, "..", "app", "static", "icons", name), omitBackground: true });
  }
  await browser.close();
})();
