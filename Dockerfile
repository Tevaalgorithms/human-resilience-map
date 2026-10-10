# One image, many roles: api | frontend | worker | dispatcher | migrate | ingest
FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PATH="/app/.venv/bin:$PATH" \
    HRM_OBJECT_STORE_PATH=/data/objects
RUN useradd --system --uid 10001 --create-home app && mkdir -p /data/objects /data/imports /data/preparation && chown -R app /data
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
USER app
EXPOSE 8000 8050
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["api"]
