# Capital Cipher hosted PAPER backend, dashboard, and watchdog.
# The build context is the repository root (see fly.toml).
FROM node:22-slim AS dashboard-build

WORKDIR /workspace
RUN corepack enable
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml turbo.json ./
COPY frontend/package.json frontend/package.json
COPY backend/package.json backend/package.json
COPY packages/contracts/package.json packages/contracts/package.json
RUN pnpm install --frozen-lockfile --filter @capital-cipher/dashboard...
COPY frontend frontend
RUN pnpm --filter @capital-cipher/dashboard build

FROM python:3.13-slim

WORKDIR /app
COPY backend/pyproject.toml backend/README.md ./
COPY backend/app ./app
COPY backend/scripts ./scripts
COPY --from=dashboard-build /workspace/frontend/dist ./frontend-dist
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
