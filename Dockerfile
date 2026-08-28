# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

# Dependencies first so this layer only rebuilds when requirements.txt changes.
COPY requirements.txt .
RUN uv pip install --system --no-cache-dir -r requirements.txt

COPY ai_news.py emailer.py models.py sources.yaml PREFERENCES.md ./
COPY agents/ ./agents/

# Runs as an unprivileged user, not root.
RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p "/app/see news" \
    && chown -R appuser:appuser /app
USER appuser

# .env, sources.yaml, PREFERENCES.md, and "see news/" are meant to be mounted
# in (see README) so config changes and output don't require a rebuild.
ENTRYPOINT ["python", "ai_news.py"]
CMD ["--no-email"]
