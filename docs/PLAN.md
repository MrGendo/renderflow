# RenderFlow — 实施计划（阶段 1 输出）

日期：2026-10-12 | 场景：动画素材处理任务管线（AI 应用开发笔试 · 任务管线可观测方向）

## 0. 当前环境与工作区

| 项 | 结论 |
|---|---|
| 工作区 | `/Users/Admin/Downloads/project/written_test`，空目录，git 已 init（main 分支），无现有代码可复用 |
| Python | PATH 上 python3=3.9.6（太旧）；可用 `/opt/homebrew/bin/python3.11`；有 `uv`。→ 统一用 uv 建 3.11 venv |
| Node | v24.16.0 / npm 11.13.0（满足 Vite+React+TS） |
| SQLite | sqlite3 3.43.2 CLI 可用；驱动用 Python 内置 sqlite3 |
| 其他 | 无 Docker、无 tmux、无 GPU；单机运行符合题目要求 |
| 代理 | 本机有 Vortex 代理，测试与本地 HTTP 访问一律走 --noproxy '*' / no_proxy |

技术栈锁定：FastAPI + Pydantic v2 + sqlite3(标准库, 手写 DAO 层) + 独立 Worker 进程；前端 React+TS+Vite+原生 CSS。

## 1. 项目结构（目标）

```
written_test/
├── README.md
├── .env.example
├── .gitignore
├── Makefile                  # install/dev/stop/test 一站式
├── backend/
│   ├── pyproject.toml        # uv 管理
│   ├── renderflow/           # 包根
│   │   ├── config.py         # 环境变量配置
│   ├── tests/                # pytest（含真实子进程杀灭测试）
│   ├── scripts/              # demo_*.py 演示脚本
├── frontend/
│   ├── src/
│   │   ├── api.ts            # 真实 API 客户端
│   │   ├── pages/            # TaskList / TaskDetail / AttemptDetail
│   │   └── hooks/useTaskPolling.ts  # 轮询（终态降频）
└── docs/                     # 本计划、测试报告
```

## 2. 数据模型（4 张核心表 + 1 张事件表）

tasks / task_steps / step_attempts / step_cache 按题目字段实现；另加 **task_events**
（状态事件表，可观测性刚需：TASK_CREATED/STEP_CLAIMED/…/TASK_FAILED，供事件查询接口与前端时间线复用）。
心跳不逐条入库：heartbeat 只 UPDATE task_steps.heartbeat_at/lease_until（覆盖式，保留最近一次），完整诊断日志写文件。

- 主键：tasks.task_id TEXT(PK)；task_steps.step_id TEXT(PK)+UNIQUE(task_id,step_type)；step_attempts.attempt_id TEXT(PK)+UNIQUE(step_id,attempt_no)；step_cache.cache_key TEXT(PK)
- 外键：task_steps.task_id→tasks ON DELETE CASCADE；step_attempts.step_id→task_steps ON DELETE CASCADE
- 索引：task_steps(status,lease_until)、task_steps(task_id)、step_attempts(step_id)、tasks(status,created_at)、task_events(task_id,created_at)
- step_steps 传递依赖用 JSON 数组存 dependencies（DAG 固定 6 节点，不做闭包表）
- WAL 模式 + busy_timeout=5000 + IMMEDIATE 事务

## 3. 状态机

任务：PENDING→RUNNING→{RETRY_WAIT↔RUNNING}→SUCCEEDED|FAILED|CANCELLED（终态不可逆，UPDATE 带 WHERE status IN (非终态) 守卫）
步骤：PENDING→READY→RUNNING→{SUCCEEDED|RETRY_WAIT→RUNNING|FAILED|SKIPPED}
非法迁移直接拒绝（状态机模块集中校验，API/Worker 共用一份实现 → 满足「统一汇总函数，不复制逻辑」）。

统一汇总函数 summarize_task(task, steps) → 处理六种情况：执行中/等待重试/依赖阻塞/失败终止/用户取消/全部成功。
下游步骤在依赖失败后标记 SKIPPED（原因 blocked_by_failed_dependency）→ 避免任务永久 RUNNING。
进度 = SUCCEEDED 步骤数 / 总步骤数（等权，README 明示规则，不伪装模型进度）。

