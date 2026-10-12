# TASK3 — RenderFlow W3：真实进程中断恢复测试 + 演示脚本

你是资深 Python 测试工程师。工作目录 `/Users/Admin/Downloads/project/written_test`。先读 `docs/PLAN.md` 与 backend/ 现有代码（W1+W2 已完成）。本任务做两件事：**真实子进程级**的 Worker 中断恢复测试，以及 5 个可重复执行的演示脚本。不要改动 renderflow/ 包的业务逻辑，除非测试暴露真实 bug——若发现 bug，最小修复并在汇报中说明。你不要 git commit。

## 背景
W2 的 test_recovery.py 是进程内模拟（手动改 DB 模拟失联）。题目明确要求：「对于真实进程中断测试，应启动独立 Worker 子进程，实际终止进程，再验证重启恢复。不要只通过把数据库状态手动修改为 RUNNING 来冒充进程中断测试。」本任务补齐这一点。

## 1. 真实进程中断测试 tests/test_process_recovery.py（T9 真实版）

基础设施（放 tests/ 下）：
- `worker_proc.py` helper（或 conftest fixture）：以 `subprocess.Popen([sys.executable, "-m", "renderflow.worker"], env={...毫秒级配置...})` 启动真实 Worker 子进程；提供 wait_until(predicate, timeout) 轮询辅助（直接查 DB，不用 API，避免端口依赖）。

测试场景（每个都要真实子进程 + 真实 SIGKILL）：
- **test_worker_killed_mid_step_recovers**：创建任务（generate_clips step_durations 设为 5000ms 让它有充足执行窗口；fault_injection 不用）。启动 Worker 子进程 → 轮询 DB 等 generate_clips 进入 RUNNING → `proc.kill()` (SIGKILL) + wait() 确认死亡 → 断言此刻 step 仍 RUNNING（真实失联状态，非手动改库）→ 等 lease_until 过期（测试用 RENDERFLOW_LEASE_SECONDS=2 压缩）→ 重新启动新 Worker 子进程 → 断言：generate_clips 被重新执行（新 attempt 行，旧 attempt 最终被标 WORKER_INTERRUPTED）、任务最终 SUCCEEDED、全部 6 步 SUCCEEDED、已成功步骤（parse_assets/analyze_scenes）未重复执行（attempt 数不变，结果复用）。
- **test_worker_killed_and_restarted_multiple_times**：crash_worker 故障注入（RENDERFLOW_ALLOW_FAULT_INJECTION=1）+ fail_first：第一次 Worker 跑到 generate_audio 时 os._exit(137) 自杀（fault 注入）→ 重启 Worker → 最终 SUCCEEDED；断言 attempt 历史完整保留。
- **test_two_real_worker_processes**：同时启动两个真实 Worker 子进程 + 两个普通任务 → 断言全部成功且每个 step 的 RUNNING attempt 在任一时刻唯一（可用事件表 STEP_CLAIMED 序列断言无同 step 交叠领取），总 attempt 数 == 总 step 数（无重复执行）。
- **test_data_survives_restart**：任务跑到中途 kill Worker → 重启 → 完成后重启 API 进程（TestClient 重建亦可，但至少验证 DB 重连后）历史与终态可查（T12 真实版）。

注意事项：
- 子进程 env 必须显式传全部 RENDERFLOW_* 变量 + PYTHONPATH/PATH；stdout/stderr 重定向到 tmp 文件，失败时在断言消息里带出日志尾部（便于诊断）。
- 每个测试用独立 tmp DB 目录（conftest 已有模式）。
- macOS 上 SIGKILL 子进程要 wait() 防 zombie。
- 全部测试串行跑（真实进程测试不并行），目标 < 90s。

## 2. 演示脚本 scripts/（backend/scripts/）
每个脚本：独立可重复运行、打印带时间戳的进度叙述、结束时打印结论行（DEMO OK: ... 或 DEMO FAILED: ...）、退出码非 0 表示失败。脚本直接用 dao/api 层，必要时起子进程 worker。
- `demo_1_normal.py`：正常任务全流程（可指定 --allow-fault 默认关）→ SUCCEEDED。
- `demo_2_retry_recover.py`：generate_clips fail_first_n_attempts=1 TRANSIENT → 展示 RETRY_WAIT → SUCCEEDED，打印 attempts 历史。
- `demo_3_worker_restart.py`：真实杀 Worker 子进程（sleep 到 RUNNING 再 kill）→ 租约过期 → 重启 → SUCCEEDED，打印恢复叙述（旧 attempt WORKER_INTERRUPTED + 新 attempt 成功）。
- `demo_4_cache_hit.py`：任务 A 正常跑完 → 任务 B 相同输入 → 打印 CACHE_HIT 与复用的 output_ref；再跑任务 C 改输入 → 不命中。
- `demo_5_retry_exhausted.py`：export_result always_fail（max_attempts=2）→ 任务 FAILED、下游无、步骤 SKIPPED 不存在（export_result 是最后一步）→ 展示 FAILED + error_summary + 手动 retry 一次成功（fail 改 1 次即过：用 fail_first_n_attempts=1 + max_attempts=1，先耗尽 FAILED，再手动 retry API 成功）。
- 公共参数：--db/--data-dir 覆盖（默认 demo 专用 ./data/demo，每次运行清空重建，保证可重复）。
- 顶部统一打印演示编号与目的。

## 3. Makefile 增量
demo-1...demo-5 目标（cd backend && uv run python scripts/demo_N.py）。

## 完成标准
1. `uv run pytest -q` 全绿（W1+W2+W3 全部测试）。
2. 5 个演示脚本各自真实跑一遍并贴输出原文（DEMO OK 行必须真实出现）。
3. 汇报：新增文件、测试与演示输出原文、发现并修复的 bug（如有）、任何偏离。
