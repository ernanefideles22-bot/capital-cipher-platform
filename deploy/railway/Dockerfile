# E1 one-shot forensic auditor, isolated from main Railway application image.
# Build context is the repository root, so explicitly copy backend/.
# No frontend build, migrations, entrypoint, application worker, OMS, or exchange adapters.
FROM python:3.13-slim

WORKDIR /app
COPY backend/pyproject.toml backend/README.md ./
COPY backend/app ./app
COPY backend/scripts ./scripts
RUN pip install --no-cache-dir ".[postgres]"

RUN groupadd --gid 10001 capitalcipher \
    && useradd --uid 10001 --gid 10001 --no-create-home capitalcipher
USER 10001:10001

ENV SYSTEM_MODE=PAPER \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Runtime command is explicitly configured on the isolated db-audit service.
# Never start the backend API or change the principal staging service.
CMD ["python", "scripts/run_paper_e1_railway_audit.py"]
