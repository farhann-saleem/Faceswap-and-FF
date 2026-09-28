import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { spawn } from "node:child_process";

const here = path.dirname(fileURLToPath(import.meta.url));
const plan = {
  version: 2, duration: 8, width: 1280, height: 720, fps: 30,
  design: { font_family: "Arial, sans-serif", font_family_display: "Georgia, serif", foreground: "#f7f3ea", accent: "#f2b84b", danger: "#ef6045", panel: "#080b10", safe_margin: 0.06, grain_opacity: 0.025 },
  scenes: [
    { id: "a", key: null, kind: "black", timeline_start: 0, duration: 4, source_start: 0, crop: null, transition: "zoom-blur", transition_duration: 0.5 },
    { id: "b", key: null, kind: "black", timeline_start: 4, duration: 4, source_start: 0, crop: null, transition: "film-burn", transition_duration: 0.4 }
  ],
  typography: [
    { id: "words", beat_id: "hook", start: 0.2, duration: 2.5, text: "How could a lake kill 1,746 people?", preset: "title", animation: "word-by-word", x: .5, y: .38, font_size: 64, color: "#f7f3ea", background_color: "#080b10", background_opacity: .2, emphasis_words: ["lake", "1,746"], timing_source: "estimated", callout: null, lower_third: null, word_timings: [] },
    { id: "counter", beat_id: "evidence", start: 2.8, duration: 1.2, text: "1,746 lives", preset: "fact", animation: "counter", x: .5, y: .5, font_size: 92, color: "#ffffff", background_color: "#080b10", background_opacity: .25, emphasis_words: [], timing_source: "estimated", callout: null, lower_third: null, word_timings: [] },
    { id: "map", beat_id: "context", start: 4.1, duration: 1.8, text: "Lake Nyos", preset: "map-label", animation: "map-callout", x: .5, y: .48, font_size: 36, color: "#ffffff", background_color: "#080b10", background_opacity: 0, emphasis_words: [], timing_source: "estimated", callout: { label: "LAKE NYOS", x: .48, y: .5, detail: "Northwest Cameroon" }, lower_third: null, word_timings: [] },
    { id: "lower", beat_id: "source", start: 6.05, duration: 1.7, text: "Archival interview", preset: "lower-third", animation: "lower-third", x: .08, y: .72, font_size: 42, color: "#ffffff", background_color: "#080b10", background_opacity: 0, emphasis_words: [], timing_source: "estimated", callout: null, lower_third: { eyebrow: "SOURCE", primary: "National Geographic", secondary: "Original documentary excerpt" }, word_timings: [] }
  ]
};
const planFile = path.join(here, ".smoke-plan.json"); const output = path.join(here, "feature-matrix-smoke.mp4");
await fs.writeFile(planFile, JSON.stringify(plan));
await new Promise((resolve, reject) => { const child = spawn(process.execPath, [path.join(here, "render.mjs"), planFile, output], { stdio: "inherit" }); child.on("exit", (code) => code === 0 ? resolve() : reject(new Error(`renderer exited ${code}`))); });
await fs.unlink(planFile);
