# 本地运行证据（Local Run Evidence）

生成时间：2026-10-12 14:4x（CST）｜生成方式：真实进程运行 + CDP 无头浏览器截图，非 mock。

## 运行形态

生产单域名形态：`uvicorn renderflow.api:app --port 8920`（RENDERFLOW_STATIC_DIR 托管前端 dist）+ 独立 `python -m renderflow.worker` 进程。SQLite/产物在 /tmp/rf_localrun（当次运行目录）。

## run_report.json — 8/8 检查通过

| # | 检查 | 结果 |
|---|---|---|
| 1 | SPA 首页由后端托管（单域名生产形态） | PASS |
| 2 | 正常任务 SUCCEEDED（6/6 步，progress 100%） | PASS |
| 3 | 首败重试恢复（generate_clips 2 次尝试：1 FAILED + 1 SUCCEEDED） | PASS |
| 4 | 相同输入缓存命中（CACHE_HIT ×6） | PASS |
| 5 | 运行中取消（CANCELLED + cancel_requested） | PASS |
| 6 | 真实 SIGKILL 执行中 Worker（exit=-9） | PASS |
| 7 | 杀后恢复 SUCCEEDED（attempt#1 WORKER_INTERRUPTED → attempt#2 SUCCEEDED） | PASS |
| 8 | 恢复事件链（LEASE_EXPIRED + STEP_RECOVERED 各 1） | PASS |

## 截图（2880×1800 @2x，Chrome headless 真实渲染）

| 文件 | 内容 |
|---|---|
| 01_task_list_light.png | 任务列表（亮色）：6 任务、状态徽章、进度条、分页 |
| 02_task_detail_dag_light.png | 任务详情（亮色）：6 节点 DAG 状态图 + 步骤明细 + 事件时间线 |
| 03_task_detail_dark.png | 任务详情（暗色主题） |
| 04_task_running_detail.png | 运行中任务详情：DAG 节点黄/绿状态混合（真实执行中间态） |
| 05_recovered_attempts_dark.png | SIGKILL 恢复任务的 generate_audio 尝试历史展开（含 WORKER_INTERRUPTED） |
| 06_task_list_dark.png | 任务列表（暗色） |

## 日志与快照

- api.log：uvicorn 全程访问日志
- worker1_killed.log：被 SIGKILL 的 Worker（日志终止于 generate_audio 执行中）
- worker2_recovered.log：接管恢复的 Worker（可见回收与续跑）
- api_snapshot.json / task_ids.json：各验证任务 ID 与终态
- run_report.json：上表机器可读版

## 复现方式

```
cd backend && uv run pytest -q          # 26 项自动化测试
make demo-3                             # 真实杀进程恢复演示（DEMO OK 输出）
# 或完整本地生产形态（README §14 方案 B 的非容器等价形式）：
RENDERFLOW_STATIC_DIR=../frontend/dist uv run uvicorn renderflow.api:app --port 8920 &
uv run python -m renderflow.worker &
```
