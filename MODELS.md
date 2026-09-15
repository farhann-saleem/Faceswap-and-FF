# Models — Faceswap-and-FF

## FaceFusion 3.3.2

Pinned upstream: [facefusion/facefusion @ 3.3.2](https://github.com/facefusion/facefusion/tree/3.3.2) — never HEAD.

Swapper: `inswapper_128`. Face select: largest per frame (`one`, `large-small`). No multi-person temporal tracking.

## ONNX catalog (R2)

Prefix `comfy-models/facefusion/3.3.2/` (+ matching `.hash` CRC32 sidecars):

```
2dfan4.onnx              fan_68_5.onnx        nsfw_2.onnx
arcface_w600k_r50.onnx   inswapper_128.onnx   nsfw_3.onnx
bisenet_resnet_34.onnx   kim_vocal_2.onnx     xseg_1.onnx
fairface.onnx            nsfw_1.onnx          yoloface_8n.onnx
```

Mask/voice assets required by upstream common pre-check even when only swapper is selected.

## Measured performance

| Job | Measured |
| --- | --- |
| Still swap, warm | 6.2–8.1s `duration_ms` |
| Still swap, E2E | ~10–20s wall |
| Video swap 15s 720×1280 | ~134s |
| Video swap 30s 720p | ~456s (audio preserved) |

`estimated_usd` is 0 until `RUNPOD_CPU_USD_PER_HR` is set.

## Product UI

| Desk | Button | Backend |
| --- | --- | --- |
| Images / Videos / Effects | **Generate** | `op=swap` via `POST /api/faceswaps` |
| Documentaries | **Assemble** / **Export** | `op=stitch` |

## Known gaps

- `op=stitch` — no product smoke yet
- Timeline v1 not deployed (`timeline_version: 1` gate)
- Raise `runpod` pin toward `>=1.10.1,<2`
