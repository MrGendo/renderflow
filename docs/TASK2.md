# TASK2 — RenderFlow W2：重试/租约恢复/并发/fencing/缓存/故障注入

你是资深 Python 后端工程师。工作目录 `/Users/Admin/Downloads/project/written_test`。先读 `docs/PLAN.md` 与 `backend/` 现有代码（W1 已完成：DB/状态机/DAG/API/Worker 成功路径）。本任务在 W1 基础上补齐恢复与可靠性能力。**不要重写 W1 已有的正确代码，增量修改，保持向后兼容。** 你不要 git commit。

## 环境
- `cd backend && uv run pytest -q` 是既有测试（T1-T4），你改动后必须保持全绿。
- macOS，Python 3.11 venv 由 uv 管理。

## 交付范围

### 1. 重试引擎（retry 逻辑集中在一个模块，Worker 调用）
- 错误分类 → 重试策略映射：
  - TRANSIENT_ERROR / TIMEOUT / RESOURCE_UNAVAILABLE / WORKER_INTERRUPTED：可重试，指数退避 backoff = min(base * 2^(attempt-1), max) + uniform(0, base*0.3) 抖动
  - INVALID_INPUT / OUTPUT_INVALID / INTERNAL_ERROR：不可重试 → 步骤直接 FAILED（INTERNAL_ERROR 保守视为不可重试，README 会说明）
- 失败处理流：attempt 置 FAILED(带 error_code/message/duration) → 若可重试且 attempt_count < max_attempts：step 置 RETRY_WAIT + next_retry_at=now+backoff + 事件 STEP_RETRY_SCHEDULED → summarize → 任务 RETRY_WAIT；若不可重试或超限：step 置 FAILED + 事件 STEP_FAILED → summarize → 任务 FAILED + 下游全部标 SKIPPED（递归，error_code=BLOCKED_BY_UPSTREAM，事件 STEP_SKIPPED）→ 任务 finished_at。
- RETRY_WAIT → READY 推进：Worker 轮询时发现 next_retry_at <= now 且依赖仍满足 → READY（事件 STEP_READY）。
- Worker 执行中收到 SIGKILL/崩溃 → 步骤停留 RUNNING 且租约会过期（本任务不做回收，TASK3 做）——但 attempt 状态要能体现中断：executor 心跳线程发现 stop_flag 时 attempt 标 INTERRUPTED?（不做，留给回收器写 WORKER_INTERRUPTED）。

### 2. 租约心跳与过期回收（Worker + 回收器合一，都在 worker.py）
- 心跳：executor 分片执行期间每 RENDERFLOW_HEARTBEAT_SECONDS 续租（UPDATE heartbeat_at + lease_until WHERE step_id AND worker_id AND lease_version AND status='RUNNING'）。
- 过期回收：Worker 主循环每轮先扫描 `status='RUNNING' AND lease_until < now`（W1 领取时写了 lease_until）：
  - 找到过期步骤 → 事务内重新原子领取：lease_version+=1、新 worker_id、新 lease_until、attempt_count+=1、新 attempt 行（前一 attempt 由回收器补记 WORKER_INTERRUPTED，finished_at=now，事件 STEP_RECOVERED + LEASE_EXPIRED）
  - 不能仅凭一次心跳缺失判死：只有 lease_until 真正过期才回收（租约即宽限期，RENDERFLOW_LEASE_SECONDS 就是容忍窗口，README 说明）
- **fencing**：所有 RUNNING 态提交（成功/失败/心跳）SQL 必须带 `AND worker_id=? AND lease_version=?`；不匹配 → 抛 LeaseLostError，Worker 放弃该步骤（不回滚新 Worker 的状态，事件 STEP_FENCED）。
- touch_lease 失败（LeaseLostError）时 executor 的分片 sleep 循环要尽快中止当前执行（检查点）。

### 3. 结果缓存（dao + storage + worker 集成）
- cache_key = sha256("v1|" + step_type + "|" + input_hash + "|" + step_version + "|" + config_hash)
- Worker 领取前检查（在领取事务内或紧邻事务）：查 step_cache 有 key 且 expires_at > now → 验证 output_ref 文件存在 + sha256 匹配（不匹配视为未命中，删除该缓存行）→ 命中：创建 attempt(status='SUCCEEDED', worker_id='cache', duration_ms=0, log_ref 记录命中事件) + step SUCCEEDED(output_ref 复用, output_checksum 复用) + 事件 CACHE_HIT + summarize。**缓存命中也走状态机，不绕过。**
- 未命中且执行成功：成功提交流程末尾（step 更新成功后）INSERT OR REPLACE INTO step_cache。只有成功且校验通过的结果进缓存。
- 缓存 TTL：RENDERFLOW_CACHE_TTL_SECONDS 默认 86400；Worker 每轮惰性清理：DELETE FROM step_cache WHERE expires_at < now（低频，每 50 轮一次即可）。
- 缓存禁用开关：RENDERFLOW_CACHE_ENABLED 默认 1。

