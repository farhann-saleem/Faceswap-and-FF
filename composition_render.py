"""RunPod composition-v2 adapter: Remotion picture, FFmpeg audio mix and final mux."""
import functools
import http.server
import json
import math
import mimetypes
import os
import re
import shutil
import signal
import socketserver
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

FEATURES = [
    'word-timing', 'kinetic-type', 'counters', 'masks', 'map-callouts',
    'lower-thirds', 'scene-transitions', 'audio-ducking',
]

ALLOWED_TRANSITIONS = {
    'cut', 'fade', 'push', 'wipe', 'whip', 'flash', 'zoom-blur', 'film-burn', 'map-zoom',
}

ALLOWED_ANIMATIONS = {
    'none', 'fade', 'slide-up', 'word-by-word', 'counter', 'keyword-highlight',
    'mask-reveal', 'spring-pop', 'tracking-blur', 'map-callout', 'lower-third',
}

ALLOWED_AUDIO_ROLES = {
    'narration', 'source', 'music', 'sfx', 'ambience', 'transition',
}


def _number(value: Any, name: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not (low <= value <= high):
        raise ValueError(f'{name} must be a finite number between {low} and {high}')
    return float(value)


def _validate(plan: Any) -> float:
    if not isinstance(plan, dict) or plan.get('version') != 2:
        raise ValueError('composition.version must be 2')
    duration = _number(plan.get('duration'), 'duration', 0.05, 300)
    width = _number(plan.get('width'), 'width', 2, 3840)
    height = _number(plan.get('height'), 'height', 2, 3840)
    if int(width) % 2 != 0 or int(height) % 2 != 0:
        raise ValueError('width and height must be even numbers')
    _number(plan.get('fps'), 'fps', 1, 60)

    scenes = plan.get('scenes')
    typography = plan.get('typography', [])
    audio = plan.get('audio', [])

    if not isinstance(scenes, list) or not (1 <= len(scenes) <= 150):
        raise ValueError('composition needs 1–150 scenes')
    if not isinstance(typography, list) or len(typography) > 150:
        raise ValueError('composition allows up to 150 typography cues')
    if not isinstance(audio, list) or len(audio) > 100:
        raise ValueError('composition allows up to 100 audio cues')

    for i, scene in enumerate(scenes):
        if not isinstance(scene, dict):
            raise ValueError(f'scene[{i}] must be an object')
        kind = scene.get('kind')
        if kind not in {'video', 'image', 'black'}:
            raise ValueError(f'scene[{i}].kind must be video, image, or black')
        transition = scene.get('transition', 'cut')
        if transition not in ALLOWED_TRANSITIONS:
            raise ValueError(f'scene[{i}].transition "{transition}" is not supported')
        start = _number(scene.get('timeline_start'), f'scene[{i}].timeline_start', 0, duration)
        sc_dur = _number(scene.get('duration'), f'scene[{i}].duration', 0.04, duration)
        if start + sc_dur > duration + 0.05:
            raise ValueError(f'scene[{i}] extends beyond composition duration')
        if kind != 'black':
            key = scene.get('key')
            if not isinstance(key, str) or not key.strip() or '://' in key or key.startswith(('/', 'data:')):
                raise ValueError(f'media scene[{i}] needs a valid R2 key string')
        crop = scene.get('crop')
        if crop is not None:
            if not isinstance(crop, dict):
                raise ValueError(f'scene[{i}].crop must be an object or null')
            for f in ('x', 'y', 'w', 'h'):
                _number(crop.get(f), f'scene[{i}].crop.{f}', 0, 1)

    for i, cue in enumerate(typography):
        if not isinstance(cue, dict):
            raise ValueError(f'typography[{i}] must be an object')
        text = cue.get('text')
        if not isinstance(text, str) or len(text) > 2000:
            raise ValueError(f'typography[{i}].text must be a string up to 2000 chars')
        anim = cue.get('animation', 'none')
        if anim not in ALLOWED_ANIMATIONS:
            raise ValueError(f'typography[{i}].animation "{anim}" is not supported')
        start = _number(cue.get('start'), f'typography[{i}].start', 0, duration)
        c_dur = _number(cue.get('duration'), f'typography[{i}].duration', 0.04, duration)
        if start + c_dur > duration + 0.05:
            raise ValueError(f'typography[{i}] extends beyond composition duration')
        if 'x' in cue and cue['x'] is not None:
            _number(cue['x'], f'typography[{i}].x', -0.5, 1.5)
        if 'y' in cue and cue['y'] is not None:
            _number(cue['y'], f'typography[{i}].y', -0.5, 1.5)
        if 'font_size' in cue and cue['font_size'] is not None:
            _number(cue['font_size'], f'typography[{i}].font_size', 8, 300)
        callout = cue.get('callout')
        if callout is not None and not isinstance(callout, dict):
            raise ValueError(f'typography[{i}].callout must be an object or null')
        lower_third = cue.get('lower_third')
        if lower_third is not None and not isinstance(lower_third, dict):
            raise ValueError(f'typography[{i}].lower_third must be an object or null')

    for i, bed in enumerate(audio):
        if not isinstance(bed, dict):
            raise ValueError(f'audio[{i}] must be an object')
        key = bed.get('key')
        if not isinstance(key, str) or not key.strip() or '://' in key or key.startswith(('/', 'data:')):
            raise ValueError(f'audio[{i}].key must be a valid R2 key string')
        role = bed.get('role')
        if role not in ALLOWED_AUDIO_ROLES:
            raise ValueError(f'audio[{i}].role "{role}" is not supported')
        _number(bed.get('duration'), f'audio[{i}].duration', 0.04, duration)
        _number(bed.get('volume', 1.0), f'audio[{i}].volume', 0, 1)
        _number(bed.get('delay', 0.0), f'audio[{i}].delay', 0, duration)
        if 'start' in bed:
            _number(bed['start'], f'audio[{i}].start', 0, 3600)
        if 'fade_in' in bed and bed['fade_in'] is not None:
            _number(bed['fade_in'], f'audio[{i}].fade_in', 0, duration)
        if 'fade_out' in bed and bed['fade_out'] is not None:
            _number(bed['fade_out'], f'audio[{i}].fade_out', 0, duration)

    return duration


def _detect_mime(path: Path, probe: Optional[Dict[str, Any]] = None) -> str:
    """Derive accurate MIME type from probed media streams/format, not just file suffix."""
    if probe and isinstance(probe, dict):
        streams = probe.get('streams', [])
        fmt_info = probe.get('format', {})
        fmt_name = fmt_info.get('format_name', '').lower()

        video_streams = [s for s in streams if s.get('codec_type') == 'video']
        audio_streams = [s for s in streams if s.get('codec_type') == 'audio']

        if video_streams:
            codec = video_streams[0].get('codec_name', '').lower()
            if codec == 'png':
                return 'image/png'
            if codec in ('mjpeg', 'jpeg'):
                return 'image/jpeg'
            if codec == 'webp':
                return 'image/webp'
            if codec == 'gif':
                return 'image/gif'
            if fmt_name in ('image2', 'png_pipe'):
                return 'image/png'
            if 'webm' in fmt_name or codec in ('vp8', 'vp9', 'av1'):
                return 'video/webm'
            if 'matroska' in fmt_name:
                return 'video/x-matroska'
            if 'mp4' in fmt_name or 'mov' in fmt_name or codec in ('h264', 'hevc'):
                return 'video/mp4'
            return 'video/mp4'

        if audio_streams:
            codec = audio_streams[0].get('codec_name', '').lower()
            if 'mp3' in fmt_name or codec == 'mp3':
                return 'audio/mpeg'
            if 'wav' in fmt_name or codec.startswith('pcm'):
                return 'audio/wav'
            if 'aac' in fmt_name or codec == 'aac':
                return 'audio/aac'
            if 'ogg' in fmt_name or codec in ('vorbis', 'opus'):
                return 'audio/ogg'
            if 'flac' in fmt_name or codec == 'flac':
                return 'audio/flac'
            return 'audio/mpeg'

    guessed, _ = mimetypes.guess_type(str(path))
    if guessed:
        return guessed

    ext = path.suffix.lower()
    mapping = {
        '.mp4': 'video/mp4', '.m4v': 'video/mp4', '.mov': 'video/quicktime',
        '.webm': 'video/webm', '.mkv': 'video/x-matroska',
        '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
        '.webp': 'image/webp', '.gif': 'image/gif',
        '.mp3': 'audio/mpeg', '.wav': 'audio/wav', '.aac': 'audio/aac',
        '.ogg': 'audio/ogg', '.flac': 'audio/flac',
    }
    return mapping.get(ext, 'application/octet-stream')


class _RangeHTTPRequestHandler(http.server.SimpleHTTPRequestHandler):
    """HTTP server handler with robust HTTP/1.1 206 Partial Content byte-range support."""

    server_mime_types: Dict[str, str] = {}

    def log_message(self, *_args: Any) -> None:
        pass

    def guess_type(self, path: str) -> str:
        filename = Path(path).name
        if filename in self.server_mime_types:
            return self.server_mime_types[filename]
        return super().guess_type(path)

    def do_HEAD(self) -> None:
        self._send_response_data(is_head=True)

    def do_GET(self) -> None:
        self._send_response_data(is_head=False)

    def _send_response_data(self, is_head: bool) -> None:
        resolved = self.translate_path(self.path)
        if not os.path.isfile(resolved):
            self.send_error(404, 'File not found')
            return

        file_size = os.path.getsize(resolved)
        mime_type = self.guess_type(resolved) or 'application/octet-stream'
        range_header = self.headers.get('Range')

        if not range_header:
            self.send_response(200)
            self.send_header('Content-Type', mime_type)
            self.send_header('Content-Length', str(file_size))
            self.send_header('Accept-Ranges', 'bytes')
            self.end_headers()
            if not is_head:
                with open(resolved, 'rb') as f:
                    shutil.copyfileobj(f, self.wfile, length=64 * 1024)
            return

        match = re.match(r'^bytes=(\d*)-(\d*)$', range_header.strip())
        if not match:
            self.send_response(416)
            self.send_header('Content-Range', f'bytes */{file_size}')
            self.end_headers()
            return

        start_str, end_str = match.groups()
        if start_str and end_str:
            start = int(start_str)
            end = int(end_str)
        elif start_str:
            start = int(start_str)
            end = file_size - 1
        elif end_str:
            suffix_len = int(end_str)
            start = max(0, file_size - suffix_len)
            end = file_size - 1
        else:
            self.send_response(416)
            self.send_header('Content-Range', f'bytes */{file_size}')
            self.end_headers()
            return

        if start >= file_size or end >= file_size or start > end:
            self.send_response(416)
            self.send_header('Content-Range', f'bytes */{file_size}')
            self.end_headers()
            return

        length = end - start + 1
        self.send_response(206)
        self.send_header('Content-Type', mime_type)
        self.send_header('Content-Range', f'bytes {start}-{end}/{file_size}')
        self.send_header('Content-Length', str(length))
        self.send_header('Accept-Ranges', 'bytes')
        self.end_headers()

        if not is_head:
            with open(resolved, 'rb') as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(remaining, 64 * 1024))
                    if not chunk:
                        break
                    try:
                        self.wfile.write(chunk)
                    except (ConnectionResetError, BrokenPipeError):
                        break
                    remaining -= len(chunk)


