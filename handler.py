"""RunPod CPU worker: R2-only swap and stitch. Test ping with the gate off first."""
from __future__ import annotations

import sys
print('>>> cpu worker starting', flush=True)
import fcntl
import json
import math
import mimetypes
import os
import shutil
import subprocess
import tempfile
import threading
import time
import uuid

from pathlib import Path

try:
    import boto3
    from botocore.config import Config
    import runpod
    print(f'>>> boto3 / runpod {getattr(runpod, "__version__", "?")} ready', flush=True)
except ImportError as exc:
    print(f'>>> dependency import failed: {exc}', flush=True)
    raise

WORKER = 'cpu-swap-stitch'
BUILD = os.getenv('WORKER_BUILD', 'cpu-v1')
ALLOW = os.getenv('ALLOW_GENERATE', '0').strip().lower() in {'1', 'true', 'yes', 'on'}
VOLUME = Path('/runpod-volume')
MODEL_ROOT = VOLUME / 'facefusion-models'
MODEL_PREFIX = os.getenv('R2_FACEFUSION_PREFIX', 'comfy-models/facefusion/3.3.2').strip('/')
USD_PER_HOUR = float(os.getenv('RUNPOD_CPU_USD_PER_HR', '0'))
THREADS = int(os.getenv('CPU_THREADS', '4'))
TIMEOUT = int(os.getenv('FFMPEG_TIMEOUT_SECONDS', '1800'))
MAX_INPUT_BYTES = int(os.getenv('MAX_INPUT_BYTES', '2147483648'))
RESERVE_BYTES = int(os.getenv('DISK_RESERVE_BYTES', '1073741824'))
LOCK = threading.Lock()  # FaceFusion uses process-global state; jobs must be serial.
_facefusion = None
_init_error = None
_r2_client = None


def _env(*names, default=''):
    return next((os.environ[n].strip() for n in names if os.environ.get(n, '').strip()), default)


def _r2():
    global _r2_client
    bucket = _env('R2_BUCKET_NAME', 'R2_BUCKET', default='comfy')
    if _r2_client is None:
        account = _env('R2_ACCOUNT_ID')
        endpoint = _env('R2_ENDPOINT') or (f'https://{account}.r2.cloudflarestorage.com' if account else '')
        access, secret = _env('R2_ACCESS_KEY'), _env('R2_SECRET_KEY')
        if not all((endpoint, access, secret)):
            raise RuntimeError('Missing R2 endpoint/account, access key or secret key')
        _r2_client = boto3.client('s3', endpoint_url=endpoint, region_name='auto',
            aws_access_key_id=access, aws_secret_access_key=secret,
            config=Config(signature_version='s3v4', connect_timeout=10, read_timeout=120,
                          retries={'max_attempts': 3, 'mode': 'standard'}))
    return bucket, _r2_client


def _disk(path, needed=0):
    if shutil.disk_usage(path).free < needed + RESERVE_BYTES:
        raise RuntimeError(f'Insufficient disk at {path}; attach a volume in the endpoint datacenter')


def _key(value, name):
    if not isinstance(value, str) or not value.strip() or len(value.encode()) > 1024:
        raise ValueError(f'{name} must be a nonempty R2 object key (maximum 1024 bytes)')
    if value.startswith(('/', 'data:')) or '://' in value or any(ord(c) < 32 for c in value):
        raise ValueError(f'{name} must be an R2 key, not a URL, path or media bytes')
    return value


def _download(key, dest, *, bucket=None, cached=False):
    media_bucket, client = _r2()
    bucket = bucket or media_bucket
    size = client.head_object(Bucket=bucket, Key=key)['ContentLength']
    if size <= 0 or (not cached and size > MAX_INPUT_BYTES):
        raise ValueError('R2 object is empty or exceeds MAX_INPUT_BYTES')
    if cached and dest.is_file() and dest.stat().st_size == size:
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    _disk(dest.parent, size)
    part = dest.with_name(dest.name + '.' + uuid.uuid4().hex + '.part')
    try:
        client.download_file(bucket, key, str(part))
        if part.stat().st_size != size:
            raise RuntimeError('Incomplete R2 download')
        part.replace(dest)
    finally:
        part.unlink(missing_ok=True)


def _r2_exists(key, bucket=None):
    """Check if key exists on R2. Returns size or 0."""
    try:
        media_bucket, client = _r2()
        resp = client.head_object(Bucket=bucket or media_bucket, Key=key)
        return resp['ContentLength']
    except Exception:
        return 0


