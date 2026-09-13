FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    FACEFUSION_DIR=/opt/facefusion \
    CPU_THREADS=4 \
    OMP_NUM_THREADS=4 \
    OPENBLAS_NUM_THREADS=1 \
    ALLOW_GENERATE=0

# No CUDA, torch, Triton or GPU runtime. Canonical upstream pinned to a release.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl ca-certificates git libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*
RUN git clone --branch 3.3.2 --depth 1 https://github.com/facefusion/facefusion.git /opt/facefusion \
    && rm -rf /opt/facefusion/.git
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --timeout 120 --retries 10 -r /opt/facefusion/requirements.txt -r /app/requirements.txt \
    && pip check \
    && python -c "import onnxruntime as ort; assert 'CPUExecutionProvider' in ort.get_available_providers(); assert 'CUDAExecutionProvider' not in ort.get_available_providers()"

WORKDIR /opt/facefusion
COPY handler.py facefusion_cpu.py /app/
# Validate the actual upstream parser and model catalog without downloading weights.
RUN python -c "import sys; sys.path.insert(0, '/app'); from facefusion_cpu import FaceFusionCPU; assert FaceFusionCPU().model_files()"
ARG WORKER_BUILD=cpu-v1
ENV WORKER_BUILD=${WORKER_BUILD}
CMD ["python", "-u", "/app/handler.py"]
