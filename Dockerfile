# 多阶段构建：前端静态产物由后端 FastAPI 直接托管（单一域名，免 CORS）
FROM node:24-slim AS frontend
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.11-slim AS backend
WORKDIR /app
COPY backend/pyproject.toml ./
COPY backend/renderflow ./renderflow
RUN pip install --no-cache-dir -e .
COPY --from=frontend /app/frontend/dist ./static
ENV RENDERFLOW_STATIC_DIR=/app/static \
    RENDERFLOW_DB_PATH=/data/renderflow.db \
    RENDERFLOW_DATA_DIR=/data \
    RENDERFLOW_LOG_DIR=/data/logs \
    RENDERFLOW_ALLOW_FAULT_INJECTION=0 \
    PYTHONUNBUFFERED=1
# 同容器两个独立进程：API + Worker（SQLite 需共享文件系统；详见 render.yaml 注释）
COPY scripts/start_prod.sh /start_prod.sh
RUN chmod +x /start_prod.sh
EXPOSE 10000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:10000/api/health',timeout=4)" || exit 1
CMD ["/start_prod.sh"]
