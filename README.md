<div align="center">
  <h1 align="center">Faceswap-and-FF</h1>
  <p align="center"><i>Look + stitch for Marketing Studio</i></p>

  [![Python](https://img.shields.io/badge/Python-3.11-blue.svg?style=for-the-badge&logo=python&logoColor=white)](https://python.org)
  [![RunPod](https://img.shields.io/badge/RunPod-Serverless%20CPU-7B2FF7.svg?style=for-the-badge)](https://runpod.io)
  [![FaceFusion](https://img.shields.io/badge/FaceFusion-3.3.2-orange.svg?style=for-the-badge)](https://github.com/facefusion/facefusion)
</div>

---

> One face into a still or a clip. Ordered clips into one film. No GPU — on purpose.

**Faceswap-and-FF** is the CPU leg of [Marketing Studio](https://github.com/farhann-saleem/sixeyes). FaceFusion 3.3.2 (`inswapper_128`) replaces identity; ffmpeg assembles timelines. Media moves as **R2 object keys only** — never bytes, base64, or signed URLs.

A 30s 720p swap is ~456s wall here. Same wall time on a GPU would cost ~100× more for no visual win.

| | |
| --- | --- |
| **Endpoint** | `rydclpv4ta6u4p` |
| **Ops** | `ping` · `swap` · `stitch` |
| **Runtime** | FaceFusion 3.3.2 · onnxruntime CPU · ffmpeg |

**Never attach a GPU to this endpoint.**

---

## Architecture

1. **The Core** — Slim Python image. Models are not baked in; ONNX files live on R2 under `comfy-models/facefusion/3.3.2/`.
2. **The Gate** — `ALLOW_GENERATE=0` → ping only (no R2, no sessions). Gate on → mount check, ensure models, load sessions, then `serverless.start()`.
3. **The Delivery** — Response is always an `output_key`. Concurrency 1. Async `/run` + poll.

```
Marketing Studio backend ──► THIS WORKER (CPU)
   Images / Videos / Effects Generate  →  op=swap
   Documentary Assemble / Export       →  op=stitch
```

User-facing copy says *prompt generation*. FaceFusion is an implementation detail.

---

## One request

```json
{
  "input": {
    "op": "swap",
    "source_key": "inputs/scene.mp4",
    "target_face_key": "avatars/person.png"
  }
}
```

- `source_key` — the scene being edited  
- `target_face_key` — the replacement identity (image)

---

## Setup

Build, tests, deploy: **[SETUP.md](SETUP.md)** · Design: **[ARCHITECTURE.md](ARCHITECTURE.md)** · ONNX catalog: **[MODELS.md](MODELS.md)**

Siblings: [sixeyes](https://github.com/farhann-saleem/sixeyes) · [Krea-2-Turbo](https://github.com/farhann-saleem/Krea-2-Turbo) · [Qwen-and-QwenEdit](https://github.com/farhann-saleem/Qwen-and-QwenEdit)

<div align="center">
  <i>Identity in. Cut locked. Scale on CPU.</i>
</div>
