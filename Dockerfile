FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN uv sync --frozen --no-dev
CMD ["uv", "run", "uvicorn", "brigada.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
