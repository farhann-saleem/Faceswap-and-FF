"""In-process adapter for the pinned FaceFusion 3.3.2 API (no CLI exits)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

FACEFUSION_DIR = Path(os.getenv('FACEFUSION_DIR', '/opt/facefusion'))
MODEL_DIR = FACEFUSION_DIR / '.assets' / 'models'


class FaceFusionCPU:
    def __init__(self):
        sys.path.insert(0, str(FACEFUSION_DIR))
        # FaceFusion discovers processors relative to its working directory.
        os.chdir(FACEFUSION_DIR)
        from facefusion import core, state_manager, logger
        from facefusion.args import apply_args
        from facefusion.program import create_program
        self.core, self.state, self.apply_args = core, state_manager, apply_args
        # Some parser factories inspect sys.argv, so hide RunPod CLI arguments.
        argv = sys.argv
        try:
            sys.argv = ['facefusion.py', 'headless-run', '--face-swapper-model', 'inswapper_128']
            self.program = create_program()
            self.defaults = vars(self.program.parse_args([
                'headless-run', '--execution-providers', 'cpu',
                '--execution-thread-count', os.getenv('CPU_THREADS', '4'),
                '--processors', 'face_swapper', '--face-swapper-model', 'inswapper_128',
                '--face-swapper-pixel-boost', '128x128',
                '--face-detector-model', 'yolo_face', '--face-landmarker-model', '2dfan4',
                '--face-selector-mode', 'one', '--face-selector-order', 'large-small',
                '--face-mask-types', 'box', '--video-memory-strategy', 'tolerant',
                '--output-video-encoder', 'libx264', '--output-audio-encoder', 'aac',
                '--log-level', 'info',
            ]))
        finally:
            sys.argv = argv
        # Allow upstream downloads — first worker downloads, uploads to R2 for future workers.
        apply_args(self.defaults, state_manager.init_item)
        logger.init('info')
        # ORT otherwise creates a machine-wide thread pool for every resident model.
        from facefusion import inference_manager
        import onnxruntime
        def cpu_session(model_path, execution_device_id, execution_providers):
            options = onnxruntime.SessionOptions()
            options.intra_op_num_threads = int(os.getenv('CPU_THREADS', '4'))
            options.inter_op_num_threads = 1
            return onnxruntime.InferenceSession(model_path, sess_options=options,
                                                providers=['CPUExecutionProvider'])
        inference_manager.create_inference_session = cpu_session
        self.modules = [core.content_analyser, core.face_classifier, core.face_detector,
                        core.face_landmarker, core.face_masker, core.face_recognizer,
                        core.voice_extractor]
        from facefusion.processors.modules import face_swapper
        self.modules.append(face_swapper)

    def model_dir(self):
        """Return the directory FaceFusion stores/expects models in."""
        return MODEL_DIR

    def model_files(self):
        """List .onnx and .hash files FaceFusion expects."""
        paths = set()
        for module in self.modules:
            if hasattr(module, 'collect_model_downloads'):
                hashes, sources = module.collect_model_downloads()
            else:
                options = module.get_model_options()
                hashes, sources = options['hashes'], options['sources']
            for item in [*hashes.values(), *sources.values()]:
                paths.add(Path(item['path']))
        return sorted(paths)

    def warm(self):
        if not self.core.pre_check() or not self.core.common_pre_check() or not self.core.processors_pre_check():
            raise RuntimeError('FaceFusion pre-check failed; models missing or corrupt')
        for module in self.modules:
            pool = module.get_inference_pool()
            if not pool or any(s.get_providers() != ['CPUExecutionProvider'] for s in pool.values()):
                raise RuntimeError(f'CPU inference pool unavailable: {module.__name__}')
        from facefusion.processors.modules import face_swapper
        face_swapper.get_static_model_initializer(str(next(
            p for p in self.model_files() if p.name == 'inswapper_128.onnx')))

    def swap(self, media: Path, face: Path, output: Path, work: Path):
        from facefusion import content_analyser, face_store, process_manager, video_manager
        from facefusion.vision import read_static_image
        args = dict(self.defaults, source_paths=[str(face)], target_path=str(media),
                    output_path=str(output), temp_path=str(work / 'frames'))
        self.apply_args(args, self.state.init_item)
        try:
            code = self.core.conditional_process()
            if code != 0 or not output.is_file() or output.stat().st_size == 0:
                raise RuntimeError(f'FaceFusion failed (code={code}); check worker logs')
        finally:
            process_manager.end()
            face_store.clear_static_faces()
            face_store.clear_reference_faces()
            read_static_image.cache_clear()
            content_analyser.analyse_image.cache_clear()
            content_analyser.analyse_video.cache_clear()
            video_manager.clear_video_pool()
