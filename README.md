# RenderFlow — 可恢复、可观测的 AI 任务执行管线 MVP

动画素材处理场景的任务管线：任务由多个耗时步骤组成，单步失败可重试、Worker 进程中断可恢复、步骤结果可缓存复用，全程可观测。AI 应用开发岗位笔试项目（任务管线可观测方向）。

> 状态标记约定：✅ 已实现并有自动化测试覆盖 | 🧪 已实现（有演示脚本/冒烟验证）| 📋 仅有设计方案（未实现）

## 1. 背景与解决的问题

动画素材处理管线（解析→镜头分析→片段生成→音频→合成→导出）单任务耗时可达分钟到小时级。直接串行调用会遇到：
- 单步失败导致整个任务重跑，浪费已完成的昂贵计算
- Worker 进程崩溃后任务永久卡在中间态
- 上游无法回答"任务现在跑到哪一步、为什么失败、还要多久"
- 相同输入重复计算（如重导出同素材）

RenderFlow 把每个步骤变成带租约、可重试、可恢复、可缓存的独立执行单元，以 SQLite 为唯一事实源，提供步骤级可观测性。

## 2. 功能清单

✅ 6 步 DAG 工作流（依赖校验、步骤级调度）
✅ 任务/步骤双层状态机（非法迁移拒绝、终态不可覆盖）
✅ 原子步骤领取（BEGIN IMMEDIATE 事务 + 状态守卫）
✅ 租约心跳 + 过期回收 + lease_version fencing（旧 Worker 提交被拒）
✅ 7 类错误分类 → 差异化重试策略（指数退避 + 抖动 + next_retry_at 持久化）
✅ 结果缓存（cache_key 四元组、SHA-256 校验、命中走状态机、TTL 清理）
✅ 故障注入（仅测试环境开关，fail_first_n/always_fail/timeout/crash_worker）
✅ 手动重试 / 任务取消 API（幂等、依赖校验、409 冲突处理）
✅ 事件表 + JSON 行式结构化日志（14 类关键事件）
✅ REST API（OpenAPI 文档 /api/docs）
✅ React 前端三页面（列表/详情含 DAG/尝试详情，轮询降频）
🧪 5 个演示脚本（正常/重试恢复/Worker 重启/缓存命中/重试耗尽）

## 3. 架构

<!-- ASCII 架构图 W5 填 -->

技术栈：FastAPI + Pydantic v2 + sqlite3（标准库）+ 独立 Worker 进程；React + TypeScript + Vite + 原生 CSS。

## 4. 任务 DAG 与状态机

DAG：parse_assets → analyze_scenes → {generate_clips, generate_audio} → compose_video → export_result

任务状态机：PENDING → RUNNING → (RETRY_WAIT ↔ RUNNING) → SUCCEEDED | FAILED | CANCELLED（终态不可逆）
步骤状态机：PENDING → READY → RUNNING → SUCCEEDED | RETRY_WAIT → RUNNING | FAILED | SKIPPED

统一汇总函数 summarize_task（单一实现，API 与 Worker 共用）：
- 取消请求且无运行中步骤 → CANCELLED
- 任一步骤 FAILED → FAILED（下游标记 SKIPPED，避免永久 RUNNING）
- 任一步骤 RETRY_WAIT → RETRY_WAIT
- 全部 SUCCEEDED → SUCCEEDED
- 有 RUNNING → RUNNING；否则 PENDING/RUNNING(按 started_at)
进度 = SUCCEEDED 步骤数 / 总步骤数（等权；明确说明：这是步骤完成度，不是模型生成进度）

## 5. 并发控制（核心设计）

SQLite 单写者特性 + WAL 模式。并发控制策略：**IMMEDIATE 事务 + 乐观租约（lease_version 作 fencing token）**。

领取（单事务）：BEGIN IMMEDIATE → 依赖推进 → SELECT READY → UPDATE 为 RUNNING（WHERE status='READY' AND 非终态，校验 rowcount）→ INSERT attempt → COMMIT。
提交（单事务）：UPDATE ... WHERE lease_version=? AND worker_id=? AND status='RUNNING'；不匹配 → LeaseLostError，旧 Worker 结果被拒（STEP_FENCED 事件）。
数据库忙：busy_timeout=5000 + 指数退避重试（最多 5 次）。
**租约即宽限期**：不凭一次心跳缺失判死 Worker——只有 lease_until 真正过期才回收重领（RENDERFLOW_LEASE_SECONDS 就是容忍窗口）；回收时 lease_version 递增，旧 Worker 的任何迟到提交都会被 fencing 拒绝，不会覆盖新执行者的状态。
**INTERNAL_ERROR 保守视为不可重试**：内部错误可能意味着代码缺陷或数据损坏，盲目重试既浪费配额也可能放大故障，因此直接置 FAILED（可经手动重试 API 定向恢复）；可重试类仅限 TRANSIENT_ERROR / TIMEOUT / RESOURCE_UNAVAILABLE / WORKER_INTERRUPTED（指数退避 + 抖动）。
不承诺恰好一次执行：外部操作可能成功但状态未记录（at-least-once）；通过版本化输出路径（attempt_N 不可变目录）+ SHA-256 校验和 + 幂等提交（fencing 拒绝重复写入）控制该不确定性。

## 6. 数据模型

tasks / task_steps / step_attempts / step_cache / task_events 五表，字段与索引见 `backend/renderflow/db.py`（SCHEMA_MIGRATIONS 列表式迁移，启动幂等执行）。

## 7. 安装 / 启动 / 测试

<!-- W5 填真实命令与结果 -->

## 8. 故障注入演示

<!-- W5 填五个演示的命令与输出 -->

## 9. 自动化测试结果

<!-- W5 填真实通过数 -->

## 10. 已知限制与失败案例分析

<!-- W5 填 -->

## 11. 秒级 → 小时级任务的设计

<!-- W5 填：心跳续租节奏、检查点恢复 vs 重执行、GPU 调度（型号/设备ID/显存/排队时间）、资源配额、何时升级消息队列 -->

## 12. AI Coding 工具与 Token 用量 / 开发时间

<!-- W5 填真实记录 -->

## 13. 已实现 vs 仅设计

<!-- W5 填 -->
