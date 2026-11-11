# One image, three roles: API (default), daily report scheduler, sensor simulator.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first so code changes do not invalidate the dependency layer.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install ".[postgres]" \
 && useradd --create-home --uid 10001 cowshed \
 && mkdir -p /reports && chown cowshed /reports

USER cowshed
EXPOSE 8000

# Several worker processes: safe because state lives in PostgreSQL, not in the process.
CMD ["uvicorn", "--factory", "cowshed.api:create_app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4"]
