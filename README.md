# RunPod CPU swap + stitch worker

Build context is this directory. CPU only: Python 3.11, ffmpeg/libx264,
FaceFusion 3.3.2 and `onnxruntime==1.22.0` (the CPU package). No CUDA or PyTorch.
FaceFusion's pinned official parser, model catalog and processing pipeline are
used directly: https://github.com/facefusion/facefusion/tree/3.3.2

## API

First console test, with `ALLOW_GENERATE=0`:

```json
{"input":{"op":"ping"}}
```

Ping includes `ok`, `worker`, `ffmpeg`, `facefusion_ready`, `volume_mounted`,
`free_gb`, `allow_generate`, `init_error`, `build`, and `worker_id`.
`ok` means the handler is responding; check the individual readiness fields.
Gate-off startup imports no FaceFusion models and makes no R2 requests.

After configuring models and setting `ALLOW_GENERATE=1`:

```json
{"input":{"op":"swap","source_key":"inputs/scene.mp4","target_face_key":"avatars/person.png"}}
```

`source_key` is the scene image/video to edit. `target_face_key` is the replacement
identity image. These map to FaceFusion's `target_path` and `source_paths`,
respectively. The largest detected face in each frame is replaced (`one`,
`large-small`); this is not temporal identity tracking for multi-person scenes.
The upstream content checks remain enabled. PNG/JPG/JPEG/WebP images and
MP4/MOV/MKV/WebM videos are supported. Video output is MP4 with original audio
when available; images retain their extension. No face in the identity image
produces a processing error.

```json
{"input":{"op":"stitch","clip_keys":["clips/a.mp4","clips/b.mp4"],"audio_key":"audio/music.wav","width":1280,"height":720,"fps":30}}
```

`clip_keys` (or alias `clips`) accepts 1–100 ordered video keys. Audio is optional.
Defaults: 1280×720 at 30 fps. Width/height must be even integers from 2–3840;
fps may be fractional, from 1–60. Clips are fit within the canvas with black
padding, normalized to H.264/yuv420p, square pixels, constant fps, stereo 48 kHz
AAC, then concatenated. Existing clip audio is retained and silent clips receive
silence. `audio_key` replaces that audio track, is padded if short and trimmed if
long, preserving the entire video. Assembly uses hard cuts, without transitions.

Success example (metadata varies by op):

```json
{"ok":true,"worker":"cpu-swap-stitch","op":"stitch","output_key":"outputs/cpu/stitch/<uuid>.mp4","duration_ms":1234,"estimated_usd":0.0001,"usd_per_hour_assumed":0.3}
```

Media input/output travels only through R2. The response contains an object key,
never media bytes, base64 or signed URLs. Outputs have unique keys and MIME types.
Errors return `ok:false`, an error message and request metering. Configure the
actual `RUNPOD_CPU_USD_PER_HR`; its default 0 means **unconfigured**. Estimates
cover request wall time including I/O, not cold initialization, idle time,
network-volume charges or storage. `worker_id` and `build` identify stale images.

## Models and R2

Use runtime secrets matching `marketing-studio-ie/.env`: `R2_ENDPOINT`,
`R2_ACCOUNT_ID`, `R2_ACCESS_KEY`, `R2_SECRET_KEY`, `R2_BUCKET`. No secrets are copied
into the image. `R2_BUCKET_NAME` is a supported higher-priority alias. Media uses
that bucket; models default explicitly to `R2_MODELS_BUCKET=comfy`.

**Read-only inventory on 2026-09-12 found no FaceFusion assets in `comfy`.**
Stage the following upstream 3.3.2-compatible files and their matching `.hash`
sidecars at `comfy-models/facefusion/3.3.2/` before enabling swap:

```text
2dfan4.onnx
arcface_w600k_r50.onnx
bisenet_resnet_34.onnx
fairface.onnx
fan_68_5.onnx
inswapper_128.onnx
kim_vocal_2.onnx
nsfw_1.onnx
nsfw_2.onnx
nsfw_3.onnx
xseg_1.onnx
yoloface_8n.onnx
```

