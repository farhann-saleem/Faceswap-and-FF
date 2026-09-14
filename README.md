# FaceFusion + ffmpeg — CPU swap and stitch worker

RunPod serverless **CPU** worker with two jobs: replace a face in a still or a clip (FaceFusion 3.3.2), and
assemble ordered clips into one film (ffmpeg). No CUDA, no PyTorch, no GPU — on purpose.

One of four repositories in **Marketing Studio**, the 8x hiring assignment.

| | |
| --- | --- |
| **Role** | `op=swap` (face replacement) and `op=stitch` (template / timeline assembly) |
| **Runtime** | Python 3.11 slim, ffmpeg + libx264, FaceFusion **3.3.2**, `onnxruntime==1.22.0` (CPU wheel) |
| **Endpoint** | `rydclpv4ta6u4p` |
| **Upstream** | [facefusion/facefusion @ 3.3.2](https://github.com/facefusion/facefusion/tree/3.3.2) — pinned tag, never HEAD |
| **I/O** | **R2 object keys only.** Never bytes, never base64, never signed URLs. |
| **Status** | Still swap and video swap verified end to end. `op=stitch` is wired and unit-tested but has not had a product smoke. Timeline v1 is not deployed. |

## Why CPU

FaceFusion is ONNX Runtime and ffmpeg is ffmpeg. Neither gets enough from a GPU to justify GPU-hour pricing,
and both are long-running and I/O-bound — exactly the work that would hold an expensive card idle while it
shuffles frames. A 30-second 720p video swap takes ~456s here; the same wall time on a GPU endpoint would cost
roughly a hundred times more for no visible gain.

**Never attach a GPU to this endpoint.**

---

## Where this fits

```
Marketing Studio backend
   │
   ├── text → image  ──────────►  Krea-2-Turbo       (RunPod GPU)
   ├── image → image ──────────►  Qwen-and-QwenEdit  (RunPod GPU)
   ├── face swap / stitch ─────►  THIS WORKER        (RunPod CPU)
   └── image → video  ─────────►  Modal LTX-2.5      (H200)
                                        │
                                  Cloudflare R2
                              models + product media
```

| Sibling | Repo |
| --- | --- |
| Application tier | [`farhann-saleem/sixeyes`](https://github.com/farhann-saleem/sixeyes) |
| Text → image | [`farhann-saleem/Krea-2-Turbo`](https://github.com/farhann-saleem/Krea-2-Turbo) |
| Image → image | [`farhann-saleem/Qwen-and-QwenEdit`](https://github.com/farhann-saleem/Qwen-and-QwenEdit) |

In the product, `op=swap` backs the Images, Videos and Effects desks via `POST /api/faceswaps`, and `op=stitch`
is the export path for the documentary timeline. User-facing copy calls this *prompt generation*; FaceFusion is
an internal implementation detail.

---

## API

Three ops: `ping`, `swap`, `stitch`. `op` defaults to `ping`.

Everything flows through R2. `source_key`, `target_face_key`, `clip_keys` and `audio_key` are **object keys**;
the response carries an `output_key`, never media. A value that looks like a URL, an absolute path, a `data:`
payload, or that contains control characters is rejected, as is anything over 1024 bytes.

### `ping` — readiness

Run this first, with the gate off:

```json
{"input":{"op":"ping"}}
```

```json
{
  "ok": true,
  "worker": "cpu-swap-stitch",
  "ffmpeg": true,
  "facefusion_ready": true,
  "volume_mounted": true,
  "free_gb": 412.7,
  "allow_generate": true,
  "init_error": null,
  "build": "cpu-v1",
  "worker_id": "ek31s3i4ilca5y"
}
```

`ok` only means the handler responded. Read the individual fields:

| Field | What a bad value means |
| --- | --- |
| `ffmpeg` | `false` → ffmpeg is not on PATH; stitch and video normalisation cannot run |
| `facefusion_ready` | `false` → models did not initialise. `swap` will fail until configuration is fixed **and workers are restarted**. There is no per-request retry of heavy init. |
| `volume_mounted` | `false` → no network volume. `swap` refuses outright rather than filling a ~5 GB container disk. |
| `free_gb` | must exceed models **plus uncompressed frames** — size for raw PNG frames, not compressed input size |
| `allow_generate` | `false` → gate is off; only `ping` works |
| `init_error` | the exact `TypeName: message` from a failed init |
| `build` / `worker_id` | how you prove a rebuild actually landed |

With the gate off, startup imports no FaceFusion models and makes no R2 requests at all.

### `swap` — replace a face

```json
{"input":{
  "op":"swap",
  "source_key":"inputs/scene.mp4",
  "target_face_key":"avatars/person.png"
}}
```

Direction matters and is the opposite of what the names suggest at first read:

- `source_key` — the **scene** being edited (FaceFusion's `target_path`). Image or video.
- `target_face_key` — the **replacement identity** (FaceFusion's `source_paths`). Image only.

| Aspect | Behaviour |
| --- | --- |
| Accepted scenes | `.png` `.jpg` `.jpeg` `.webp` `.mp4` `.mov` `.mkv` `.webm` |
| Accepted faces | `.png` `.jpg` `.jpeg` `.webp` |
| Face selection | the largest detected face per frame (`one`, `large-small`). **Not** temporal identity tracking for multi-person scenes. |
| Video output | MP4, original audio preserved when present |
| Image output | keeps its extension |
| Non-MP4 or odd dimensions | normalised first: padded to even, re-encoded H.264 / yuv420p |
| No face in the identity image | processing error |
| Content checks | upstream FaceFusion checks stay enabled |

FaceFusion needs a reasonably front-facing face. Looking-down, profile and back-of-head frames barely change —
that is the model, not a bug.

### `stitch` — assemble clips

```json
{"input":{
  "op":"stitch",
  "clip_keys":["clips/a.mp4","clips/b.mp4"],
  "audio_key":"audio/music.wav",
  "width":1280,
  "height":720,
  "fps":30
}}
```

| Param | Default | Rules |
| --- | --- | --- |
| `clip_keys` | **required** | 1–100 ordered video keys. Alias: `clips`. |
| `audio_key` | none | optional replacement soundtrack |
| `width` | `1280` | even integer, 2–3840 |
| `height` | `720` | even integer, 2–3840 |
| `fps` | `30` | finite number, 1–60; fractional allowed |

What it does, per clip: fit within the canvas with black padding, `setsar=1`, constant fps, H.264 / yuv420p /
`crf 20` / `preset veryfast`, stereo 48 kHz AAC. Clips with no audio get generated silence so the concat
demuxer never sees a stream mismatch. Segments are then concatenated with `-c copy` and `+faststart`.

An `audio_key` replaces the whole audio track — padded if short, trimmed if long, and the **video is never
truncated**. Assembly uses hard cuts; there are no transitions.

### Response

```json
{
  "ok": true,
  "worker": "cpu-swap-stitch",
  "op": "stitch",
  "output_key": "outputs/cpu/stitch/<uuid>.mp4",
  "width": 1280, "height": 720, "fps": 30, "clip_count": 2,
  "duration_ms": 1234,
  "estimated_usd": 0.0001,
  "usd_per_hour_assumed": 0.3,
  "build": "cpu-v1",
  "worker_id": "ek31s3i4ilca5y"
}
```

Failures return the same envelope with `ok: false` and `error`, and are still metered. Output keys are unique
and carry a guessed MIME type.

`estimated_usd` covers **request wall time including I/O** — not cold initialisation, not idle time, not
network-volume or storage charges. Configure the real `RUNPOD_CPU_USD_PER_HR`; the default `0` means
**unconfigured, not free compute.**

---

## Measured performance

| Job | Measured |
| --- | --- |
| Still swap, warm | **6.2–8.1s** `duration_ms` across 7 catalog jobs |
| Still swap, end to end | ~10–20s wall — the wake/ping and R2 round trips sit outside `duration_ms` |
| Video swap, 15s 720×1280 | **133.7s** (job `1fab1e69`), output ~17 MB `video/mp4` |
| Video swap, 30s 720p | **~456s**, audio preserved |
| Effects clips | ~7s, h264 720 |
| `estimated_usd` | **0** — the hourly rate is unset |

A 4K 30s source was deliberately downscaled to 720p before swapping: FaceFusion extracts raw PNG frames, and
the reserve for 4K frames is enormous. The handler computes that reserve as
`width × height × 3 × ceil(duration × fps) × 1.1` and refuses the job if the volume cannot hold it.

---

## Models and R2

Runtime secrets match the application repo's `.env`: `R2_ENDPOINT`, `R2_ACCOUNT_ID`, `R2_ACCESS_KEY`,
`R2_SECRET_KEY`, `R2_BUCKET`. **No secrets are copied into the image.** `R2_BUCKET_NAME` is a
higher-priority alias. Media uses that bucket; models come from `R2_MODELS_BUCKET` (default `comfy`).

The adapter derives its catalog from the **pinned upstream** rather than guessing filenames. FaceFusion 3.3.2
needs these `.onnx` files plus their matching upstream `.hash` sidecars at
`comfy-models/facefusion/3.3.2/`:

```text
2dfan4.onnx              fan_68_5.onnx        nsfw_2.onnx
arcface_w600k_r50.onnx   inswapper_128.onnx   nsfw_3.onnx
bisenet_resnet_34.onnx   kim_vocal_2.onnx     xseg_1.onnx
fairface.onnx            nsfw_1.onnx          yoloface_8n.onnx
```

Each `.hash` is the upstream **CRC32** sidecar (e.g. `inswapper_128.hash`), not SHA256. The mask and voice
assets are required by the standard upstream common pre-check even though this worker only selects the face
swapper.

> A read-only inventory on 2026-09-12 found **no FaceFusion assets in `comfy`**. Stage them before enabling
> swap, or let the worker's first enabled boot fetch and then seed them.

Startup sequence when `ALLOW_GENERATE=1`:

1. Require a **real mount** at `/runpod-volume`, then symlink FaceFusion's `.assets/models/` to
   `/runpod-volume/facefusion-models/` so downloads persist across workers.
2. Pre-seed from R2 — anything already cached at the right size is skipped.
3. Warm FaceFusion, which fetches anything still missing from upstream and validates checksums.
4. Seed R2 with any newly acquired files so the next worker skips upstream entirely.
5. Load every inference session **and** the swapper ONNX initializer.
6. Only then call `serverless.start()`.

A shared-volume `fcntl` file lock plus atomic rename protect simultaneous initialisations. Sessions persist
between serial jobs using FaceFusion's `tolerant` memory mode. No model downloads happen at image build time.

Stitch stays fully usable without any models. Job directories are unique and cleaned up on success and failure.

---

## Build and test

```bash
docker build --build-arg WORKER_BUILD=cpu-v1 -t ms-runpod-cpu:local .

# ping with the gate off
docker run --rm ms-runpod-cpu:local \
  python /app/handler.py --test_input '{"input":{"op":"ping"}}'

# the full suite — no R2 credentials and no models required
docker run --rm -v "$PWD/tests:/tests:ro" -e PYTHONPATH=/app ms-runpod-cpu:local \
  python -m unittest discover -s /tests -v
```

The build validates itself before it finishes: `pip check` for dependency consistency, an assertion that
`CPUExecutionProvider` is available and `CUDAExecutionProvider` is **not**, and a load of
`FaceFusionCPU().model_files()` that exercises the real upstream parser and catalog **without downloading
weights**.

`tests/test_worker.py` covers:

| Test | Guarantee |
| --- | --- |
| `test_ping_and_gate_do_not_touch_r2` | gate-off ping makes zero R2 calls |
| `test_validation_and_failures` | key/parameter validation and the error envelope |
| `test_stitch_mixed_sizes_rates_and_audio` | mixed resolutions, frame rates and missing audio streams normalise |
| `test_short_soundtrack_does_not_truncate_video` | a short `audio_key` pads instead of cutting the film |
| `test_swap_direction_and_cleanup` | `source_key` is the scene, `target_face_key` is the identity; temp dirs are removed |
| `test_atomic_model_cache_and_corruption_repair` | a corrupt cached model is re-fetched |
| `test_missing_volume_refuses_model_downloads` | no volume → refuse, do not fill the container disk |
| `test_partial_download_is_removed` | `.part` files never survive a failure |
| `test_upload_failure_returns_no_output_key_and_cleans_temp` | no phantom `output_key` on upload failure |
| `test_pinned_cpu_arguments_and_catalog` | the real pinned parser accepts our CPU/swapper/mask/encoder enums |
| `test_actual_onnx_cpu_inference` | ONNX Runtime really executes on CPU |

---

## Deploy to RunPod

Set endpoint runtime secrets from [`.env.example`](.env.example). Never bake `.env` into the image. Use an
immutable registry tag or digest, and a **distinct `WORKER_BUILD` per deployment** so `ping` can prove which
image is answering.

| Setting | Value | Why |
| --- | --- | --- |
| Worker type | **CPU**, with RAM for all resident ONNX sessions | never attach a GPU |
| Network volume | attached, endpoint pinned to **its datacenter only** | cross-DC mounts fail silently — workers sit in `Initializing` with no logs |
| Concurrency | **1** | FaceFusion uses process-global state; jobs are serialised by a lock anyway |
| Execution timeout | ≥1800s to start | this is the overall hard bound; FaceFusion runs in-process |
| `FFMPEG_TIMEOUT_SECONDS` | 1800 | per-ffmpeg-subprocess limit |
| Idle timeout | long while debugging, then tuned for model startup cost | model init is expensive |
| FlashBoot | on if available | still recycle workers after a rebuild |
| Invocation | async `/run`, poll `/status/{id}` | CPU video work is far too long for `/runsync` |

After an image update: idle 5s, drain and purge queued test jobs, stop old workers, then **verify `worker_id`
and `build` changed**.

Endpoint provisioning and model staging are deployment steps. This repo creates no endpoints and uploads no
model assets.

---

## Environment variables

Names only. Values go on the endpoint.

| Name | Default | Purpose |
| --- | --- | --- |
| `ALLOW_GENERATE` | `0` | gate. `0` → only `ping`; no model init, no R2 calls. |
| `R2_ENDPOINT` | — | `https://<account-id>.r2.cloudflarestorage.com` |
| `R2_ACCOUNT_ID` | — | used to derive the endpoint when `R2_ENDPOINT` is absent |
| `R2_ACCESS_KEY` / `R2_SECRET_KEY` | — | R2 S3-compatible credentials |
| `R2_BUCKET` | `comfy` | media bucket. `R2_BUCKET_NAME` takes precedence. |
| `R2_MODELS_BUCKET` | `comfy` | models always come from here |
| `R2_FACEFUSION_PREFIX` | `comfy-models/facefusion/3.3.2` | model prefix |
| `R2_OUTPUT_PREFIX` | `outputs/cpu` | where `output_key` lands |
| `RUNPOD_CPU_USD_PER_HR` | `0` | **0 means unconfigured, not free** |
| `CPU_THREADS` | `4` | ffmpeg `-filter_threads` and encoder threads |
| `FFMPEG_TIMEOUT_SECONDS` | `1800` | per-subprocess ceiling |
| `MAX_INPUT_BYTES` | `2147483648` | rejects oversized R2 inputs (2 GB) |
| `DISK_RESERVE_BYTES` | `1073741824` | headroom kept free at all times (1 GB) |
| `WORKER_BUILD` | `cpu-v1` | build arg, echoed by `ping` |

---

## The eleven GPU lessons, applied here

The GPU workers paid for these. They were applied to this worker before it was first deployed rather than
rediscovered. Full write-ups live in the app repo at
[`docs/RUNPOD.md`](https://github.com/farhann-saleem/sixeyes/blob/main/docs/RUNPOD.md).

| Lesson | Application here |
| --- | --- |
| 1 · Volume / datacenter | Attach `/runpod-volume`; select only its datacenter; verify `volume_mounted: true`. |
| 2 · Runtime compatibility | Match FaceFusion 3.3.2 and its `onnxruntime==1.22.0` pin. `pip check` plus a parser smoke test run during build. PyTorch and ComfyUI do not apply. |
| 3 · Compiler / JIT | No Triton, no CUDA, so no gcc is needed. Add build tools if a JIT dependency ever appears. |
| 4 · Canonical pinned repo | Clone `facefusion/facefusion` at tag `3.3.2`, never HEAD. |
| 5 · Crash throttling | Inspect ping and startup logs and fix the root cause before retrying. If platform backoff persists, recreate the endpoint, purge its queue, and let the backoff expire. |
| 6 · Heavy init | R2 caching, pre-checks and all CPU sessions load before RunPod accepts jobs. Gate-off ping skips them entirely. |
| 7 · Exact enums | The real pinned parser validates the CPU, swapper, selector, mask and encoder values at build time. |
| 8 · Exact inputs | The adapter uses parsed defaults and verified upstream argument names; identity/media direction is documented above. |
| 9 · Model-specific pipeline | Upstream `conditional_process()` with `face_swapper` — alignment, embeddings, masking, content checks and audio handling included. |
| 10 · Stale workers | After an image update: idle 5s, drain and purge, stop old workers, verify a changed `worker_id` and `build`. |
| 11 · Disk | Swap refuses a missing volume, verifies free space before downloads and frame extraction, and reports `free_gb`. |

---

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `facefusion_ready: false` with an `init_error` | model init failed at boot | fix configuration and **restart workers** — init is never retried per request |
| `swap` fails, `stitch` works | same as above | models are only needed by swap |
| `Insufficient disk at ...` | volume too small for models plus raw frames | downscale the input (720p, not 4K) or grow the volume |
| `/runpod-volume is not mounted` | empty directory, not a mount | attach a volume in the endpoint's datacenter, then stop old workers |
| `ALLOW_GENERATE is off` | the gate | set `ALLOW_GENERATE=1` and restart |
| RunPod says `COMPLETED` with an `output_key` but the app row is still `IN_PROGRESS` | the caller's R2 download hung | restart the backend and **resume the same job id**. Do not `/run` again. |
| Health shows more idle workers than expected | endpoint max workers drifted | the lock is max 1 |
| Face barely changes in some frames | profile / looking-down / back-of-head frames | FaceFusion needs a front-ish face; this is the model |

---

## Security

- No credentials in this repo. `.env.example` is names-only; `.gitignore` blocks `.env`, `*.pem`, `*.key`,
  `credentials.json`, `secrets.json` and `rclone.conf`. Nothing is baked into the image.
- The Dockerfile copies **only `handler.py` and `facefusion_cpu.py`**. Never `COPY .`.
- **Keys, not bytes.** Media enters and leaves as R2 object keys. The response never contains media bytes,
  base64, or a signed URL. `_key()` rejects URLs, absolute paths, `data:` payloads, control characters and
  anything over 1024 bytes; `MAX_INPUT_BYTES` caps object size.
- ffmpeg and ffprobe are invoked as argv lists with `-nostdin` — no shell. Only **generated** filenames
  (`segment-0.mp4`, …) enter the concat manifest, so a hostile object key can never reach ffmpeg's concat
  grammar.
- Downloads are size-verified against `head_object` and atomically renamed; partial `.part` files are always
  removed.
- Upstream FaceFusion content checks (`nsfw_*`) remain enabled.
- Errors are `TypeName: message` strings which can include an object key or path, but never a credential.

---

## Known gaps

- **`op=stitch` has had no product smoke.** It is wired and unit-tested; no real film has been compiled
  through it end to end.
- **Timeline v1 is not deployed.** The documentary Export path stays blocked until `ping` reports
  `timeline_version: 1`. The artifact lives in the app repo at `scripts/cpu-timeline/`.
- **`RUNPOD_CPU_USD_PER_HR` is unset**, so every `estimated_usd` reads `0`. That is not evidence of free
  compute.
- **`runpod==1.7.13` is pinned.** The house rule is `runpod>=1.10.1,<2`, because 1.7.11–1.10.0 corrupts job
  tracking on network-volume endpoints. This pin sits inside that range and should be raised.
- **No FaceFusion assets were found in the `comfy` bucket** at last inventory; the first enabled boot will
  fetch from upstream and then seed R2.
- **Single-face only.** The largest detected face per frame is replaced. No multi-person identity tracking.
- **Hard cuts only.** No transitions, no crossfades, no per-clip trim in `op=stitch`.
- **Serial by design.** Concurrency 1; FaceFusion's process-global state makes parallel jobs unsafe.
