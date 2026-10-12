# 本地运行证据（Local Run Evidence）

生成时间：2026-10-12 14:4x 与 15:2x-15:3x（CST）｜生成方式：真实进程运行 + CDP 无头浏览器截图，非 mock。

## 长时间运行案例（新增，最有代表性）

**docs/evidence/long_task_recovery_case.json + 07_long_task_recovery_detail.png**
任务 433a4498：6 步 × 15s（约 90s 工作量），生产默认租约 15s / 心跳 5s，总耗时 389s。

| 时刻 | 真实事件 |
|---|---|
| 15:23:41 | parse_assets RUNNING；两次采样 6s 间隔确认心跳续租生效（heartbeat_at/lease_until 前进） |
| 15:24:11 | generate_clips 执行 30s 时对 Worker 进程真实 SIGKILL |
| 15:24:26 | 租约到期（15s 宽限期不误杀——不凭单次心跳缺失判死） |
| 15:24:26→15:29:34 | 边界案例：全机无存活 Worker，任务停在 RUNNING 2/6——状态可查、已成功的 parse_assets/analyze_scenes 零重跑；恢复需要至少一个存活 Worker 进程（如实记录） |
| 15:29:34 | 重启 Worker → 立即回收：LEASE_EXPIRED×1 + STEP_RECOVERED×1；悬挂 attempt#1 补记 FAILED/WORKER_INTERRUPTED（298s 含悬挂期） |
| 15:29:34-15:30:10 | attempt#2 重执行 15s 成功 → 后续 3 步续跑 → SUCCEEDED 6/6 |

JSON 内含完整事件流（22 条）、generate_clips 双 attempt 历史、各步骤 worker/lease_version 明细。

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
| 07_long_task_recovery_detail.png | 长任务案例详情页：generate_clips 双 attempt（WORKER_INTERRUPTED → SUCCEEDED）展开 |

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