Each `.hash` is the matching upstream CRC32 sidecar (for example
`inswapper_128.hash`), not SHA256. The adapter derives its exact catalog from
upstream instead of guessing names. The mask/voice assets are required by the
standard upstream common pre-check even though this worker only selects swap.
No model downloads occur at image build time or from public hosts at runtime.

At enabled startup the worker requires a real mount at `/runpod-volume`, downloads
missing/corrupt models to `/runpod-volume/facefusion-models/`, validates checksums,
and symlinks FaceFusion's `.assets/models/` files into the cache. A shared-volume
file lock and atomic rename protect simultaneous initializations. All inference
sessions and the swapper ONNX initializer load **before** `serverless.start()`.
Sessions persist between serial jobs using FaceFusion's `tolerant` memory mode.

Failed model initialization is exposed by ping; swap fails until configuration is
fixed and workers restarted. Stitch remains usable without models. There is no
per-request retry of heavy initialization. Job directories are unique and cleaned
on success/failure. Swap reserves space for extracted PNG frames; size your volume
for the models plus uncompressed frames, not just compressed input video size.

## Build, tests, deployment

```sh
docker build --build-arg WORKER_BUILD=cpu-v1 -t ms-runpod-cpu:local .
docker run --rm ms-runpod-cpu:local python /app/handler.py --test_input '{"input":{"op":"ping"}}'
# Run the local suite inside the built image; no R2 credentials/models required.
docker run --rm -v "$PWD/tests:/tests:ro" -e PYTHONPATH=/app ms-runpod-cpu:local python -m unittest discover -s /tests -v
```

Set endpoint runtime secrets using `.env.example`; do not bake `.env` into Docker.
Use an immutable registry tag/digest and distinct `WORKER_BUILD` per deployment.
Select a CPU worker with sufficient RAM for all resident ONNX sessions. Keep
concurrency at one. Set execution timeout for CPU video workloads (for example
1800 seconds initially); ffmpeg subprocesses have a configurable 1800-second
limit per command. FaceFusion runs in-process; the endpoint execution timeout is
the overall hard job bound. Use asynchronous `/run` and poll `/status/{id}`.

Application of all eleven Qwen deployment lessons:

| Lesson | CPU worker application |
|---|---|
| 1: Volume/datacenter | Attach `/runpod-volume`; select only its datacenter; verify `volume_mounted:true`. |
| 2: Runtime compatibility | Match FaceFusion 3.3.2 and its ORT 1.22.0 pin; `pip check` and parser smoke check during build. PyTorch/ComfyUI do not apply. |
| 3: Compiler/JIT | No Triton/JIT or CUDA dependencies, so no compiler is needed. If adding a JIT dependency, add build tools then. |
| 4: Canonical pinned repo | Clone `facefusion/facefusion` tag `3.3.2`, never HEAD. |
| 5: Crash throttling | Inspect ping/startup logs, fix root cause before retrying. If platform backoff persists, recreate endpoint and purge its queue; allow backoff to expire. |
| 6: Heavy init | R2 caching, pre-checks and all CPU sessions load before RunPod starts. Gate-off ping skips them. |
| 7: Exact enums | The actual pinned parser validates CPU, swapper, selector, mask and encoder values during build. |
| 8: Exact inputs | Adapter uses parsed defaults and verified upstream argument names; API identity/media direction is documented above. |
| 9: Model-specific pipeline | Use upstream `conditional_process()` with `face_swapper`, including alignment, embeddings, masking, checks and audio handling. |
| 10: Stale workers | After image updates, set idle timeout to 5s, drain/purge queued test jobs and stop old workers; verify changed `worker_id` and `build`. |
| 11: Disk | Swap refuses missing volume, verifies free disk before downloads and frame extraction, and reports `free_gb`. |

Use a longer idle timeout while debugging, then tune it for CPU model startup
cost. Enable FlashBoot if available for the selected CPU endpoint. Never attach
a GPU to this worker. Endpoint provisioning and model staging are deployment
steps; this directory does not create endpoints or upload model assets.
