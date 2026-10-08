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

ARG APP_SOURCE_REVISION=""
ENV APP_SOURCE_REVISION=${APP_SOURCE_REVISION}

WORKDIR /app
COPY backend/pyproject.toml backend/README.md ./
COPY backend/app ./app
COPY backend/scripts ./scripts
COPY supabase/migrations ./migrations
COPY --from=dashboard-build /workspace/frontend/dist ./frontend-dist
RUN pip install --no-cache-dir ".[postgres]" \
    && apt-get update \
    && apt-get install -y --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 10001 capitalcipher \
    && useradd --uid 10001 --gid 10001 --no-create-home capitalcipher \
    && mkdir -p /var/lib/capital-cipher/data-lake /run/secrets \
    && chown -R 10001:10001 /var/lib/capital-cipher /run/secrets

COPY deploy/railway/entrypoint.sh /usr/local/bin/capital-cipher-entrypoint
RUN chmod 0755 /usr/local/bin/capital-cipher-entrypoint

ENV SYSTEM_MODE=PAPER \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
EXPOSE 8000
ENTRYPOINT ["/usr/local/bin/capital-cipher-entrypoint"]
CMD ["python", "scripts/run_railway_staging_backend.py"]
