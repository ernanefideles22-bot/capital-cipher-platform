# Capital Cipher AI backend - PAPER by default and fail-closed staging.
FROM python:3.13-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY app ./app
COPY scripts ./scripts
RUN pip install --no-cache-dir ".[postgres]"

RUN groupadd --gid 10001 capitalcipher \
    && useradd --uid 10001 --gid 10001 --no-create-home capitalcipher \
    && mkdir -p /var/lib/capital-cipher/data-lake \
    && chown -R 10001:10001 /var/lib/capital-cipher

ENV SYSTEM_MODE=PAPER \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
EXPOSE 8000
USER 10001:10001
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
