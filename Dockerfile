FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir --require-hashes -r requirements.txt

COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir --no-deps .

CMD ["uvicorn", "--factory", "vikuget.app:create_app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
