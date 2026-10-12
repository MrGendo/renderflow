# TASK1 — RenderFlow W1：后端核心（数据库/状态机/DAG/API/Worker 雮形）

你是资深 Python 后端工程师。在 `/Users/Admin/Downloads/project/written_test` 下开发。先读 `docs/PLAN.md`（整体设计已定，严格遵循其中第 2-6 节的设计决策）。本任务只做 W1 范围，勿超前实现 W2/W3 内容（故障注入、租约过期回收、fencing、重试/取消 API 都不做）。

## 环境
- Python：用 `uv`（在 PATH）。在 backend/ 下 `uv venv --python 3.11`（系统有 /opt/homebrew/bin/python3.11），依赖安装用 `uv pip install`。
- macOS，无 Docker。git 仓库已在项目根 init（main 分支）。**你不要执行任何 git commit**，由我验收后提交。
- 本机有 HTTP 代理，测试中访问 localhost 一律不需要代理（如遇代理干扰，设 no_proxy）。

## 交付物（仅这些文件，勿创建无关文件）

```
backend/
  pyproject.toml           # 项目名 renderflow；deps: fastapi, uvicorn, pydantic, pydantic-settings
  renderflow/
    __init__.py
    config.py              # pydantic-settings，全部配置带默认值
    enums.py               # TaskStatus/StepStatus/ErrorCode 枚举
    db.py                  # 连接管理、schema DDL、schema_migrations、初始化入口
    state_machine.py       # 合法迁移表 + 守卫函数 + summarize_task 统一汇总
    workflow.py            # 6 步 DAG 定义 + 步骤规格（版本/超时/重试策略/输入摘要函数）
    dao.py                 # 全部 SQL（含原子领取）
    storage.py             # 输出文件原子提交（tmp→fsync→sha256→os.replace）、路径生成
    events.py              # task_events 表写入 + JSON 行式文件日志
    executor.py            # MOCK 步骤执行器（可配置时长，W1 只需成功路径）
    api.py                 # FastAPI 应用（uvicorn renderflow.api:app）
    schemas.py             # Pydantic 请求/响应模型 + 统一响应包装
    worker.py              # Worker 主循环，`python -m renderflow.worker` 启动
  tests/
    conftest.py            # 临时 DB/data 目录 fixture
    test_basic_flow.py     # T1-T4
.gitignore                 # 项目根：.venv/__pycache__/*.db/data/node_modules/dist/.env
.env.example               # 项目根
Makefile                   # 项目根：install/dev-api/dev-worker/test 目标
```

## 关键设计契约（必须逐条落实）

### 1. 枚举与字符串
- TaskStatus: PENDING RUNNING RETRY_WAIT FAILED SUCCEEDED CANCELLED
- StepStatus: PENDING READY RUNNING RETRY_WAIT FAILED SUCCEEDED SKIPPED
- ErrorCode(W1 先定义全 7 类供 schema 用): TRANSIENT_ERROR TIMEOUT WORKER_INTERRUPTED INVALID_INPUT RESOURCE_UNAVAILABLE OUTPUT_INVALID INTERNAL_ERROR
- 所有时间戳 DB 存 UTC unix epoch REAL；API 层转 ISO8601（带 Z）输出，另附原始数值。
- ID：task_id/step_id/attempt_id = uuid4 hex；worker_id = f"{hostname}:{pid}:{uuid4hex[:8]}"。

