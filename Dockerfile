FROM ghcr.io/astral-sh/uv:0.12.8 AS uv

FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

COPY --from=uv /uv /uvx /bin/

RUN useradd --create-home --uid 10001 app \
    && mkdir -p /app /var/lib/tola-bot/files \
    && chown -R app:app /app /var/lib/tola-bot

WORKDIR /app
COPY --chown=app:app pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY --chown=app:app app ./app
RUN uv sync --frozen --no-dev

USER app
EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
