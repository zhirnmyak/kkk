# syntax=docker/dockerfile:1
FROM node:24-alpine AS web
WORKDIR /webapp
COPY webapp/package.json webapp/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY webapp/index.html webapp/tsconfig.json webapp/vite.config.ts ./
COPY webapp/src/ ./src/
COPY webapp/design-reference/ ./design-reference/
RUN npm run build

FROM python:3.14-slim-bookworm AS runtime
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONUTF8=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH"
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates libgomp1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/* \
    && python -m venv /opt/venv
COPY requirements.txt /opt/requirements.txt
# CPU wheels keep the image usable without NVIDIA drivers and avoid CUDA packages.
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu \
      torch==2.15.0 torchvision==0.29.2 \
    && pip install --no-cache-dir -r /opt/requirements.txt \
    && pip check
RUN groupadd --gid 10001 maxiimka \
    && useradd --uid 10001 --gid maxiimka --create-home maxiimka \
    && mkdir -p /app/data /models /opt/maxiimka-config \
    && chown -R maxiimka:maxiimka /app /models /opt/maxiimka-config
WORKDIR /app
COPY --chown=maxiimka:maxiimka main.py config.py admin_setup.py runtime_settings.py console_settings.py \
    gateway_manager.py model_checks.py capability_cache.py reasoning_support.py vision_support.py \
    agent.py db.py llm.py rag.py ocr.py ocr_vision.py pptx_reader.py quota.py user_errors.py \
    study_intent.py scheduler.py imagegen.py ./
COPY --chown=maxiimka:maxiimka bot/ ./bot/
COPY --chown=maxiimka:maxiimka miniapp/ ./miniapp/
COPY --chown=maxiimka:maxiimka certs/ ./certs/
COPY --from=web --chown=maxiimka:maxiimka /webapp/dist/ ./webapp/dist/
COPY --chown=maxiimka:maxiimka docker/runtime.py docker/warm_models.py ./docker/
# The hackathon owner explicitly requested the configured credentials in this image.
COPY --chown=maxiimka:maxiimka docker/bundled/ /opt/maxiimka-config/
COPY --chown=maxiimka:maxiimka model-cache/ /models/
ENV DATA_DIR=/app/data HTTP_HOST=0.0.0.0 HTTP_PORT=8000 \
    MAXIMKA_CONTAINER=1 MAX_LOCAL_MINIAPP=1 MESSENGER=max UPDATES_MODE=polling \
    MAX_TOKEN_FILE=/opt/maxiimka-config/max_token.txt \
    HF_HOME=/models/huggingface EASYOCR_MODULE_PATH=/models/easyocr \
    ANONYMIZED_TELEMETRY=False TOKENIZERS_PARALLELISM=false
USER maxiimka
RUN python docker/warm_models.py
ENV HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
EXPOSE 8000
VOLUME ["/app/data"]
HEALTHCHECK --interval=15s --timeout=4s --start-period=45s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)" || exit 1
ENTRYPOINT ["python", "/app/docker/runtime.py"]