def _upload(local_path, key, bucket=None):
    """Upload file to R2."""
    import mimetypes as mt
    media_bucket, client = _r2()
    client.upload_file(str(local_path), bucket or media_bucket, key,
                       ExtraArgs={'ContentType': mt.guess_type(local_path.name)[0] or 'application/octet-stream'})
    print(f'  seeded R2: {key} ({local_path.stat().st_size} bytes)', flush=True)


def _setup_model_dir(adapter):
    """Symlink FaceFusion's model dir → volume cache. Return volume model dir."""
    if not os.path.ismount(VOLUME):
        raise RuntimeError('/runpod-volume is not mounted; attach a network volume in the same datacenter')
    MODEL_ROOT.mkdir(parents=True, exist_ok=True)
    model_dir = adapter.model_dir()
    # Symlink FaceFusion .assets/models/ → volume so downloads persist
    model_dir.parent.mkdir(parents=True, exist_ok=True)
    if model_dir.is_symlink():
        if model_dir.resolve() == MODEL_ROOT.resolve():
            return MODEL_ROOT
        model_dir.unlink()
    elif model_dir.is_dir():
        # Move any existing models to volume first
        for f in model_dir.iterdir():
            dest = MODEL_ROOT / f.name
            if not dest.exists():
                shutil.move(str(f), str(dest))
        model_dir.rmdir()
    model_dir.symlink_to(MODEL_ROOT)
    return MODEL_ROOT


def _preseed_from_r2():
    """Try to download models from R2 → volume before FaceFusion init.
    If R2 has models (from previous seeding), skip upstream download."""
    bucket = os.getenv('R2_MODELS_BUCKET', 'comfy')
    try:
        _, client = _r2()
        resp = client.list_objects_v2(Bucket=bucket, Prefix=MODEL_PREFIX + '/')
        for obj in resp.get('Contents', []):
            key = obj['Key']
            name = key.split('/')[-1]
            if not name:
                continue
            dest = MODEL_ROOT / name
            if dest.is_file() and dest.stat().st_size == obj['Size']:
                print(f'  cached {name} ({obj["Size"]} bytes)', flush=True)
                continue
            print(f'  downloading R2: {key} → {dest}', flush=True)
            _download(key, dest, bucket=bucket, cached=True)
    except Exception as exc:
        print(f'  R2 preseed skipped: {exc}', flush=True)


def _seed_r2_after_init():
    """Upload models from volume → R2 so future workers/volumes have them."""
    bucket = os.getenv('R2_MODELS_BUCKET', 'comfy')
    seeded = 0
    for f in sorted(MODEL_ROOT.iterdir()):
        if f.suffix not in {'.onnx', '.hash'}:
            continue
        key = f'{MODEL_PREFIX}/{f.name}'
        size = _r2_exists(key, bucket=bucket)
        if size == f.stat().st_size:
            continue
        try:
            _upload(f, key, bucket=bucket)
            seeded += 1
        except Exception as exc:
            print(f'  R2 seed failed for {f.name}: {exc}', flush=True)
    if seeded:
        print(f'>>> seeded {seeded} model files to R2 at {MODEL_PREFIX}/', flush=True)


def initialize():
    global _facefusion, _init_error
    if not ALLOW:
        print('>>> gate off: no model initialization', flush=True)
        return
    try:
        from facefusion_cpu import FaceFusionCPU
        adapter = FaceFusionCPU()

        # 1. Symlink model dir → volume
        _setup_model_dir(adapter)
        print('>>> model dir linked to volume', flush=True)

        # 2. Try preseed from R2 (fast if R2 has models from previous seeding)
        print('>>> checking R2 for cached models...', flush=True)
        _preseed_from_r2()

        # 3. FaceFusion warm — downloads any missing from upstream automatically
        print('>>> warming FaceFusion (may download from upstream on first run)...', flush=True)
        adapter.warm()

        # 4. Seed R2 with any new models (so future workers skip upstream)
        print('>>> seeding R2 with models...', flush=True)
        _seed_r2_after_init()

        _facefusion = adapter
        print('>>> CPU models loaded; accepting jobs', flush=True)
    except Exception as exc:
        _init_error = f'{type(exc).__name__}: {exc}'
        print(f'>>> FaceFusion init failed: {_init_error}', flush=True)
        # Keep ping/stitch accessible; no hidden initialization during swap jobs.