### 2. 数据库（SQLite，标准库 sqlite3，不用 SQLAlchemy）
四张核心表 + task_events，字段严格按 docs/PLAN.md 第 2 节：
- tasks: task_id TEXT PK, status TEXT NOT NULL, input_json TEXT NOT NULL, created_at REAL, updated_at REAL, started_at REAL, finished_at REAL, cancel_requested INTEGER DEFAULT 0, error_summary TEXT, workflow_version TEXT NOT NULL
- task_steps: step_id TEXT PK, task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE, step_type TEXT NOT NULL, status TEXT NOT NULL, dependencies TEXT NOT NULL(JSON数组), input_hash TEXT NOT NULL, step_version TEXT NOT NULL, config_hash TEXT NOT NULL, output_ref TEXT, output_checksum TEXT, attempt_count INTEGER DEFAULT 0, max_attempts INTEGER NOT NULL, next_retry_at REAL, worker_id TEXT, heartbeat_at REAL, lease_until REAL, lease_version INTEGER DEFAULT 0, created_at REAL, started_at REAL, finished_at REAL, error_code TEXT, error_message TEXT; UNIQUE(task_id, step_type)
- step_attempts: attempt_id TEXT PK, step_id TEXT NOT NULL REFERENCES task_steps(step_id) ON DELETE CASCADE, attempt_no INTEGER NOT NULL, worker_id TEXT, status TEXT NOT NULL, started_at REAL, finished_at REAL, duration_ms INTEGER, error_code TEXT, error_message TEXT, log_ref TEXT, output_ref TEXT; UNIQUE(step_id, attempt_no)
- step_cache: cache_key TEXT PK, step_type TEXT NOT NULL, step_version TEXT NOT NULL, config_hash TEXT NOT NULL, input_hash TEXT NOT NULL, output_ref TEXT NOT NULL, output_checksum TEXT NOT NULL, created_at REAL, expires_at REAL
- task_events: event_id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, step_id TEXT, attempt_id TEXT, worker_id TEXT, event_type TEXT NOT NULL, status TEXT, duration_ms INTEGER, error_code TEXT, detail_json TEXT, created_at REAL NOT NULL
- 索引：tasks(status,created_at); task_steps(task_id); task_steps(status,lease_until); step_attempts(step_id); step_cache(expires_at); task_events(task_id,created_at)
- PRAGMA: journal_mode=WAL, synchronous=NORMAL, busy_timeout=5000, foreign_keys=ON
- db.py 内置 SCHEMA_MIGRATIONS 列表（W1 就一条建表迁移）；应用与 Worker 启动时幂等执行；提供 `python -m renderflow.db` 手动初始化命令。写操作统一走一个 helper：BEGIN IMMEDIATE + 失败(sqlite3.OperationalError 含 locked/busy)时指数退避重试（最多 5 次）。

### 3. 状态机（state_machine.py，API 与 Worker 共用，禁止两处复制逻辑）
- ALLOWED_TASK_TRANSITIONS / ALLOWED_STEP_TRANSITIONS 显式字典；非法迁移抛 StateTransitionError。
- 终态(SUCCEEDED/FAILED/CANCELLED)不可迁出：dao 里所有终态更新 SQL 必须带 `WHERE status NOT IN ('SUCCEEDED','FAILED','CANCELLED')` 守卫。
- `summarize_task(task_row, step_rows) -> TaskSummary`，返回 (derived_status, reason_code, progress_percent, is_terminal)。reason_code 枚举: PENDING/RUNNING/WAITING_RETRY/WAITING_DEPENDENCIES/FAILED/SUCCEEDED/CANCELLED。规则：
  1) cancel_requested 且无 RUNNING 步骤 → CANCELLED
  2) 任一步骤 FAILED → 任务 FAILED（error_summary 取该步骤 error_code+message）——W1 正常路径不会触发，但逻辑要写好并有单测
  3) 任一步骤 RETRY_WAIT → 任务 RETRY_WAIT
  4) 全部 SUCCEEDED → SUCCEEDED
  5) 有 RUNNING → RUNNING；否则有 READY/PENDING → PENDING(未开始) 或 RUNNING(已 started_at)
  6) progress = round(100 * succeeded_count / total_steps)（等权，全部步骤计入分母，SKIPPED 不算成功）

