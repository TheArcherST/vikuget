FROM ghcr.io/astral-sh/uv:0.11.10-python3.13-trixie-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

ENV PATH="/app/.venv/bin:$PATH"

CMD ["uvicorn", "--factory", "vikuget.app:create_app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
