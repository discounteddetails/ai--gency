FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /usr/local/bin/uv

# ffmpeg: Nova samples video frames to check them for plates, house numbers and faces
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
# Chromium for Pixel's design renderer (shared path so the non-root user can run it too)
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/pw
RUN .venv/bin/playwright install --with-deps chromium && chmod -R a+rX /opt/pw

COPY app ./app
COPY docs ./docs
COPY scripts ./scripts

RUN useradd --create-home dex && mkdir -p /data && chown dex /data
# On Railway set RAILWAY_RUN_UID=0 so the root-owned /data volume is writable.
USER dex
ENV PATH="/app/.venv/bin:$PATH" DB_PATH=/data/agent.db

# Exactly one worker: the scheduler, debouncer and booking lock live in-process.
CMD ["sh", "-c", "uvicorn app.main:create_app --factory --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
