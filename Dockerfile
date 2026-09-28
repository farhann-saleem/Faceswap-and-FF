FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    FACEFUSION_DIR=/opt/facefusion \
    CPU_THREADS=4 \
    OMP_NUM_THREADS=4 \
    OPENBLAS_NUM_THREADS=1 \
    ALLOW_GENERATE=0

# No CUDA, torch, Triton or GPU runtime. Canonical upstream pinned to a release.
# Pinned Node.js 20 LTS via official NodeSource repository, system Chromium, and deterministic typography fonts.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl ca-certificates git libgl1 libglib2.0-0 \
        chromium fonts-inter fonts-noto fonts-dejavu-core gnupg \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_20.x nodistro main" > /etc/apt/sources.list.d/nodesource.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*
RUN git clone --branch 3.3.2 --depth 1 https://github.com/facefusion/facefusion.git /opt/facefusion \
    && rm -rf /opt/facefusion/.git
COPY requirements.txt /app/requirements.txt
RUN grep -vE '^(gradio|gradio-rangeslider|psutil)([=<>]|$)' /opt/facefusion/requirements.txt > /tmp/ff-req.txt \
    && echo "psutil>=7.0.0" >> /tmp/ff-req.txt \
    && pip install --no-cache-dir --timeout 120 --retries 10 -r /tmp/ff-req.txt -r /app/requirements.txt \
    && pip check \
    && python -c "import onnxruntime as ort; assert 'CPUExecutionProvider' in ort.get_available_providers(); assert 'CUDAExecutionProvider' not in ort.get_available_providers()"



WORKDIR /opt/facefusion
COPY handler.py facefusion_cpu.py timeline_stitch.py composition_render.py /app/
# Validate the actual upstream parser and model catalog without downloading weights.
RUN python -c "import sys; sys.path.insert(0, '/app'); from facefusion_cpu import FaceFusionCPU; assert FaceFusionCPU().model_files()"

COPY remotion-renderer/package.json remotion-renderer/package-lock.json /app/remotion-renderer/
RUN cd /app/remotion-renderer && npm ci --omit=dev && npx remotion browser ensure
COPY remotion-renderer /app/remotion-renderer
ENV PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium

ARG WORKER_BUILD=cpu-v2
ENV WORKER_BUILD=${WORKER_BUILD}
CMD ["python", "-u", "/app/handler.py"]