### 4. DAG（workflow.py）
固定六步：parse_assets → analyze_scenes → generate_clips/generate_audio（都依赖 analyze_scenes）→ compose_video（依赖 generate_clips+generate_audio）→ export_result（依赖 compose_video）。
每步规格：step_type, step_version(如 "1.0.0"), dependencies, timeout_s(默认 30), max_attempts(默认 3), retry_backoff_base_s(默认 1.0), retry_backoff_max_s(默认 30), config(dict 影响输出的参数), build_input(task_input, upstream_outputs)->dict。
input_hash = sha256(canonical_json(step_type + normalized_input))；config_hash = sha256(canonical_json(step_version + config))。
canonical_json：json.dumps(sort_keys=True, separators=(',',':'), ensure_ascii=False)。
依赖产出传递：build_input 读取上游步骤的 output_ref 文件内容（storage.load_output）。
启动时校验 DAG 无环且依赖都存在，否则抛 WorkflowDefinitionError。

### 5. 输出存储（storage.py）
- 路径：`{data_dir}/tasks/{task_id}/steps/{step_type}/attempt_{attempt_no}/output.json`（版本化不可变，不同尝试不覆盖）
- 提交流程：写 `{...}/output.json.tmp` → flush+fsync → 计算 sha256 → os.replace 原子改名 → 返回 (output_ref=相对路径, checksum)。DB 登记失败时调用方负责清理 tmp（W1 简单处理即可，W2 完善）。
- load_output(output_ref) 读文件并校验 sha256（不符抛 StorageError(OUTPUT_INVALID)）。
- 每次尝试的执行日志：同目录 `run.log`（JSON 行式，executor 逐事件追加），log_ref 存相对路径。

### 6. MOCK 执行器（executor.py）
- 每步默认 duration_ms 来自 config（env RENDERFLOW_STEP_DURATION_MS 默认 800，任务创建 payload 可按步骤覆盖：`step_durations: {generate_clips: 1200}`）。
- 执行 = 分片 sleep（每 100ms 一片，便于 W2 加心跳/取消检查），产出 output.json：{"step_type","task_id","attempt_no","produced_at","inputs_digest","mock_payload":{...按步骤类型不同的确定性伪内容...}}。伪内容由 input_hash 做种子生成，相同输入产出相同内容（缓存测试的前提）。
- W1 只实现成功路径；错误分类/故障注入留接口（execute(step_ctx) -> StepResult(success, error_code, error_message)），W2 填充。

### 7. Worker（worker.py）
主循环（while not stop_flag）：
1. 领取：单事务 `BEGIN IMMEDIATE` → SELECT 满足条件的 step（status='READY'，或 W1 只需 READY）→ UPDATE 该 step：status='RUNNING', worker_id, lease_until=now+lease_s, lease_version+=1, heartbeat_at=now, started_at=COALESCE(started_at,now), attempt_count+=1 → 同事务 INSERT step_attempts(attempt_no=attempt_count, status='RUNNING', ...) → COMMIT。领取前把依赖全 SUCCEEDED 的 PENDING 步骤推进为 READY（同一事务或紧邻事务）。依赖检查：严格验证所有 dependencies 的状态是 SUCCEEDED。
2. 事件：STEP_CLAIMED/STEP_STARTED 写 task_events。
3. 执行 executor（executor 内部每 100ms 回调更新 heartbeat_at/lease_until——W1 直接在 dao 提供 touch_lease(step_id, worker_id, lease_version)）。
4. 成功：storage 提交输出 → 事务内（守卫 WHERE lease_version=? AND status='RUNNING'）UPDATE step SUCCEEDED/output_ref/output_checksum/finished_at + UPDATE attempt SUCCEEDED/duration_ms/output_ref → task_events STEP_SUCCEEDED → 重新 summarize_task 并更新 tasks.status（带终态守卫）。
5. 循环间隔 env RENDERFLOW_POLL_INTERVAL_MS 默认 300ms；无活可干时 sleep。
- SIGTERM/SIGINT → 优雅停止：完成当前步骦后退出（设 stop_flag，executor 分片 sleep 检查）。
- W1 无失败路径：executor 异常统一 INTERNAL_ERROR 记 attempt FAILED、step FAILED（复用状态机汇总），不算超前。

