"""Unit and integration tests for Remotion composition-v2, audio mastering, HTTP range server, and compatibility."""
import copy
import http.client
import json
import math
import os
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch, MagicMock
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if 'boto3' not in sys.modules:
    try:
        import boto3
    except ImportError:
        sys.modules['boto3'] = MagicMock()
        sys.modules['botocore'] = MagicMock()
        sys.modules['botocore.config'] = MagicMock()

if 'runpod' not in sys.modules:
    try:
        import runpod
    except ImportError:
        mock_runpod = MagicMock()
        mock_runpod.__version__ = '1.10.1'
        mock_runpod.serverless = MagicMock()
        sys.modules['runpod'] = mock_runpod

import composition_render
import handler as worker


class CompositionValidationTests(unittest.TestCase):
    def base_plan(self):
        return {
            'version': 2,
            'duration': 10.0,
            'width': 1280,
            'height': 720,
            'fps': 30,
            'scenes': [
                {
                    'id': 's1',
                    'kind': 'video',
                    'key': 'video1.mp4',
                    'timeline_start': 0.0,
                    'duration': 5.0,
                    'transition': 'cut',
                },
                {
                    'id': 's2',
                    'kind': 'black',
                    'timeline_start': 5.0,
                    'duration': 5.0,
                    'transition': 'fade',
                },
            ],
            'typography': [
                {
                    'id': 't1',
                    'text': 'Test caption',
                    'animation': 'fade',
                    'start': 1.0,
                    'duration': 3.0,
                    'x': 0.5,
                    'y': 0.8,
                    'font_size': 48,
                }
            ],
            'audio': [
                {
                    'key': 'narration.mp3',
                    'role': 'narration',
                    'start': 0.0,
                    'duration': 4.0,
                    'delay': 0.0,
                    'volume': 0.9,
                }
            ],
        }

    def test_valid_base_plan_passes(self):
        plan = self.base_plan()
        dur = composition_render._validate(plan)
        self.assertEqual(dur, 10.0)

    def test_all_9_transitions_pass_validation(self):
        all_transitions = ['cut', 'fade', 'push', 'wipe', 'whip', 'flash', 'zoom-blur', 'film-burn', 'map-zoom']
        self.assertEqual(len(all_transitions), 9)
        for trans in all_transitions:
            plan = self.base_plan()
            plan['scenes'][0]['transition'] = trans
            dur = composition_render._validate(plan)
            self.assertEqual(dur, 10.0)

    def test_all_typography_animations_pass_validation(self):
        all_animations = [
            'none', 'fade', 'slide-up', 'word-by-word', 'counter',
            'keyword-highlight', 'mask-reveal', 'spring-pop', 'tracking-blur',
            'map-callout', 'lower-third',
        ]
        for anim in all_animations:
            plan = self.base_plan()
            plan['typography'][0]['animation'] = anim
            if anim == 'map-callout':
                plan['typography'][0]['callout'] = {'label': 'LAKE NYOS', 'x': 0.5, 'y': 0.5}
            elif anim == 'lower-third':
                plan['typography'][0]['lower_third'] = {'primary': 'Speaker', 'secondary': 'Title'}
            dur = composition_render._validate(plan)
            self.assertEqual(dur, 10.0)

    def test_invalid_transition_rejected(self):
        for bad_trans in ('cube', 'spin', 'random', '', 123, None):
            plan = self.base_plan()
            plan['scenes'][0]['transition'] = bad_trans
            with self.assertRaises(ValueError):
                composition_render._validate(plan)

    def test_invalid_animation_rejected(self):
        for bad_anim in ('rotate', 'explode', 'flip', '', 456, None):
            plan = self.base_plan()
            plan['typography'][0]['animation'] = bad_anim
            with self.assertRaises(ValueError):
                composition_render._validate(plan)

    def test_out_of_bounds_and_invalid_numbers_rejected(self):
        invalid_mutations = [
            lambda p: p.update(duration=301),
            lambda p: p.update(duration=0.01),
            lambda p: p.update(duration=float('nan')),
            lambda p: p.update(duration=float('inf')),
            lambda p: p.update(duration=True),
            lambda p: p.update(width=1281),  # Odd width
            lambda p: p.update(height=721),  # Odd height
            lambda p: p.update(width=5000),  # > 3840
            lambda p: p.update(fps=0),
            lambda p: p.update(fps=65),
            lambda p: p.update(scenes=[]),
            lambda p: p['scenes'][0].update(timeline_start=8.0),  # 8.0 + 5.0 > 10.0
            lambda p: p['typography'][0].update(duration=15.0),  # > 10.0
            lambda p: p['audio'][0].update(volume=1.5),  # > 1.0
            lambda p: p['audio'][0].update(volume=-0.1),
        ]
        for mutate in invalid_mutations:
            plan = copy.deepcopy(self.base_plan())
            mutate(plan)
            with self.assertRaises(ValueError):
                composition_render._validate(plan)

    def test_non_string_and_unsafe_keys_rejected(self):
        bad_keys = [
            12345,
            '',
            '   ',
            'https://bucket.r2.cloudflarestorage.com/evil.mp4',
            'http://attacker.com/leak.mp4',
            '/etc/passwd',
            'data:video/mp4;base64,...',
        ]
        for bad in bad_keys:
            plan = copy.deepcopy(self.base_plan())
            plan['scenes'][0]['key'] = bad
            with self.assertRaises(ValueError):
                composition_render._validate(plan)


