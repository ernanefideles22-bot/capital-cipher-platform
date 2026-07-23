# Capital Cipher hosted PAPER backend and watchdog.
# The build context is the repository root (see fly.toml).
FROM python:3.13-slim

WORKDIR /app
COPY backend/pyproject.toml backend/README.md ./
COPY backend/app ./app
COPY backend/scripts ./scripts
RUN pip install --no-cache-dir ".[postgres]"

RUN groupadd --gid 10001 capitalcipher \
    && useradd --uid 10001 --gid 10001 --no-create-home capitalcipher \
    && mkdir -p /var/lib/capital-cipher/data-lake /run/secrets \
    && chown -R 10001:10001 /var/lib/capital-cipher /run/secrets

ENV SYSTEM_MODE=PAPER \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
EXPOSE 8000
USER 10001:10001
CMD ["python", "scripts/run_staging_backend.py"]
