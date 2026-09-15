# Setup — Faceswap-and-FF (CPU)

## Prerequisites

- Docker
- RunPod **CPU** endpoint + network volume (same DC only)
- R2 media + models buckets (`comfy` for FaceFusion ONNX)
- FaceFusion assets staged under `comfy-models/facefusion/3.3.2/` (or first boot fetches upstream then seeds R2)

## Build & test

```bash
docker build --build-arg WORKER_BUILD=cpu-v1 -t ms-runpod-cpu:local .

# gate-off ping
docker run --rm ms-runpod-cpu:local \
  python /app/handler.py --test_input '{"input":{"op":"ping"}}'

# unit suite — no R2 / models required
docker run --rm -v "$PWD/tests:/tests:ro" -e PYTHONPATH=/app ms-runpod-cpu:local \
  python -m unittest discover -s /tests -v
```

Build self-checks: `pip check`, CPUExecutionProvider present / CUDA absent, FaceFusion catalog parse without download.

## Deploy

| Setting | Value |
| --- | --- |
| Worker type | **CPU** (never GPU) |
| Data centers | volume’s DC **only** |
| Concurrency | **1** |
| Execution timeout | ≥1800s |
| Invocation | async `/run` + poll |
| `WORKER_BUILD` | distinct per deploy (echoed by ping) |

After image update: idle 5s → purge → stop old workers → verify `worker_id` **and** `build` changed.

## Environment (names only)

[`.env.example`](.env.example)

| Name | Default | Purpose |
| --- | --- | --- |
| `ALLOW_GENERATE` | `0` | gate — `0` = ping only, no model init / R2 |
| `R2_ENDPOINT` / `R2_ACCOUNT_ID` | — | R2 |
| `R2_ACCESS_KEY` / `R2_SECRET_KEY` | — | credentials |
| `R2_BUCKET` | `comfy` | media (`R2_BUCKET_NAME` wins) |
| `R2_MODELS_BUCKET` | `comfy` | models |
| `R2_FACEFUSION_PREFIX` | `comfy-models/facefusion/3.3.2` | |
| `R2_OUTPUT_PREFIX` | `outputs/cpu` | |
| `RUNPOD_CPU_USD_PER_HR` | `0` | **0 = unconfigured, not free** |
| `CPU_THREADS` | `4` | |
| `FFMPEG_TIMEOUT_SECONDS` | `1800` | |
| `MAX_INPUT_BYTES` | 2 GB | |
| `DISK_RESERVE_BYTES` | 1 GB | |
| `WORKER_BUILD` | `cpu-v1` | |

## Ops quick reference

**swap**

```json
{"input":{
  "op":"swap",
  "source_key":"inputs/scene.mp4",
  "target_face_key":"avatars/person.png"
}}
```

- `source_key` = scene (FaceFusion `target_path`)
- `target_face_key` = identity image (FaceFusion `source_paths`)

**stitch**

```json
{"input":{
  "op":"stitch",
  "clip_keys":["clips/a.mp4","clips/b.mp4"],
  "audio_key":"audio/music.wav",
  "width":1280,"height":720,"fps":30
}}
```

Response carries `output_key`, never media bytes.

## Troubleshooting (short)

| Symptom | Fix |
| --- | --- |
| `facefusion_ready: false` | Fix config + **restart workers** (no per-request init retry) |
| `/runpod-volume is not mounted` | Attach volume in endpoint DC, stop old workers |
| `ALLOW_GENERATE is off` | Set `1`, restart |
| App stuck IN_PROGRESS with output_key | Resume same job — do not re-`/run` |
| Face barely changes | Profile / looking-down frames — model limit |
