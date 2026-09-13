"""Local integration tests: real ffmpeg, filesystem-backed R2 test double."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import handler as worker


class LocalR2:
    def __init__(self, root):
        self.root = root
        self.downloads = 0
        self.uploads = []

    def head_object(self, Bucket, Key):
        return {'ContentLength': (self.root / Key).stat().st_size}

    def download_file(self, bucket, key, dest):
        self.downloads += 1
        shutil.copyfile(self.root / key, dest)

    def upload_file(self, source, bucket, key, ExtraArgs):
        dest = self.root / key
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, dest)
        self.uploads.append((bucket, key, ExtraArgs))


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.client = LocalR2(self.root)
        self.patchers = [patch.object(worker, '_r2', return_value=('comfy', self.client)),
                         patch.object(worker, 'ALLOW', True), patch.object(worker, 'RESERVE_BYTES', 0)]
        for p in self.patchers:
            p.start()

    def tearDown(self):
        for p in reversed(self.patchers):
            p.stop()
        self.temp.cleanup()

    def clip(self, name, color, size, rate, audio=False):
        args = ['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i',
                f'color=c={color}:s={size}:r={rate}:d=0.6']
        if audio:
            args += ['-f', 'lavfi', '-i', 'sine=frequency=440:duration=0.6']
        subprocess.run(args + ['-c:v', 'libx264', '-threads', '1', '-pix_fmt', 'yuv420p',
                              '-t', '0.6', str(self.root / name)], check=True)

    def test_ping_and_gate_do_not_touch_r2(self):
        with patch.object(worker, 'ALLOW', False), patch.object(worker, '_r2', side_effect=AssertionError):
            worker.initialize()
            result = worker.handler({'input': {'op': 'ping'}})
            self.assertTrue(result['ok'])
            self.assertTrue({'ffmpeg', 'facefusion_ready', 'volume_mounted', 'free_gb'} <= result.keys())
            for op in ('swap', 'stitch'):
                self.assertIn('ALLOW_GENERATE', worker.handler({'input': {'op': op}})['error'])

    def test_validation_and_failures(self):
        for inp in ([], {'op': 'wat'}, {'op': 'stitch', 'clip_keys': []},
                    {'op': 'stitch', 'clip_keys': ['https://example.com/a.mp4']},
                    {'op': 'stitch', 'clip_keys': ['a.mp4'], 'width': 321},
                    {'op': 'stitch', 'clip_keys': ['a.mp4'], 'fps': float('nan')},
                    {'op': 'swap', 'source_key': 'x.mp4'}):
            self.assertFalse(worker.handler({'input': inp})['ok'])
        self.assertEqual(self.client.downloads, 0)
        result = worker.handler({'input': {'op': 'stitch', 'clip_keys': ['missing.mp4']}})
        self.assertFalse(result['ok'])
        self.assertIn('duration_ms', result)
        self.assertEqual(self.client.uploads, [])

    @unittest.skipUnless(shutil.which('ffmpeg'), 'ffmpeg required')
    def test_stitch_mixed_sizes_rates_and_audio(self):
        self.clip('a.mp4', 'red', '320x240', 24, audio=True)
        self.clip('b.mp4', 'blue', '240x320', 30)
        result = worker.handler({'input': {'op': 'stitch', 'clip_keys': ['a.mp4', 'b.mp4'],
                                          'width': 320, 'height': 240, 'fps': 25}})
        self.assertTrue(result['ok'], result)
        probe = worker._probe(self.root / result['output_key'])
        video = next(s for s in probe['streams'] if s['codec_type'] == 'video')
        self.assertEqual((video['width'], video['height'], video['r_frame_rate']), (320, 240, '25/1'))
        self.assertTrue(any(s['codec_type'] == 'audio' for s in probe['streams']))
        self.assertAlmostEqual(float(probe['format']['duration']), 1.2, delta=0.15)
        self.assertEqual(self.client.uploads[0][2]['ContentType'], 'video/mp4')
        self.assertNotIn('bytes', result)
        self.assertNotIn('base64', json.dumps(result))
        # Decode samples on each side of the seam: clip order must survive concat.
        for seconds, channel in [('0.2', 0), ('0.9', 2)]:
            data = subprocess.check_output(['ffmpeg', '-v', 'error', '-ss', seconds, '-i',
                str(self.root / result['output_key']), '-frames:v', '1', '-vf', 'scale=1:1',
                '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-threads', '1', '-'])
            self.assertGreater(data[channel], 100)

    @unittest.skipUnless(shutil.which('ffmpeg'), 'ffmpeg required')
    def test_short_soundtrack_does_not_truncate_video(self):
        self.clip('a.mp4', 'red', '320x240', 30)
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                        'sine=frequency=880:duration=0.15', str(self.root / 'audio.wav')], check=True)
        result = worker.handler({'input': {'op': 'stitch', 'clips': ['a.mp4'],
                                          'audio_key': 'audio.wav', 'width': 320, 'height': 240}})
        self.assertTrue(result['ok'], result)
        probe = worker._probe(self.root / result['output_key'])
        self.assertAlmostEqual(float(probe['format']['duration']), 0.6, delta=0.1)

    def test_swap_direction_and_cleanup(self):
        (self.root / 'scene.png').write_bytes(b'scene')
        (self.root / 'identity.png').write_bytes(b'identity')
        directories = []
        class Adapter:
            def swap(self, media, face, output, work):
                directories.append(work)
                assert media.read_bytes() == b'scene'
                assert face.read_bytes() == b'identity'
                output.write_bytes(b'output')
        with patch.object(worker, '_facefusion', Adapter()):
            result = worker.handler({'input': {'op': 'swap', 'source_key': 'scene.png',
                                              'target_face_key': 'identity.png'}})
        self.assertTrue(result['ok'], result)
        self.assertEqual((self.root / result['output_key']).read_bytes(), b'output')
        self.assertFalse(directories[0].exists())

    def test_atomic_model_cache_and_corruption_repair(self):
        prefix = self.root / worker.MODEL_PREFIX
        prefix.mkdir(parents=True)
        payload = b'model test data'
        (prefix / 'test.onnx').write_bytes(payload)
        (prefix / 'test.hash').write_text(f'{zlib.crc32(payload):08x}')
        links = self.root / 'assets'
        paths = [links / 'test.hash', links / 'test.onnx']
        class Adapter:
            def model_paths(self):
                return paths
        cache = self.root / 'cache'
        with patch.object(worker.os.path, 'ismount', return_value=True), patch.object(worker, 'MODEL_ROOT', cache):
            worker.ensure_weights(Adapter())
            self.assertTrue(paths[1].is_symlink())
            self.assertEqual(paths[1].read_bytes(), payload)
            count = self.client.downloads
            worker.ensure_weights(Adapter())
            self.assertEqual(self.client.downloads, count)
            (cache / 'test.onnx').write_bytes(b'corrupt')
            worker.ensure_weights(Adapter())
            self.assertEqual(paths[1].read_bytes(), payload)
        self.assertEqual(list(cache.glob('*.part')), [])

    def test_missing_volume_refuses_model_downloads(self):
        with patch.object(worker.os.path, 'ismount', return_value=False):
            with self.assertRaisesRegex(RuntimeError, 'not mounted'):
                worker.ensure_weights(None)
        self.assertEqual(self.client.downloads, 0)

    def test_partial_download_is_removed(self):
        (self.root / 'input.png').write_bytes(b'complete input')
        def interrupted(bucket, key, dest):
            Path(dest).write_bytes(b'partial')
            raise OSError('connection lost')
        dest = self.root / 'job' / 'input.png'
        with patch.object(self.client, 'download_file', side_effect=interrupted):
            with self.assertRaises(OSError):
                worker._download('input.png', dest)
        self.assertFalse(dest.exists())
        self.assertEqual(list(dest.parent.glob('*.part')), [])

    def test_upload_failure_returns_no_output_key_and_cleans_temp(self):
        directories = []
        def fake_stitch(inp, work):
            directories.append(work)
            output = work / 'out.mp4'
            output.write_bytes(b'output')
            return output, {}
        with patch.object(worker, 'stitch', side_effect=fake_stitch), \
             patch.object(self.client, 'upload_file', side_effect=OSError('upload failed')):
            result = worker.handler({'input': {'op': 'stitch'}})
        self.assertFalse(result['ok'])
        self.assertNotIn('output_key', result)
        self.assertFalse(directories[0].exists())


@unittest.skipUnless(Path(os.getenv('FACEFUSION_DIR', '/opt/facefusion')).is_dir(),
                     'pinned FaceFusion checkout required')
class FaceFusionContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cwd = Path.cwd()
        from facefusion_cpu import FaceFusionCPU
        cls.adapter = FaceFusionCPU()

    @classmethod
    def tearDownClass(cls):
        os.chdir(cls.cwd)

    def test_pinned_cpu_arguments_and_catalog(self):
        self.assertEqual(self.adapter.state.get_item('execution_providers'), ['cpu'])
        self.assertEqual(self.adapter.state.get_item('download_providers'), [])
        self.assertEqual(self.adapter.state.get_item('video_memory_strategy'), 'tolerant')
        names = {p.name for p in self.adapter.model_paths()}
        models = {'2dfan4', 'arcface_w600k_r50', 'bisenet_resnet_34', 'fairface',
                  'fan_68_5', 'inswapper_128', 'kim_vocal_2', 'nsfw_1', 'nsfw_2',
                  'nsfw_3', 'xseg_1', 'yoloface_8n'}
        self.assertEqual(names, {m + suffix for m in models for suffix in ('.onnx', '.hash')})

    def test_actual_onnx_cpu_inference(self):
        import numpy as np
        import onnx
        from onnx import helper, TensorProto
        from facefusion import inference_manager
        graph = helper.make_graph([helper.make_node('Identity', ['x'], ['y'])], 'cpu-smoke',
            [helper.make_tensor_value_info('x', TensorProto.FLOAT, [1])],
            [helper.make_tensor_value_info('y', TensorProto.FLOAT, [1])])
        model = helper.make_model(graph, opset_imports=[helper.make_opsetid('', 13)], ir_version=10)
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / 'identity.onnx')
            onnx.save(model, path)
            session = inference_manager.create_inference_session(path, '0', ['cpu'])
            self.assertEqual(session.get_providers(), ['CPUExecutionProvider'])
            self.assertEqual(session.run(None, {'x': np.array([42], dtype=np.float32)})[0].tolist(), [42])


if __name__ == '__main__':
    unittest.main()