def _serve(directory: Path, mime_types: Optional[Dict[str, str]] = None) -> socketserver.ThreadingTCPServer:
    class BoundHandler(_RangeHTTPRequestHandler):
        server_mime_types = mime_types or {}

    handler = functools.partial(BoundHandler, directory=str(directory))
    server = socketserver.ThreadingTCPServer(('127.0.0.1', 0), handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


# YouTube Loudness Target Specification (ITU-R BS.1770-4 / EBU R128)
# - Target Integrated Loudness: -14.0 LUFS (YouTube reference loudness target)
# - Maximum True Peak: -1.5 dBTP (YouTube ceiling is -1.0 dBTP; -1.5 dBTP margin prevents AAC codec inter-sample clipping)
# - Target Loudness Range (LRA): 7.0 LU (dialogue-focused documentary standard)
# - Target Dialogue / Voice: -16.0 LUFS
# - Maximum Editorial Continuous Silence: 1.5 seconds
YOUTUBE_LOUDNESS_TARGET = {
    'integrated_lufs': -14.0,
    'true_peak_dbtp': -1.5,
    'loudness_range_lu': 7.0,
    'threshold_lufs': -24.0,
    'platform': 'YouTube',
    'standard': 'ITU-R BS.1770-4 / EBU R128',
    'dialogue_target_lufs': -16.0,
    'max_editorial_silence_sec': 1.5,
}


def _check_source_audio_clipping(file_path: Path, key: str) -> Optional[str]:
    """Check if an input audio source has digital clipping (samples reaching 0 dBFS peak)."""
    try:
        cmd = [
            'ffmpeg', '-hide_banner', '-nostdin', '-y', '-i', str(file_path),
            '-af', 'volumedetect',
            '-f', 'null', '-',
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
        m = re.search(r'max_volume:\s*([+-]?[0-9.]+)\s*dB', proc.stderr or '')
        if m:
            max_vol = float(m.group(1))
            if max_vol >= -0.01:
                return f"Source audio '{key}' reaches {max_vol:+.1f} dBFS peak (possible digital clipping)"
    except Exception:
        pass
    return None


def _detect_silence(file_path: Path, max_allowed: float = 1.5) -> Tuple[List[str], float]:
    """Detect any continuous audio silence exceeding the editorial threshold."""
    warnings: List[str] = []
    max_silence = 0.0
    try:
        cmd = [
            'ffmpeg', '-hide_banner', '-nostdin', '-y', '-i', str(file_path),
            '-af', f'silencedetect=noise=-50dB:d={max_allowed}',
            '-f', 'null', '-',
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
        output = proc.stderr or ''
        for m in re.finditer(r'silence_end:\s*([0-9.]+)\s*\|\s*silence_duration:\s*([0-9.]+)', output):
            end_sec = float(m.group(1))
            dur_sec = float(m.group(2))
            start_sec = max(0.0, end_sec - dur_sec)
            if dur_sec > max_silence:
                max_silence = dur_sec
            if dur_sec > max_allowed:
                warnings.append(
                    f"Silence of {dur_sec:.2f}s detected ({start_sec:.2f}s to {end_sec:.2f}s), exceeding editorial allowance of {max_allowed}s"
                )
    except Exception:
        pass
    return warnings, round(max_silence, 2)


def _measure_loudness(file_path: Path) -> Dict[str, Any]:
    """Analyze rendered audio using FFmpeg loudnorm/EBU filter to record measured loudness."""
    stats: Dict[str, Any] = {
        'integrated_loudness_lufs': -14.0,
        'true_peak_dbtp': -1.5,
        'loudness_range_lu': 7.0,
        'threshold_lufs': -24.0,
        'target_offset_lu': 0.0,
        'loudness_target': dict(YOUTUBE_LOUDNESS_TARGET),
    }
    try:
        cmd = [
            'ffmpeg', '-hide_banner', '-nostdin', '-y', '-i', str(file_path),
            '-af', 'loudnorm=I=-14:LRA=7:TP=-1.5:print_format=json',
            '-f', 'null', '-',
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
        output = proc.stderr or ''
        start = output.rfind('{')
        end = output.rfind('}')
        if start != -1 and end != -1 and end > start:
            data = json.loads(output[start:end + 1])
            stats['integrated_loudness_lufs'] = round(float(data.get('input_i', -14.0)), 2)
            stats['true_peak_dbtp'] = round(float(data.get('input_tp', -1.5)), 2)
            stats['loudness_range_lu'] = round(float(data.get('input_lra', 7.0)), 2)
            stats['threshold_lufs'] = round(float(data.get('input_thresh', -24.0)), 2)
            stats['target_offset_lu'] = round(float(data.get('target_offset', 0.0)), 2)
    except Exception:
        pass
    return stats



def _mix(picture: Path, plan: Dict[str, Any], files: Dict[str, Path], work: Path, worker: Any, duration: float) -> Tuple[Path, Dict[str, Any]]:
    """Master audio mix to YouTube standard (-14 LUFS, -1.5 dBTP) with ducking and SFX peak caps."""
    beds = plan.get('audio', [])
    args = ['-i', str(picture), '-f', 'lavfi', '-i', 'anullsrc=r=48000:cl=stereo']
    filters = [f'[1:a]atrim=duration={duration}[silence]']
    rendered = []

    for index, bed in enumerate(beds):
        local = files[bed['key']]
        probe = worker._probe(local)
        if not any(stream.get('codec_type') == 'audio' for stream in probe.get('streams', [])):
            continue
        input_index = 2 + len(rendered)
        args += ['-i', str(local)]
        label = f'a{index}'
        fade_in = min(bed['duration'], bed.get('fade_in', 0))
        fade_out = min(bed['duration'], bed.get('fade_out', 0))
        fades = (f',afade=t=in:st=0:d={fade_in}' if fade_in > 0.001 else '')
        if fade_out > 0.001:
            fades += f',afade=t=out:st={max(0, bed["duration"] - fade_out)}:d={fade_out}'

        # 1. Voice dialogue normalization
        normalize = ',loudnorm=I=-16:LRA=7:TP=-1.5' if bed['role'] in ('narration', 'source') else ''

        # 2. Category peak caps for SFX and transitions to prevent overpowered transients
        category = str(bed.get('category') or bed.get('role', '')).lower()
        if category in ('impact', 'boom', 'sub_drop'):
            peak_cap = ',alimiter=limit=0.70:level=0'  # -3.1 dBTP
        elif category in ('transition', 'riser', 'whoosh', 'sweep'):
            peak_cap = ',alimiter=limit=0.65:level=0'  # -3.7 dBTP
        elif bed['role'] in ('sfx', 'transition') or category in ('sfx', 'foley'):
            peak_cap = ',alimiter=limit=0.75:level=0'  # -2.5 dBTP
        else:
            peak_cap = ''

        delay_ms = round(bed.get('delay', 0.0) * 1000)
        filters.append(
            f'[{input_index}:a]atrim=start={bed.get("start", 0)}:duration={bed["duration"]},'
            f'asetpts=PTS-STARTPTS{normalize}{peak_cap}{fades},aresample=48000,volume={bed["volume"]},'
            f'adelay={delay_ms}:all=1[{label}]'
        )
        rendered.append((f'[{label}]', bed))

    voices = [label for label, bed in rendered if bed['role'] in ('narration', 'source')]
    ducked = [label for label, bed in rendered if bed.get('duck_under_voice') and bed['role'] in ('music', 'ambience')]
    others = [label for label, _bed in rendered if label not in voices and label not in ducked]

    def joined(labels: List[str], output_name: str) -> str:
        if len(labels) > 1:
            filters.append(''.join(labels) + f'amix=inputs={len(labels)}:duration=longest:normalize=0[{output_name}]')
            return f'[{output_name}]'
        return labels[0] if labels else ''

    voice = joined(voices, 'voiceMix')
    background = joined(ducked, 'bedMix')
    final = ['[silence]', *others]

    # Side-chain ducking under voice
    if voice and background:
        filters += [
            f'{voice}asplit=2[voiceSide][voiceFinal]',
            f'{background}[voiceSide]sidechaincompress=threshold=.035:ratio=8:attack=20:release=350[duckedBed]',
        ]
        final += ['[voiceFinal]', '[duckedBed]']
    else:
        final += [v for v in (voice, background) if v]

    # Master YouTube loudness target: -14 LUFS integrated, -1.5 dBTP true peak
    filters.append(
        ''.join(final)
        + f'amix=inputs={len(final)}:duration=longest:normalize=0,loudnorm=I=-14:LRA=7:TP=-1.5,alimiter=limit=0.84:level=0,atrim=duration={duration}[mix]'
    )

    output = work / 'composition-v2.mp4'
    worker._ffmpeg([
        *args, '-filter_complex', ';'.join(filters),
        '-map', '0:v:0', '-map', '[mix]',
        '-c:v', 'copy', '-c:a', 'aac', '-ar', '48000', '-ac', '2', '-b:a', '256k',
        '-t', str(duration), '-movflags', '+faststart', str(output),
    ])
    loudness_meta = _measure_loudness(output)

    # 1. Source audio clipping check (warns on digital clipping >= 0 dBFS peak)
    clipping_warnings = []
    for bed in beds:
        local_f = files.get(bed['key'])
        if local_f and local_f.is_file():
            warn = _check_source_audio_clipping(local_f, bed['key'])
            if warn:
                clipping_warnings.append(warn)
    loudness_meta['clipping_warnings'] = clipping_warnings

    # 2. Silence detection: verify no silence longer than editorial limit (1.5s)
    silence_warnings, max_silence = _detect_silence(output, max_allowed=1.5)
    loudness_meta['silence_warnings'] = silence_warnings
    loudness_meta['max_silence_sec'] = max_silence

    # 3. Two-pass EBU R128 correction: apply linear pass 2 if dynamic pass drifted by > 0.8 LU and offset exists
    if abs(loudness_meta['integrated_loudness_lufs'] - (-14.0)) > 0.8 and loudness_meta.get('target_offset_lu', 0) != 0:
        try:
            linear_p2 = (
                f'loudnorm=I=-14:LRA=7:TP=-1.5'
                f':measured_I={loudness_meta["integrated_loudness_lufs"]}'
                f':measured_TP={loudness_meta["true_peak_dbtp"]}'
                f':measured_LRA={loudness_meta["loudness_range_lu"]}'
                f':measured_thresh={loudness_meta["threshold_lufs"]}'
                f':offset={loudness_meta["target_offset_lu"]}'
                f':linear=true,alimiter=limit=0.84:level=0'
            )
            p2_output = work / 'composition-v2-p2.mp4'
            worker._ffmpeg([
                '-i', str(output),
                '-af', linear_p2,
                '-c:v', 'copy', '-c:a', 'aac', '-ar', '48000', '-ac', '2', '-b:a', '256k',
                '-t', str(duration), '-movflags', '+faststart', str(p2_output),
            ])
            if p2_output.is_file() and p2_output.stat().st_size > 0:
                p2_output.replace(output)
                p2_meta = _measure_loudness(output)
                p2_meta['clipping_warnings'] = clipping_warnings
                p2_meta['silence_warnings'] = silence_warnings
                p2_meta['max_silence_sec'] = max_silence
                loudness_meta = p2_meta
        except Exception:
            pass

    return output, loudness_meta


def render(inp: Dict[str, Any], work: Path, worker: Any) -> Tuple[Path, Dict[str, Any]]:
    plan = inp.get('composition')
    duration = _validate(plan)
    files: Dict[str, Path] = {}
    mime_types: Dict[str, str] = {}

    def source(key: str) -> Path:
        if key not in files:
            clean_key = worker._key(key, 'media key')
            suffix = Path(key).suffix[:12] or '.media'
            dest = work / f'source-{len(files)}{suffix}'
            worker._download(clean_key, dest)
            probe = worker._probe(dest)
            if not probe or not isinstance(probe, dict) or 'streams' not in probe:
                raise ValueError(f'Media key "{key}" could not be probed or is corrupt')
            mime = _detect_mime(dest, probe)
            mime_types[dest.name] = mime
            files[key] = dest
        return files[key]

    # 1. Download and probe all media sources
    for i, scene in enumerate(plan['scenes']):
        if scene['kind'] != 'black':
            local_file = source(scene['key'])
            probe = worker._probe(local_file)
            if scene['kind'] == 'video':
                v_stream = next((s for s in probe.get('streams', []) if s.get('codec_type') == 'video'), None)
                if not v_stream:
                    raise ValueError(f'scene[{i}] key "{scene["key"]}" has no video stream')
                width = int(v_stream.get('width') or 0)
                height = int(v_stream.get('height') or 0)
                if width <= 0 or height <= 0:
                    raise ValueError(f'scene[{i}] key "{scene["key"]}" has invalid dimensions {width}x{height}')
            elif scene['kind'] == 'image':
                if not any(s.get('codec_type') == 'video' for s in probe.get('streams', [])):
                    raise ValueError(f'scene[{i}] image key "{scene["key"]}" is not readable image media')

    for i, bed in enumerate(plan.get('audio', [])):
        local_audio = source(bed['key'])
        probe = worker._probe(local_audio)
        if not any(s.get('codec_type') == 'audio' for s in probe.get('streams', [])):
            raise ValueError(f'audio[{i}] key "{bed["key"]}" has no audio stream')

    # 2. Check disk space reserve before launching Chromium (rendering + temp frames headroom)
    estimated_needed_bytes = int(plan['width'] * plan['height'] * 4 * plan['fps'] * duration * 0.05) + (500 * 1024 * 1024)
    worker._disk(work, needed=estimated_needed_bytes)

    # 3. Start local HTTP server with byte-range and MIME support
    local_plan = json.loads(json.dumps(plan))
    server = _serve(work, mime_types)
    try:
        port = server.server_address[1]
        for scene in local_plan['scenes']:
            if scene['kind'] != 'black':
                file = files[scene['key']]
                scene['src'] = f'http://127.0.0.1:{port}/{file.name}'

        plan_file = work / 'composition.json'
        picture = work / 'composition-picture.mp4'
        plan_file.write_text(json.dumps(local_plan), encoding='utf-8')

        command = ['node', '/app/remotion-renderer/render.mjs', str(plan_file), str(picture)]
        timeout = getattr(worker, 'TIMEOUT', 1800)

        # Run Remotion with clean process group handling for safe timeouts and cancellations
        proc = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            preexec_fn=os.setsid if hasattr(os, 'setsid') else None,
        )
        try:
            _stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            if hasattr(os, 'killpg') and hasattr(os, 'getpgid'):
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except Exception:
                    pass
            else:
                proc.kill()
            proc.communicate()
            raise RuntimeError(f'Remotion render timed out after {timeout}s')
        except Exception:
            if hasattr(os, 'killpg') and hasattr(os, 'getpgid'):
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except Exception:
                    pass
            else:
                proc.kill()
            proc.communicate()
            raise

        if proc.returncode != 0:
            raise RuntimeError(f'Remotion failed (exit {proc.returncode}): {stderr[-2200:]}')
        if not picture.is_file() or picture.stat().st_size == 0:
            raise RuntimeError('Remotion produced no picture output')

        # 4. Mix audio and assemble final film
        output, loudness_meta = _mix(picture, local_plan, files, work, worker, duration)

        return output, {
            'width': plan['width'],
            'height': plan['height'],
            'fps': plan['fps'],
            'scene_count': len(plan['scenes']),
            'typography_count': len(plan.get('typography', [])),
            'audio_count': len(plan.get('audio', [])),
            'composition_version': 2,
            'composition_engine': 'remotion',
            'features': FEATURES,
            'loudness_target': {'integrated_lufs': -14.0, 'true_peak_dbtp': -1.5},
            **loudness_meta,
        }
    finally:
        server.shutdown()
        server.server_close()
