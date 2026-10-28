FROM python:3.13-slim
WORKDIR /app
COPY backend/pyproject.toml backend/README.md ./
COPY backend/app ./app
COPY backend/scripts ./scripts
RUN pip install --no-cache-dir . && useradd --uid 10001 --no-create-home demo
USER demo
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["python", "scripts/run_binance_demo.py"]
