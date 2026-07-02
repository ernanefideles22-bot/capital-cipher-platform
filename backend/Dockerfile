# Capital Cipher AI backend — Phase 1 (PAPER mode only)
FROM python:3.13-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install --no-cache-dir .

ENV SYSTEM_MODE=PAPER
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