### 8. API（api.py + schemas.py）
- 统一包装：成功 {"ok":true,"data":...}；错误 {"ok":false,"error":{"code","message"}}，HTTP 404(不存在)/409(状态冲突)/400(参数)。FastAPI RequestValidationError 也转成该结构(400, VALIDATION_ERROR)。
- POST /api/tasks：body {"input": {...必填 assets 列表等业务字段...}, "step_durations"?: {...}, "workflow_version"?}。校验 input.assets 非空数组。创建 tasks 行(PENDING) + 6 条 task_steps 行(PENDING，含 input_hash/config_hash/max_attempts 等) + TASK_CREATED 事件，立即返回 task_id 与步骤概要（不等待执行）。
- GET /api/tasks?page&pageSize&status：分页 + 状态筛选，返回 total/items，每项含 progress、succeeded_steps/total_steps、duration_ms（created→finished 或 now）。
- GET /api/tasks/{task_id}：全字段 + summarize 的 reason_code + 各步骤概要数组。
- GET /api/tasks/{task_id}/steps：步骤数组（全字段）。
- GET /api/steps/{step_id}/attempts：尝试数组（含 log_ref、output_ref）。
- GET /api/tasks/{task_id}/events：事件数组（升序，可 limit）。
- GET /api/health → {"ok":true,"data":{"status":"up"}}；GET /api/ready → 检查 DB 可连接且 schema 就绪 → 200/503。
- 进度与 reason_code 一律来自 summarize_task / DB，前端不自行推导。

### 9. 配置（config.py + .env.example）
RENDERFLOW_DB_PATH(默认 ./data/renderflow.db)、RENDERFLOW_DATA_DIR(默认 ./data)、RENDERFLOW_POLL_INTERVAL_MS、RENDERFLOW_STEP_DURATION_MS、RENDERFLOW_LEASE_SECONDS(默认 15)、RENDERFLOW_HEARTBEAT_SECONDS(默认 5)、RENDERFLOW_LOG_DIR(默认 ./data/logs)。.env.example 列全部带注释；真实 .env 不提交。

### 10. 测试（tests/，pytest + fastapi TestClient + 直接调 dao/worker 函数）
- conftest：每测试独立 tmp 目录 DB/data，env 注入毫秒级时长。
- test_basic_flow.py：
  T1 正常全流程：创建任务 → 循环调用 worker 单步推进（或起子进程 worker）直到终态 → 断言任务 SUCCEEDED、6 步全 SUCCEEDED、每步 output 文件存在且 sha256 匹配 DB、attempt 各 1 条 SUCCEEDED、事件含 TASK_CREATED/STEP_CLAIMED/STEP_STARTED/STEP_SUCCEEDED/TASK_SUCCEEDED。
  T2 状态一致：中途多个时点采样，断言 API 返回 status 与 DB tasks.status 一致、progress 单调不减。
  T3 依赖顺序：记录各步骤 started_at，断言 analyze_scenes 晚于 parse_assets 完成、compose_video 晚于 generate_clips 与 generate_audio 完成、export_result 最后；且 compose_video 开始时其依赖都已 SUCCEEDED。
  T4 查询：任务列表分页/筛选、详情、步骤列表、attempts 列表、404 情形（不存在 task/step）、events 接口。
- 运行方式：`cd backend && uv run pytest -q`。所有测试必须真实通过；不许删断言。

## 完成标准
1. `uv run pytest -q` 全绿，如实报告通过数。
2. `uv run python -m renderflow.db` 可初始化 DB；`uv run uvicorn renderflow.api:app --port 8900` 可启动，/api/health /api/ready 200。
3. 手动冒烟：起 API+Worker，curl POST /api/tasks 创建任务，轮询到 SUCCEEDED。
4. 汇报：创建的文件清单、测试输出原文、冒烟证据、任何偏离本任务书的决定及理由。

## 禁止
- 不做 W2/W3 功能（故障注入/租约过期回收/fencing 拒绝提交/重试退避/取消/缓存）。step_cache 表 W1 建表即可，读写逻辑 W2 做。
- 不创建 frontend/。
- 不 git commit。
- 不引入 Redis/Celery/消息队列/SQLAlchemy。
