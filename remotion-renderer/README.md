# Documentary composition v2

This is the deterministic Remotion picture renderer for the documentary pipeline. It accepts only the declarative composition-v2 JSON contract. It never evaluates script text as code and never starts paid media generation.

Install and test locally:

```bash
npm install
npm run smoke
```

The smoke render exercises word-by-word type, a number counter, a map callout, a lower third, zoom blur, and film-burn transitions. The cloud worker must replace every scene `key` with a job-local HTTP `src`, render the muted picture here, then use the existing FFmpeg path for narration, original source audio, music, ambience, SFX, ducking, loudness, and final mux.
