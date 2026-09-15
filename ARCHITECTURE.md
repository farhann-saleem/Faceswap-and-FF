# Architecture — Faceswap-and-FF

## Why CPU

FaceFusion is ONNX Runtime. ffmpeg is I/O-bound. A GPU would idle while frames shuffle. A 30s 720p swap ~456s wall here; same wall time on GPU costs ~100× more for no visual win.

**Never attach a GPU.**

## Boot sequence (gate on)

1. Verify `/runpod-volume` is mounted.
2. Ensure FaceFusion ONNX (+ `.hash` CRC32 sidecars) from R2 / upstream.
3. Load inference sessions + swapper initializer.
4. Then `serverless.start()`.

Gate off (`ALLOW_GENERATE=0`): ping imports nothing heavy and hits no R2.

`fcntl` lock + atomic rename protect concurrent inits. Sessions persist between serial jobs (`tolerant` memory). Concurrency **1** — FaceFusion process-global state.

## Ops

| Op | Job |
| --- | --- |
| `ping` | Readiness fields |
| `swap` | Identity into still/clip → `output_key` |
| `stitch` | Ordered clips (+ optional audio) → MP4 `output_key` |

Keys only. `_key()` rejects URLs, absolute paths, `data:`, control chars, >1024 bytes.

## Stitch pipeline

Per clip: fit + black pad, `setsar=1`, constant fps, H.264 yuv420p, silence if no audio → concat `-c copy` +faststart. Optional `audio_key` replaces whole track (pad/trim); video never truncated. Hard cuts only.

## Disk math

Swap reserves `width × height × 3 × ceil(duration × fps) × 1.1` for raw PNG frames. Prefer 720p sources over 4K.

## Security

Dockerfile copies `handler.py` + `facefusion_cpu.py` only. ffmpeg argv lists, no shell. Concat manifest uses generated filenames only. NSFW pre-checks stay enabled.
