"""RunPod CPU timeline-v1 extension. No local execution by the web backend."""
import math
import re
from pathlib import Path


def number(value, name, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f'{name} must be finite, {low}–{high}')
    return value


def render(inp, work, worker):
    plan = inp.get('timeline')
    if not isinstance(plan, dict) or plan.get('version') != 1:
        raise ValueError('timeline.version must be 1')
    # App maximum is 300s; 90s remains a valid plan size, not the hard ceiling.
    MAX_DURATION = 300
    duration = number(plan.get('duration'), 'duration', .05, MAX_DURATION)
    slices, beds = plan.get('slices'), plan.get('audio', [])
    if not isinstance(slices, list) or not 1 <= len(slices) <= 100:
        raise ValueError('timeline needs 1–100 slices')
    if not isinstance(beds, list) or len(beds) > 100:
        raise ValueError('timeline allows up to 100 audio beds')
    width = worker._integer(inp, 'width', 1280, 2, 3840)
    height = worker._integer(inp, 'height', 720, 2, 3840)
    fps = number(inp.get('fps', 30), 'fps', 1, 60)
    if width % 2 or height % 2:
        raise ValueError('Canvas dimensions must be even')
    # Validate the whole plan before any download or encoding.
    total = 0
    for s in slices:
        if s.get('kind') not in ('black', 'image', 'video'):
            raise ValueError('Invalid slice kind')
        number(s.get('start'), 'start', 0, 86400)
        total += number(s.get('duration'), 'slice duration', .001, MAX_DURATION)
        if s['kind'] != 'black':
            worker._key(s.get('key'), 'slice key')
        crop = s.get('crop')
        if crop is not None:
            if not isinstance(crop, dict):
                raise ValueError('slice crop must be an object')
            for key in ('x', 'y', 'w', 'h'):
                number(crop.get(key), f'crop.{key}', 0, 1)
            if crop['w'] < .05 or crop['h'] < .05:
                raise ValueError('crop window too small')
            if crop['x'] + crop['w'] > 1.001 or crop['y'] + crop['h'] > 1.001:
                raise ValueError('crop must stay inside the frame')
        if not isinstance(s.get('texts'), list) or len(s['texts']) > 20:
            raise ValueError('Invalid text list')
        number(s.get('fade_in', 0), 'fade in', 0, 2)
        number(s.get('fade_out', 0), 'fade out', 0, 2)
        for text in s['texts']:
            if not isinstance(text.get('text'), str) or len(text['text']) > 2000:
                raise ValueError('Text exceeds 2000 characters')
            number(text.get('x'), 'text x', 0, 1)
            number(text.get('y'), 'text y', 0, 1)
            number(text.get('font_size'), 'font size', 8, 300)
            if not re.fullmatch(r'#[0-9a-fA-F]{6}', text.get('color', '')):
                raise ValueError('Text color must be #rrggbb')
            if text.get('text_preset', 'caption') not in ('caption', 'title', 'fact', 'place'):
                raise ValueError('Invalid text preset')
            if text.get('text_animation', 'none') not in ('none', 'fade', 'slide-up'):
                raise ValueError('Invalid text animation')
            if text.get('font_weight', 'regular') not in ('regular', 'bold'):
                raise ValueError('Invalid font weight')
            if not re.fullmatch(r'#[0-9a-fA-F]{6}', text.get('background_color', '#000000')):
                raise ValueError('Text background must be #rrggbb')
            number(text.get('background_opacity', 0), 'text background opacity', 0, 1)
            number(text.get('elapsed', 0), 'text elapsed', 0, MAX_DURATION)
            number(text.get('clip_duration', s['duration']), 'text duration', .001, MAX_DURATION)
    if abs(total - duration) > .02:
        raise ValueError('Slice durations must equal timeline duration')
    for bed in beds:
        worker._key(bed.get('key'), 'audio key')
        number(bed.get('start'), 'audio start', 0, 86400)
        number(bed.get('delay'), 'audio delay', 0, MAX_DURATION)
        number(bed.get('duration'), 'audio duration', .001, MAX_DURATION)
        number(bed.get('volume'), 'audio volume', 0, 1)
        if bed.get('role', 'source') not in ('narration', 'source', 'music', 'sfx', 'ambience'):
            raise ValueError('Invalid audio role')
        number(bed.get('fade_in', 0), 'audio fade in', 0, 10)
        number(bed.get('fade_out', 0), 'audio fade out', 0, 10)
        if not isinstance(bed.get('duck_under_voice', False), bool):
            raise ValueError('duck_under_voice must be boolean')
    files = {}
    def source(key):
        if key not in files:
            dest = work / f'source-{len(files)}.media'
            worker._download(key, dest)
            files[key] = dest
        return files[key]
    segments = []
    for i, s in enumerate(slices):
        worker._disk(work)
        if s['kind'] == 'black':
            args = ['-f', 'lavfi', '-i', f'color=c=black:s={width}x{height}:r={fps}']
        elif s['kind'] == 'image':
            args = ['-loop', '1', '-i', str(source(s['key']))]
        else:
            args = ['-ss', str(s['start']), '-i', str(source(s['key']))]
        crop = s.get('crop')
        if crop and isinstance(crop, dict) and not (crop.get('x', 0) <= .001 and crop.get('y', 0) <= .001 and crop.get('w', 1) >= .999 and crop.get('h', 1) >= .999):
            # Normalized crop of the source frame, then fit to canvas.
            vf = (f"crop=iw*{crop['w']}:ih*{crop['h']}:iw*{crop['x']}:ih*{crop['y']},"
                  f'scale={width}:{height}:force_original_aspect_ratio=increase:force_divisible_by=2,'
                  f'crop={width}:{height},setsar=1,fps={fps},setpts=PTS-STARTPTS')
        else:
            vf = (f'scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2,'
                  f'pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps},setpts=PTS-STARTPTS')
        fade_in = min(s['duration'], s.get('fade_in', 0))
        fade_out = min(s['duration'], s.get('fade_out', 0))
        if fade_in > .001:
            vf += f',fade=t=in:st=0:d={fade_in}'
        if fade_out > .001:
            vf += f',fade=t=out:st={max(0, s["duration"] - fade_out)}:d={fade_out}'
        # Owner text never enters filter syntax; only generated text file names do.
        for j, text in enumerate(s['texts']):
            text_file = work / f'text-{i}-{j}.txt'
            text_file.write_text(text['text'], encoding='utf-8')
            elapsed = text.get('elapsed', 0)
            y = f"(h-text_h)*{text['y']}"
            if text.get('text_animation') == 'slide-up':
                y += f"+max(0\\,1-(t+{elapsed})/0.28)*28"
            alpha = ''
            if text.get('text_animation') in ('fade', 'slide-up'):
                alpha = f":alpha='min(1\\,(t+{elapsed})/0.24)'"
            opacity = text.get('background_opacity', 0)
            box = ''
            if opacity > 0:
                border = 22 if text.get('text_preset') == 'title' else 14
                box = f":box=1:boxcolor={text.get('background_color', '#000000')}@{opacity}:boxborderw={border}"
            regular_font = Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf')
            bold_font = Path('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf')
            serif_font = Path('/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf')
            wanted_font = serif_font if text.get('text_preset') == 'title' and text.get('font_weight') != 'bold' else bold_font if text.get('font_weight') == 'bold' else regular_font
            font = f':fontfile={wanted_font}' if wanted_font.exists() else ''
            vf += (f",drawtext=textfile='{text_file}':expansion=none:fontsize={text['font_size']}:"
                   f"fontcolor={text['color']}{font}:x=(w-text_w)*{text['x']}:y={y}:borderw=2:"
                   f"bordercolor=black@0.65{box}{alpha}")
        segment = work / f'timeline-{i}.mp4'
        worker._ffmpeg([*args, '-an', '-vf', vf, '-t', str(s['duration']), '-c:v', 'libx264',
                        '-preset', 'veryfast', '-crf', '20', '-pix_fmt', 'yuv420p',
                        '-threads', str(worker.THREADS), '-video_track_timescale', '90000', str(segment)])
        segments.append(segment)
    manifest = work / 'timeline-concat.txt'
    manifest.write_text(''.join(f"file '{p.name}'\n" for p in segments))
    assembled = work / 'timeline-video.mp4'
    worker._ffmpeg(['-f', 'concat', '-safe', '1', '-i', str(manifest), '-c', 'copy', str(assembled)])
    args = ['-i', str(assembled), '-f', 'lavfi', '-i', 'anullsrc=r=48000:cl=stereo']
    filters = [f'[1:a]atrim=duration={duration}[silence]']
    rendered_beds = []
    for i, bed in enumerate(beds):
        local = source(bed['key'])
        # Silent stock videos are normal. Do not fail the whole film for that bed.
        if not any(s.get('codec_type') == 'audio' for s in worker._probe(local)['streams']):
            continue
        input_index = 2 + len(rendered_beds)
        args += ['-i', str(local)]
        label = f'a{i}'
        fades = ''
        fade_in = min(bed['duration'], bed.get('fade_in', 0))
        fade_out = min(bed['duration'], bed.get('fade_out', 0))
        if fade_in > .001:
            fades += f',afade=t=in:st=0:d={fade_in}'
        if fade_out > .001:
            fades += f',afade=t=out:st={max(0, bed["duration"] - fade_out)}:d={fade_out}'
        filters += [f'[{input_index}:a]atrim=start={bed["start"]}:duration={bed["duration"]},asetpts=PTS-STARTPTS{fades},'
                    f'aresample=48000,volume={bed["volume"]},adelay={round(bed["delay"]*1000)}:all=1[{label}]']
        rendered_beds.append((f'[{label}]', bed))
    voices = [label for label, bed in rendered_beds if bed.get('role', 'source') in ('narration', 'source')]
    ducked = [label for label, bed in rendered_beds if bed.get('duck_under_voice', False) and bed.get('role') in ('music', 'ambience')]
    others = [label for label, bed in rendered_beds if label not in voices and label not in ducked]
    if len(voices) > 1:
        filters += [''.join(voices) + f'amix=inputs={len(voices)}:duration=longest:normalize=0[voiceMix]']
        voice = '[voiceMix]'
    else:
        voice = voices[0] if voices else ''
    if len(ducked) > 1:
        filters += [''.join(ducked) + f'amix=inputs={len(ducked)}:duration=longest:normalize=0[bedMix]']
        background = '[bedMix]'
    else:
        background = ducked[0] if ducked else ''
    final_labels = ['[silence]', *others]
    if voice and background:
        filters += [f'{voice}asplit=2[voiceSide][voiceFinal]']
        filters += [f'{background}[voiceSide]sidechaincompress=threshold=.035:ratio=8:attack=20:release=350[duckedBed]']
        final_labels += ['[voiceFinal]', '[duckedBed]']
    else:
        if voice:
            final_labels.append(voice)
        if background:
            final_labels.append(background)
    filters += [''.join(final_labels) + f'amix=inputs={len(final_labels)}:duration=longest:normalize=0,alimiter=limit=.95,atrim=duration={duration}[mix]']
    output = work / 'timeline-stitched.mp4'
    worker._ffmpeg([*args, '-filter_complex', ';'.join(filters), '-map', '0:v:0', '-map', '[mix]',
                    '-c:v', 'copy', '-c:a', 'aac', '-ar', '48000', '-ac', '2', '-t', str(duration),
                    '-movflags', '+faststart', str(output)])
    return output, {'width': width, 'height': height, 'fps': fps, 'clip_count': len(slices), 'timeline_version': 1}