### 4. 故障注入（仅测试/演示）
- 任务创建 payload 可带 `fault_injection: {"<step_type>": {...}}`，每步支持：
  - fail_first_n_attempts: int（前 N 次 attempt 失败后成功）
  - failure_type: TRANSIENT_ERROR|TIMEOUT|INVALID_INPUT|RESOURCE_UNAVAILABLE|OUTPUT_INVALID|INTERNAL_ERROR 之一
  - always_fail: true（每次都失败）
  - timeout_after_s: float（执行该秒数后触发 TIMEOUT——executor sleep 超过步骤 timeout_s 即抛 StepTimeout）
  - crash_worker: true（executor 执行中 os._exit(137) 模拟进程死亡——仅演示/测试用）
- **生效门**：env RENDERFLOW_ALLOW_FAULT_INJECTION=1 才解析该字段，否则忽略并在响应中注明 ignored。默认 .env.example 里 =0。
- fault 配置存 tasks.input_json 里（随任务持久化，Worker 重启后仍生效），但 API 响应中 echo 出来便于演示。
- 实现位置：executor 读 task 的 fault_injection + step_type + 当前 attempt_no 决定行为。

### 5. API 增量
- POST /api/steps/{step_id}/retry：仅当 step 处于 FAILED（或 RETRY_WAIT）时允许；校验依赖全 SUCCEEDED（不满足 → 409 DEPENDENCY_NOT_SATISFIED）；重置 attempt_count 语义 = 允许再试 max_attempts 次（新增 retry_round 计数或把 attempt_count 归零但保留历史 attempts 行——选后者，attempt_no 继续递增不覆盖历史，attempt_count 重置为 0）；触发下游 SKIPPED 步骤重新评估为 PENDING（依赖恢复后可再执行）；任务若 FAILED → 回 RUNNING；响应含新的 retry 语义；重复并发请求必须幂等（事务 + 状态守卫，第二个请求得到 409 ALREADY_RETRYING 或幂等成功——选幂等成功，返回相同结果）。事件 STEP_RETRY_MANUAL。
- POST /api/tasks/{task_id}/cancel：写 cancel_requested=1 + 事件 TASK_CANCEL_REQUESTED；READY/PENDING/RETRY_WAIT 步骤 → CANCELLED（或 SKIPPED？统一用 CANCELLED 步骤终态——**注意 StepStatus 枚举没有 CANCELLED，用 SKIPPED + error_code=CANCELLED_BY_USER**）；RUNNING 步骤不立即中断（executor 分片检查 cancel_requested，尽快自然停止并标 SKIPPED(CANCELLED_BY_USER)）；任务在全部步骤到达终态后 → CANCELLED；若任务已终态 → 409。
- GET /api/tasks/{task_id} 响应增加 cancel_requested 字段。

### 6. 测试（新增文件，保持 T1-T4 绿）
- test_retry.py：T5 首败后成（fail_first_n_attempts=1 + TRANSIENT → RETRY_WAIT → 成功，任务 SUCCEEDED，attempts 2 条历史保留）；T6 INVALID_INPUT 直接 FAILED 不重试；T7 always_fail + max_attempts=2 → 2 次后 FAILED，下游 SKIPPED，任务 FAILED，finished_at 非空；T8 上游失败期间轮询 worker 多轮，断言下游从未进入 READY/RUNNING。
- test_recovery.py（进程内模拟）：T10 租约过期重领（手动把 step 置 RUNNING + 过期 lease_until 模拟失联，跑 worker 一轮 → 新 attempt、STEP_RECOVERED 事件）；T11 fencing（伪造旧 worker 提交：用旧 lease_version 调 dao.complete_step → 断言 LeaseLostError 且 DB 状态未被旧结果覆盖）。
- test_cache.py：T13 同输入第二任务命中缓存（attempt=CACHE_HIT、duration_ms≈0、事件 CACHE_HIT、output_ref 与第一任务相同但路径是缓存条目引用）；T14 输入变化（assets 加一项）→ 不命中；T15 环境变量强制 step_version 变化（monkeypatch workflow 版本）→ 不命中；T16 删除缓存文件 / 篡改内容 → 不命中且原缓存行被清。
- test_concurrency.py：T17 两个线程各跑一个 worker 实例（同 DB），创建任务后并发执行，断言每个 step 恰好被领取一次（attempt 行数 == 领取次数，无重复 attempt_no），任务最终 SUCCEEDED；T18 对同一 FAILED step 并发发 10 个 retry 请求 → 断言最终状态一致（只产生一组新 attempts，无中途态泄漏）；T19 直接用两个连接对 DB 施加写锁（BEGIN IMMEDIATE 挂住）+ worker 领取 → busy 重试成功而不是崩。
- test_cancel.py：T20a 任务运行中取消 → 未开始步骤 SKIPPED(CANCELLED_BY_USER)、任务 CANCELLED、finished_at 非空；T20b 取消已完成任务 → 409；T20c 取消后任务绝不会被标 SUCCEEDED（轮询 worker 若干轮验证）。
- 真实子进程杀灭测试（T9）**本任务不做**，TASK3 单独做。
- 所有故障场景测试用毫秒级配置（env 注入），全套测试目标 < 120s。

## 完成标准
1. `uv run pytest -q` 全绿（T1-T20c，真实通过数汇报）。
2. 汇报：修改/新增文件清单、测试输出原文、设计取舍（尤其 retry 幂等语义与 fencing 实现）、偏离任务书的决定。
