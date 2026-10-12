#!/bin/sh
# 生产启动：Worker（后台独立进程）+ API（前台）。SQLite/产物都在 /data 持久卷。
set -e
mkdir -p /data/logs
python -m renderflow.db
python -m renderflow.worker &
WORKER_PID=$!
echo "worker pid=$WORKER_PID"
exec uvicorn renderflow.api:app --host 0.0.0.0 --port ${PORT:-10000}
