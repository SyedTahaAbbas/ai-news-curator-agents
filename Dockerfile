# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

# The package and its dependencies. pyproject.toml is the single source of
# truth for both, so there is no requirements.txt to keep in lockstep.
COPY pyproject.toml README.md ./
COPY ainews/ ./ainews/
RUN uv pip install --system --no-cache-dir .

# Config lives beside the data, not inside the install: ainews/paths.py
# resolves sources.yaml, PREFERENCES.md and the output from the working
# directory, which is why these are mounted rather than baked in (see README).
COPY sources.yaml PREFERENCES.md ./

# Runs as an unprivileged user, not root.
RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p "/app/see news" \
    && chown -R appuser:appuser /app
USER appuser

ENTRYPOINT ["ai-news"]
CMD ["--no-email"]
