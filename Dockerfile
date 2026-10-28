FROM python:3.12-slim
# Pinned: a mutable tag would let an upstream change alter every build.
COPY --from=ghcr.io/astral-sh/uv:0.13.3 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Dependencies first (cached layer), then the project itself.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev

# Run unprivileged: code execution in the API must not mean root in the container.
RUN useradd --system --uid 10001 --no-create-home app
USER app

EXPOSE 8000
CMD ["uvicorn", "brigada.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