def _run(args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=TIMEOUT, check=False)
    if result.returncode:
        raise RuntimeError(f'{Path(args[0]).name} failed: {result.stderr[-1800:]}')
    return result.stdout


def _probe(path):
    return json.loads(_run(['ffprobe', '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(path)]))


def _ffmpeg(args):
    return _run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
                 '-filter_threads', str(THREADS), *args])


def _integer(inp, name, default, lo, hi):
    value = inp.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise ValueError(f'{name} must be an integer between {lo} and {hi}')
    return value


def stitch(inp, work):
    keys = inp.get('clip_keys', inp.get('clips'))
    if not isinstance(keys, list) or not 1 <= len(keys) <= 100:
        raise ValueError('clip_keys must contain 1–100 R2 video keys')
    keys = [_key(k, 'clip_keys item') for k in keys]
    audio_key = _key(inp['audio_key'], 'audio_key') if inp.get('audio_key') is not None else None
    width = _integer(inp, 'width', 1280, 2, 3840)
    height = _integer(inp, 'height', 720, 2, 3840)
    if width % 2 or height % 2:
        raise ValueError('width and height must be even for H.264/yuv420p')
    fps = inp.get('fps', 30)
    if isinstance(fps, bool) or not isinstance(fps, (int, float)) or not math.isfinite(fps) or not 1 <= fps <= 60:
        raise ValueError('fps must be a finite number between 1 and 60')
    segments = []
    for i, key in enumerate(keys):
        clip = work / f'clip-{i}.media'
        _download(key, clip)
        probe = _probe(clip)
        video = next((s for s in probe['streams'] if s['codec_type'] == 'video'), None)
        if not video:
            raise ValueError(f'clip_keys[{i}] has no video stream')
        duration = float(video.get('duration') or probe['format'].get('duration') or 0)
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError(f'clip_keys[{i}] has no valid duration')
        _disk(work)
        segment = work / f'segment-{i}.mp4'
        vf = (f'scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2,'
              f'pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps},setpts=PTS-STARTPTS')
        args = ['-i', str(clip)]
        has_audio = any(s['codec_type'] == 'audio' for s in probe['streams'])
        if not has_audio:
            args += ['-f', 'lavfi', '-i', 'anullsrc=r=48000:cl=stereo']
        args += ['-map', '0:v:0', '-map', '0:a:0' if has_audio else '1:a:0',
                 '-vf', vf, '-af', 'aresample=48000:async=1:first_pts=0,apad',
                 '-t', str(duration), '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20',
                 '-pix_fmt', 'yuv420p', '-threads', str(THREADS), '-video_track_timescale', '90000',
                 '-c:a', 'aac', '-ar', '48000', '-ac', '2', '-b:a', '192k', str(segment)]
        _ffmpeg(args)
        segments.append(segment)
        clip.unlink()
    manifest = work / 'concat.txt'
    # Only generated filenames enter the concat grammar; R2 keys never do.
    manifest.write_text(''.join(f"file '{p.name}'\n" for p in segments))
    assembled = work / 'assembled.mp4'
    _ffmpeg(['-f', 'concat', '-safe', '1', '-i', str(manifest), '-c', 'copy', '-movflags', '+faststart', str(assembled)])
    if audio_key:
        audio = work / 'soundtrack.media'
        _download(audio_key, audio)
        if not any(s['codec_type'] == 'audio' for s in _probe(audio)['streams']):
            raise ValueError('audio_key has no audio stream')
        output = work / 'stitched.mp4'
        _ffmpeg(['-i', str(assembled), '-i', str(audio), '-map', '0:v:0', '-map', '1:a:0',
                 '-c:v', 'copy', '-af', 'aresample=48000:async=1:first_pts=0,apad',
                 '-c:a', 'aac', '-ar', '48000', '-ac', '2', '-shortest', '-movflags', '+faststart', str(output)])
    else:
        output = assembled
    return output, {'width': width, 'height': height, 'fps': fps, 'clip_count': len(keys)}


def swap(inp, work):
    source_key = _key(inp.get('source_key'), 'source_key')
    face_key = _key(inp.get('target_face_key'), 'target_face_key')
    if _facefusion is None:
        raise RuntimeError(f'FaceFusion is not ready; fix startup configuration and restart. {_init_error or ""}')
    suffix = Path(source_key).suffix.lower()
    if suffix not in {'.png', '.jpg', '.jpeg', '.webp', '.mp4', '.mov', '.mkv', '.webm'}:
        raise ValueError('source_key must have a supported image/video extension')
    face_suffix = Path(face_key).suffix.lower()
    if face_suffix not in {'.png', '.jpg', '.jpeg', '.webp'}:
        raise ValueError('target_face_key must be an image (.png/.jpg/.jpeg/.webp)')
    media, face = work / ('source' + suffix), work / ('face' + face_suffix)
    _download(source_key, media)
    _download(face_key, face)
    if suffix in {'.mp4', '.mov', '.mkv', '.webm'}:
        probe = _probe(media)
        video = next((s for s in probe['streams'] if s['codec_type'] == 'video'), None)
        if not video:
            raise ValueError('source_key has no video stream')
        duration = float(video.get('duration') or probe['format'].get('duration') or 0)
        from fractions import Fraction
        fps = float(Fraction(video.get('avg_frame_rate') or '0'))
        if not math.isfinite(duration) or duration <= 0 or fps <= 0:
            raise ValueError('Source video has no valid duration/frame rate')
        # FaceFusion extracts PNG frames. Reserve raw-frame space plus encoding headroom.
        _disk(work, int(video['width'] * video['height'] * 3 * math.ceil(duration * fps) * 1.1))
        if suffix != '.mp4' or video['width'] % 2 or video['height'] % 2:
            normalized = work / 'normalized.mp4'
            _ffmpeg(['-i', str(media), '-map', '0:v:0', '-map', '0:a:0?',
                     '-vf', 'pad=ceil(iw/2)*2:ceil(ih/2)*2', '-c:v', 'libx264',
                     '-preset', 'veryfast', '-crf', '18', '-pix_fmt', 'yuv420p',
                     '-threads', str(THREADS), '-c:a', 'aac', str(normalized)])
            media = normalized
        suffix = '.mp4'
    output = work / ('swapped' + suffix)
    _facefusion.swap(media, face, output, work)
    return output, {}


def handler(job):
    start = time.monotonic()
    try:
        if not isinstance(job, dict) or not isinstance(job.get('input', {}), dict):
            raise ValueError('input must be an object')
        inp = job.get('input', {})
        op = str(inp.get('op') or 'ping').lower()
        if op == 'ping':
            return {'ok': True, 'worker': WORKER, 'ffmpeg': bool(shutil.which('ffmpeg')),
                    'facefusion_ready': _facefusion is not None, 'volume_mounted': os.path.ismount(VOLUME),
                    'free_gb': round(shutil.disk_usage(VOLUME if VOLUME.exists() else '/').free / 1e9, 2),
                    'allow_generate': ALLOW, 'build': BUILD, 'worker_id': os.getenv('RUNPOD_POD_ID'),
                    'init_error': _init_error}
        if op not in {'swap', 'stitch'}:
            raise ValueError(f'unknown op {op}. Use ping, swap, or stitch')
        if not ALLOW:
            raise RuntimeError('ALLOW_GENERATE is off. No FaceFusion download or processing is allowed')
        with LOCK:
            root = VOLUME if os.path.ismount(VOLUME) else Path('/tmp')
            _disk(root)
            with tempfile.TemporaryDirectory(prefix='cpu-job-', dir=root) as temp:
                output, metadata = (swap if op == 'swap' else stitch)(inp, Path(temp))
                if not output.is_file() or output.stat().st_size == 0:
                    raise RuntimeError('Processing produced no output')
                bucket, client = _r2()
                key = f'{os.getenv("R2_OUTPUT_PREFIX", "outputs/cpu").strip("/")}/{op}/{uuid.uuid4().hex}{output.suffix}'
                client.upload_file(str(output), bucket, key, ExtraArgs={
                    'ContentType': mimetypes.guess_type(output.name)[0] or 'application/octet-stream'})
        result = {'ok': True, 'worker': WORKER, 'op': op, 'output_key': key, **metadata}
    except Exception as exc:
        result = {'ok': False, 'worker': WORKER, 'error': f'{type(exc).__name__}: {exc}'}
    duration_ms = int((time.monotonic() - start) * 1000)
    return {**result, 'duration_ms': duration_ms,
            'estimated_usd': round(duration_ms / 3_600_000 * USD_PER_HOUR, 6),
            'usd_per_hour_assumed': USD_PER_HOUR, 'build': BUILD,
            'worker_id': os.getenv('RUNPOD_POD_ID')}


if __name__ == '__main__':
    initialize()  # All downloads and inference sessions precede accepting jobs (Lesson 6).
    runpod.serverless.start({'handler': handler})
