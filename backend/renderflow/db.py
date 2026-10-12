"""SQLite 连接管理、schema DDL、schema_migrations 与统一写事务 helper。

标准库 sqlite3，不用 SQLAlchemy。所有写操作统一走 run_tx（BEGIN IMMEDIATE +
locked/busy 指数退避重试），避免多处各写一套事务代码。
"""

import os
import sqlite3
import threading
import time

from .config import Settings, get_settings

# 同一连接的写事务必须进程内串行：FastAPI 线程池并发请求共用一条连接，
# 无锁会触发 "cannot start a transaction within a transaction"。
# 跨进程（API 进程 + Worker 进程）仍由 SQLite 文件锁兜底。
_TX_LOCK = threading.RLock()

# W1 唯一一条迁移：五张表 + 索引。字段严格对齐 docs/PLAN.md 第 2 节。
_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    input_json TEXT NOT NULL,
    created_at REAL,
    updated_at REAL,
    started_at REAL,
    finished_at REAL,
    cancel_requested INTEGER DEFAULT 0,
    error_summary TEXT,
    workflow_version TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_steps (
    step_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    step_type TEXT NOT NULL,
    status TEXT NOT NULL,
    dependencies TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    step_version TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    output_ref TEXT,
    output_checksum TEXT,
    attempt_count INTEGER DEFAULT 0,
    max_attempts INTEGER NOT NULL,
    next_retry_at REAL,
    worker_id TEXT,
    heartbeat_at REAL,
    lease_until REAL,
    lease_version INTEGER DEFAULT 0,
    created_at REAL,
    started_at REAL,
    finished_at REAL,
    error_code TEXT,
    error_message TEXT,
    UNIQUE(task_id, step_type)
);

CREATE TABLE IF NOT EXISTS step_attempts (
    attempt_id TEXT PRIMARY KEY,
    step_id TEXT NOT NULL REFERENCES task_steps(step_id) ON DELETE CASCADE,
    attempt_no INTEGER NOT NULL,
    worker_id TEXT,
    status TEXT NOT NULL,
    started_at REAL,
    finished_at REAL,
    duration_ms INTEGER,
    error_code TEXT,
    error_message TEXT,
    log_ref TEXT,
    output_ref TEXT,
    UNIQUE(step_id, attempt_no)
);

CREATE TABLE IF NOT EXISTS step_cache (
    cache_key TEXT PRIMARY KEY,
    step_type TEXT NOT NULL,
    step_version TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    output_ref TEXT NOT NULL,
    output_checksum TEXT NOT NULL,
    created_at REAL,
    expires_at REAL
);

CREATE TABLE IF NOT EXISTS task_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    step_id TEXT,
    attempt_id TEXT,
    worker_id TEXT,
    event_type TEXT NOT NULL,
    status TEXT,
    duration_ms INTEGER,
    error_code TEXT,
    detail_json TEXT,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tasks_status_created ON tasks(status, created_at);
CREATE INDEX IF NOT EXISTS idx_task_steps_task ON task_steps(task_id);
CREATE INDEX IF NOT EXISTS idx_task_steps_lease ON task_steps(status, lease_until);
CREATE INDEX IF NOT EXISTS idx_step_attempts_step ON step_attempts(step_id);
CREATE INDEX IF NOT EXISTS idx_step_cache_expires ON step_cache(expires_at);
CREATE INDEX IF NOT EXISTS idx_task_events_task ON task_events(task_id, created_at);
"""

# (版本号, 迁移说明, DDL)；新增迁移只追加，不改历史条目
SCHEMA_MIGRATIONS = [
    (1, "W1 初始建表：tasks/task_steps/step_attempts/step_cache/task_events + 索引", _SCHEMA_V1),
    # W2：手动重试幂等标记（attempt_count 归零语义下区分"已重试"与"从未失败"）
    (2, "W2 task_steps 增加 manual_retry_count", "ALTER TABLE task_steps ADD COLUMN manual_retry_count INTEGER NOT NULL DEFAULT 0;"),
]


def connect(settings: Settings | None = None) -> sqlite3.Connection:
    """打开连接并应用全部 PRAGMA。isolation_level=None 表示手动控制事务。"""
    st = settings or get_settings()
    parent = os.path.dirname(st.db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    # check_same_thread=False：TestClient 会在别的线程里跑请求，连接需要跨线程可用
    conn = sqlite3.connect(st.db_path, isolation_level=None, timeout=5.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """幂等执行未应用的迁移。应用与 Worker 启动时各调一次。"""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at REAL NOT NULL)"
    )
    applied = {row["version"] for row in conn.execute("SELECT version FROM schema_migrations")}
    for version, name, ddl in SCHEMA_MIGRATIONS:
        if version in applied:
            continue
        script = f"BEGIN IMMEDIATE;\n{ddl}\nINSERT INTO schema_migrations VALUES ({version}, '{name}', {time.time()});\nCOMMIT;"
        conn.executescript(script)


def run_tx(conn: sqlite3.Connection, fn, retries: int = 5):
    """统一写事务：BEGIN IMMEDIATE + fn(cursor) + COMMIT。

    遇 locked/busy 时指数退避重试（最多 retries 次），其余异常回滚后上抛。
    fn 内部不得再开事务。
    """
    delay = 0.05
    last_err: sqlite3.OperationalError | None = None
    with _TX_LOCK:
        for attempt in range(retries + 1):
            try:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    result = fn(conn)
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
                conn.execute("COMMIT")
                return result
            except sqlite3.OperationalError as err:
                # 仅对锁冲突退避重试；BEGIN 失败时事务未开，直接重来
                msg = str(err).lower()
                if ("locked" in msg or "busy" in msg) and attempt < retries:
                    last_err = err
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise
    raise last_err  # pragma: no cover 理论不可达，仅类型兜底


def main() -> None:
    """`python -m renderflow.db` 手动初始化数据库。"""
    st = get_settings()
    conn = connect(st)
    try:
        init_db(conn)
        applied = [row["version"] for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]
        print(f"数据库已初始化: {st.db_path}（已应用迁移: {applied}）")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