class MimeProbingAndHttpServerTests(unittest.TestCase):
    def test_detect_mime_from_probes(self):
        # Video probe MP4
        p_mp4 = {'streams': [{'codec_type': 'video', 'codec_name': 'h264'}], 'format': {'format_name': 'mov,mp4,m4a'}}
        self.assertEqual(composition_render._detect_mime(Path('source-0.media'), p_mp4), 'video/mp4')

        # Video probe WebM
        p_webm = {'streams': [{'codec_type': 'video', 'codec_name': 'vp9'}], 'format': {'format_name': 'matroska,webm'}}
        self.assertEqual(composition_render._detect_mime(Path('source-1.media'), p_webm), 'video/webm')

        # Image probe PNG
        p_png = {'streams': [{'codec_type': 'video', 'codec_name': 'png'}], 'format': {'format_name': 'image2,png_pipe'}}
        self.assertEqual(composition_render._detect_mime(Path('source-2.media'), p_png), 'image/png')

        # Image probe JPEG
        p_jpg = {'streams': [{'codec_type': 'video', 'codec_name': 'mjpeg'}], 'format': {'format_name': 'image2'}}
        self.assertEqual(composition_render._detect_mime(Path('source-3.media'), p_jpg), 'image/jpeg')

        # Audio probe MP3
        p_mp3 = {'streams': [{'codec_type': 'audio', 'codec_name': 'mp3'}], 'format': {'format_name': 'mp3'}}
        self.assertEqual(composition_render._detect_mime(Path('source-4.media'), p_mp3), 'audio/mpeg')

        # Audio probe WAV
        p_wav = {'streams': [{'codec_type': 'audio', 'codec_name': 'pcm_s16le'}], 'format': {'format_name': 'wav'}}
        self.assertEqual(composition_render._detect_mime(Path('source-5.media'), p_wav), 'audio/wav')

    def test_http_server_range_requests_and_mime(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            test_file = tmp / 'sample.media'
            data = b'0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz'
            test_file.write_bytes(data)
            total = len(data)

            mime_types = {'sample.media': 'video/mp4'}
            server = composition_render._serve(tmp, mime_types)
            try:
                port = server.server_address[1]
                base_url = f'http://127.0.0.1:{port}/sample.media'

                # 1. Full GET (no Range) -> 200 OK
                req = urllib.request.Request(base_url)
                with urllib.request.urlopen(req) as resp:
                    self.assertEqual(resp.status, 200)
                    self.assertEqual(resp.headers.get('Content-Type'), 'video/mp4')
                    self.assertEqual(int(resp.headers.get('Content-Length')), total)
                    self.assertEqual(resp.headers.get('Accept-Ranges'), 'bytes')
                    self.assertEqual(resp.read(), data)

                # 2. Byte range: start-end -> 206 Partial Content
                req = urllib.request.Request(base_url, headers={'Range': 'bytes=0-9'})
                with urllib.request.urlopen(req) as resp:
                    self.assertEqual(resp.status, 206)
                    self.assertEqual(resp.headers.get('Content-Range'), f'bytes 0-9/{total}')
                    self.assertEqual(int(resp.headers.get('Content-Length')), 10)
                    self.assertEqual(resp.read(), data[0:10])

                # 3. Byte range: start- (open ended) -> 206 Partial Content
                req = urllib.request.Request(base_url, headers={'Range': 'bytes=20-'})
                with urllib.request.urlopen(req) as resp:
                    self.assertEqual(resp.status, 206)
                    self.assertEqual(resp.headers.get('Content-Range'), f'bytes 20-{total - 1}/{total}')
                    self.assertEqual(int(resp.headers.get('Content-Length')), total - 20)
                    self.assertEqual(resp.read(), data[20:])

                # 4. Suffix range: -N -> 206 Partial Content
                req = urllib.request.Request(base_url, headers={'Range': 'bytes=-5'})
                with urllib.request.urlopen(req) as resp:
                    self.assertEqual(resp.status, 206)
                    self.assertEqual(resp.headers.get('Content-Range'), f'bytes {total - 5}-{total - 1}/{total}')
                    self.assertEqual(int(resp.headers.get('Content-Length')), 5)
                    self.assertEqual(resp.read(), data[-5:])

                # 5. Invalid range -> 416 Range Not Satisfiable
                req = urllib.request.Request(base_url, headers={'Range': 'bytes=500-600'})
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    urllib.request.urlopen(req)
                self.assertEqual(ctx.exception.code, 416)
                self.assertIn(f'bytes */{total}', ctx.exception.headers.get('Content-Range'))

                # 6. HEAD request -> 200 OK headers, no body
                conn = http.client.HTTPConnection('127.0.0.1', port)
                conn.request('HEAD', '/sample.media')
                resp = conn.getresponse()
                self.assertEqual(resp.status, 200)
                self.assertEqual(resp.getheader('Content-Length'), str(total))
                self.assertEqual(resp.getheader('Content-Type'), 'video/mp4')
                body = resp.read()
                self.assertEqual(len(body), 0)
                conn.close()

            finally:
                server.shutdown()
                server.server_close()


class AudioMixingAndLoudnessTests(unittest.TestCase):
    def test_audio_roles_ducking_peak_caps_and_youtube_loudnorm(self):
        calls = []
        def mock_ffmpeg(args):
            calls.append(args)
            Path(args[-1]).write_bytes(b'video-out')

        mock_worker = SimpleNamespace(
            _probe=lambda p: {'streams': [{'codec_type': 'audio'}]},
            _ffmpeg=mock_ffmpeg,
        )

        plan = {
            'duration': 12.0,
            'audio': [
                {'key': 'voice.mp3', 'role': 'narration', 'start': 0, 'duration': 6.0, 'delay': 0, 'volume': 1.0, 'duck_under_voice': False},
                {'key': 'source.mp3', 'role': 'source', 'start': 0, 'duration': 3.0, 'delay': 6.0, 'volume': 0.9, 'duck_under_voice': False},
                {'key': 'music.mp3', 'role': 'music', 'start': 0, 'duration': 12.0, 'delay': 0, 'volume': 0.2, 'duck_under_voice': True, 'fade_in': 1.0, 'fade_out': 1.5},
                {'key': 'ambience.mp3', 'role': 'ambience', 'start': 0, 'duration': 8.0, 'delay': 2.0, 'volume': 0.15, 'duck_under_voice': True},
                {'key': 'boom.mp3', 'role': 'sfx', 'category': 'impact', 'start': 0, 'duration': 1.5, 'delay': 1.0, 'volume': 0.5, 'duck_under_voice': False},
                {'key': 'riser.mp3', 'role': 'transition', 'category': 'whoosh', 'start': 0, 'duration': 1.0, 'delay': 5.5, 'volume': 0.4, 'duck_under_voice': False},
            ],
        }

        with tempfile.TemporaryDirectory() as tmp_dir:
            work = Path(tmp_dir)
            picture = work / 'picture.mp4'
            picture.write_bytes(b'fake-picture')
            files = {bed['key']: work / bed['key'] for bed in plan['audio']}
            for f in files.values():
                f.write_bytes(b'fake-audio')

            out, meta = composition_render._mix(picture, plan, files, work, mock_worker, 12.0)
            self.assertTrue(out.exists())
            self.assertEqual(meta['loudness_target']['integrated_lufs'], -14.0)
            self.assertEqual(meta['loudness_target']['true_peak_dbtp'], -1.5)

            # Examine FFmpeg filter complex
            filter_arg = calls[0][calls[0].index('-filter_complex') + 1]

            # 1. Voice dialogue loudnorm
            self.assertIn('loudnorm=I=-16:LRA=7:TP=-1.5', filter_arg)

            # 2. Category peak caps for SFX
            self.assertIn('alimiter=limit=0.7', filter_arg)   # Impact peak cap
            self.assertIn('alimiter=limit=0.65', filter_arg)  # Transition/whoosh peak cap

            # 3. Ducking sidechain compressor
            self.assertIn('asplit=2[voiceSide][voiceFinal]', filter_arg)
            self.assertIn('sidechaincompress=threshold=.035:ratio=8:attack=20:release=350[duckedBed]', filter_arg)

            # 4. Master YouTube loudness target
            self.assertIn('loudnorm=I=-14:LRA=7:TP=-1.5,alimiter=limit=0.84:level=0', filter_arg)


class WorkerCompatibilityTests(unittest.TestCase):
    def test_ping_returns_timeline_and_composition_v2_capabilities(self):
        res = worker.handler({'input': {'op': 'ping'}})
        self.assertTrue(res['ok'])
        self.assertEqual(res['timeline_version'], 1)
        self.assertEqual(res['composition_version'], 2)
        self.assertEqual(res['composition_engine'], 'remotion')
        for expected_feature in [
            'word-timing', 'kinetic-type', 'counters', 'masks',
            'map-callouts', 'lower-thirds', 'scene-transitions', 'audio-ducking',
        ]:
            self.assertIn(expected_feature, res['features'])

    def test_compose_v2_does_not_require_facefusion_readiness(self):
        # Simulate worker when FaceFusion has failed or is not initialized
        fake_plan = {
            'version': 2, 'duration': 4.0, 'width': 1280, 'height': 720, 'fps': 30,
            'scenes': [{'id': 's1', 'kind': 'black', 'timeline_start': 0, 'duration': 4, 'transition': 'cut'}],
        }
        with patch.object(worker, '_facefusion', None), \
             patch.object(worker, 'ALLOW', True), \
             patch.object(worker, '_disk', lambda *args, **kwargs: None), \
             patch.object(worker, '_r2', return_value=('bucket', MagicMock())), \
             patch('composition_render.render', return_value=(Path('/tmp/fake.mp4'), {'composition_version': 2})) as mock_compose:

            # Ensure temp file exists for upload
            Path('/tmp/fake.mp4').write_bytes(b'output')
            result = worker.handler({'input': {'op': 'compose_v2', 'composition': fake_plan}})
            self.assertTrue(result['ok'], result)
            self.assertEqual(result['op'], 'compose_v2')
            self.assertEqual(result['composition_version'], 2)
            mock_compose.assert_called_once()
            Path('/tmp/fake.mp4').unlink(missing_ok=True)

    def test_legacy_timeline_v1_routing(self):
        timeline_plan = {'version': 1, 'duration': 5, 'slices': [], 'audio': []}
        with patch.object(worker, 'ALLOW', True), \
             patch.object(worker, '_disk', lambda *args, **kwargs: None), \
             patch.object(worker, '_r2', return_value=('bucket', MagicMock())), \
             patch('timeline_stitch.render', return_value=(Path('/tmp/fake_timeline.mp4'), {'timeline_version': 1})) as mock_timeline:

            Path('/tmp/fake_timeline.mp4').write_bytes(b'timeline-output')
            result = worker.handler({'input': {'op': 'stitch', 'timeline': timeline_plan}})
            self.assertTrue(result['ok'], result)
            self.assertEqual(result['timeline_version'], 1)
            mock_timeline.assert_called_once()
            Path('/tmp/fake_timeline.mp4').unlink(missing_ok=True)

    def test_legacy_stitch_and_swap_ops_preserved(self):
        # Invalid inputs to verify legacy validation is reached without crashing
        with patch.object(worker, 'ALLOW', True):
            res_stitch = worker.handler({'input': {'op': 'stitch', 'clip_keys': []}})
            self.assertFalse(res_stitch['ok'])
            self.assertIn('clip_keys', res_stitch['error'])

            res_swap = worker.handler({'input': {'op': 'swap', 'source_key': 'a.png'}})
            self.assertFalse(res_swap['ok'])


if __name__ == '__main__':
    unittest.main()
