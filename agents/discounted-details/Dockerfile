FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app ./app
COPY docs ./docs
COPY scripts ./scripts

RUN useradd --create-home dex && mkdir -p /data && chown dex /data
USER dex
ENV PATH="/app/.venv/bin:$PATH" DB_PATH=/data/agent.db

# Exactly one worker: the scheduler, debouncer and booking lock live in-process.
CMD ["sh", "-c", "uvicorn app.main:create_app --factory --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
