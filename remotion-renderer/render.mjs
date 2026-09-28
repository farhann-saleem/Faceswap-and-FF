import { bundle } from "@remotion/bundler";
import { renderMedia, selectComposition, ensureBrowser } from "@remotion/renderer";
import path from "node:path";
import fs from "node:fs/promises";
import fsSync from "node:fs";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const [planFile, outputFile] = process.argv.slice(2);
if (!planFile || !outputFile) throw new Error("Usage: node render.mjs PLAN.json OUTPUT.mp4");
const inputProps = JSON.parse(await fs.readFile(path.resolve(planFile), "utf8"));
if (inputProps.version !== 2) throw new Error("composition.version must be 2");

function findBrowserExecutable() {
  const candidates = [
    process.env.PUPPETEER_EXECUTABLE_PATH,
    process.env.REMOTION_BROWSER_EXECUTABLE,
    process.env.CHROME_BIN,
    process.env.CHROMIUM_PATH,
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
  ].filter(Boolean);

  for (const candidate of candidates) {
    if (fsSync.existsSync(candidate)) {
      return candidate;
    }
  }
  return null;
}

let browserExecutable = findBrowserExecutable();
if (!browserExecutable) {
  try {
    const status = await ensureBrowser();
    if (status?.path && fsSync.existsSync(status.path)) {
      browserExecutable = status.path;
    }
  } catch (err) {
    // Rely on renderer default if ensureBrowser fails
  }
}

const serveUrl = await bundle({ entryPoint: path.join(here, "src/index.tsx"), onProgress: () => undefined });
const composition = await selectComposition({ serveUrl, id: "DocumentaryV2", inputProps });
await renderMedia({
  composition,
  serveUrl,
  codec: "h264",
  outputLocation: path.resolve(outputFile),
  inputProps,
  browserExecutable: browserExecutable || undefined,
  chromiumOptions: {
    disableWebSecurity: false,
    headless: true,
  },
  concurrency: 2,
});
process.stdout.write(JSON.stringify({ ok: true, output: path.resolve(outputFile), frames: composition.durationInFrames }) + "\n");