## 4. DAG（固定 6 步）

parse_assets → analyze_scenes → {generate_clips, generate_audio} → compose_video → export_result
输入输出引用：step 表只存 output_ref（相对路径）+output_checksum(sha256)，大对象落 data/ 目录，按 tasks/<tid>/steps/<stype>/attempt_<n>/ 版本化不可变路径。

## 5. 并发控制策略（明确选型）

**SQLite IMMEDIATE 事务 + 乐观租约（lease_version fencing token）**：
领取 = 单条事务内 `BEGIN IMMEDIATE; SELECT ... WHERE status='READY' AND (lease_until IS NULL OR lease_until<now) LIMIT 1; UPDATE status='RUNNING', lease_version+=1, worker_id=? ...; COMMIT`。
提交结果 = `UPDATE ... WHERE step_id=? AND lease_version=? AND status='RUNNING'`——旧 Worker 租约被夺走后 lease_version 不匹配 → 拒绝提交（fencing）。
busy 时指数退避重试（不是 fail）。两 Worker 并行测试验证唯一领取。

## 6. 重试/缓存/故障注入要点

- 错误分类 7 类；INVALID_INPUT/OUTPUT_INVALID→不可重试（直接 FAILED）；TRANSIENT/TIMEOUT/WORKER_INTERRUPTED/RESOURCE_UNAVAILABLE→指数退避+抖动，next_retry_at 持久化
- 缓存键 = sha256(step_type|input_hash|step_version|config_hash)；命中前必须验文件存在+校验和匹配；命中也记 attempt(status=CACHE_HIT) 走状态机
- 故障注入只经任务创建 payload 的 fault_injection 字段 + 仅当 env RENDERFLOW_ALLOW_FAULT_INJECTION=1 时生效
- 输出提交：tmp 文件 → fsync → sha256 校验 → os.replace 原子改名 → 事务内登记

## 7. 实施波次（每波：委派→我跑测试验证→git commit）

W1 后端核心：config/db/状态机/DAG/DAO/任务 CRUD API/Worker 循环/attempt 记录 + 基础测试
W2 恢复与并发：租约领取/心跳续租/过期回收/fencing/两 Worker 并测/故障注入/缓存 + 对应测试
W3 真实进程杀灭恢复测试（子进程起真 Worker → SIGKILL → 重启 → 断言续跑）+ 手动重试/取消 API + 边界
W4 前端三页面 + 轮询降频 + build 验证
W5 验收：干净环境按 README 重装重跑全部测试与 5 个演示脚本 + README 交付

## 8. 核心风险与对策

| 风险 | 对策 |
|---|---|
| SQLite 并发写锁竞争 | WAL+busy_timeout+IMMEDIATE 短事务；领取/提交均单事务 |
| 旧 Worker 提交覆盖新结果 | lease_version fencing；提交必须匹配 token |
| 测试拖慢（退避等待） | 测试用毫秒级 poll/backoff 覆盖 env |
| 前端状态与库不一致 | 进度一律由后端计算返回，前端不自行推导 |
| 「恰好一次」过度承诺 | README 明确 at-least-once + 幂等输出路径+校验和的不确定性边界 |
| AI 一次性写完整个项目（用户规则1） | 严格按 W1..W5 波次委派，每波独立验证后提交 |

## 9. 验收测试清单（对题目 §13 的 20 条映射）

T1 正常全流程 T2 任务/步骤状态一致 T3 依赖顺序 T4 查询正确
T5 首败后成 T6 不可重试直接败 T7 重试上限终止 T8 上游失败下游不执行(SKIPPED)
T9 真实子进程中断恢复(不造假) T10 过期租约重领 T11 旧 Worker fencing 拒绝 T12 重启后历史仍在
T13 缓存命中 T14 输入变缓存失效 T15 版本变缓存失效 T16 文件丢/篡改失效
T17 两 Worker 唯一领取 T18 重复重试请求幂等 T19 busy_timeout 容忍 T20 取消不误标成功
前端：tsc 无错 + vite build 通过 + dev 模式人工核对三页面。
