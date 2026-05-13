/*
  Lightweight Playwright screenshot runner for ETA operator surfaces.

  Usage:
    node eta_engine/scripts/playwright_capture.js <url> <outputPath> [selector] [waitMs]

  The script resolves Playwright from repo-local installs first so verification
  keeps working without depending on a global npm setup.
*/

const fs = require("fs");
const path = require("path");
const { createRequire } = require("module");

function resolvePlaywright() {
  const candidateRoots = [
    process.env.ETA_PLAYWRIGHT_NODE_MODULES,
    path.resolve(__dirname, "../../website/node_modules"),
    path.resolve(__dirname, "../../apps/app/node_modules"),
    path.resolve(__dirname, "../../apps/web/node_modules"),
  ].filter(Boolean);

  for (const root of candidateRoots) {
    try {
      const scopedRequire = createRequire(path.join(root, "package.json"));
      return scopedRequire("playwright");
    } catch (_) {
      // Try the next candidate root.
    }
  }

  try {
    return require("playwright");
  } catch (_) {
    throw new Error(
      "Playwright not found. Set ETA_PLAYWRIGHT_NODE_MODULES or install it under website/node_modules, apps/app/node_modules, or apps/web/node_modules.",
    );
  }
}

async function main() {
  const [, , urlArg, outArg, selectorArg, waitArg] = process.argv;
  const url = (urlArg || "").trim();
  const outFile = (outArg || "").trim();
  let selector = (selectorArg || "").trim();
  let waitMs = Number(waitArg || 0);

  if (!waitArg && selector && /^[0-9]+$/.test(selector)) {
    waitMs = Number(selector);
    selector = "";
  }

  if (!url || !outFile) {
    throw new Error("Usage: node eta_engine/scripts/playwright_capture.js <url> <outputPath> [selector] [waitMs]");
  }

  const { chromium } = resolvePlaywright();
  fs.mkdirSync(path.dirname(outFile), { recursive: true });

  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({
      viewport: { width: 1440, height: 1600 },
      deviceScaleFactor: 1,
    });
    await page.goto(url, { waitUntil: "networkidle", timeout: 90000 });
    if (selector) {
      await page.waitForSelector(selector, { timeout: 30000 });
    }
    if (Number.isFinite(waitMs) && waitMs > 0) {
      await page.waitForTimeout(waitMs);
    }
    await page.screenshot({ path: outFile, fullPage: true });
    process.stdout.write(outFile);
  } finally {
    await browser.close();
  }
}

main().catch((error) => {
  console.error(error && error.stack ? error.stack : String(error));
  process.exit(1);
});
