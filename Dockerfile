# syntax=docker/dockerfile:1
FROM oven/bun:1.3.14 AS frontend
WORKDIR /ui
COPY frontend/package.json frontend/bun.lock ./
RUN bun install --frozen-lockfile
COPY frontend/ ./
RUN bun run build

FROM python:3.12.14-slim-trixie AS app
COPY --from=ghcr.io/astral-sh/uv:0.12.22 /uv /uvx /usr/local/bin/
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 UV_PYTHON_DOWNLOADS=never UV_CACHE_DIR=/tmp/uv \
    PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH" HOME=/tmp
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/tmp/uv uv sync --frozen --no-dev --no-install-project --no-editable
COPY README.md ./
COPY src/ ./src/
COPY --from=frontend /ui/dist ./frontend/dist
RUN uv sync --frozen --no-dev --no-editable --no-cache
RUN groupadd --gid 10001 platform && useradd --uid 10001 --gid platform --no-create-home platform \
    && mkdir -p /data/services /data/imports \
    && touch /data/.initialized \
    && chown -R platform:platform /data
USER platform
EXPOSE 8765
ENTRYPOINT ["python", "-m", "btc_options.container"]
CMD ["dashboard"]
