"""演示脚本公共库：环境自举、子进程管理、进度叙述打印。

每个 demo 独立可重复运行：默认使用 ./data/demo 目录，运行前清空重建。
"""

import json
import os
import shutil
import subprocess
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DEMO_DIR = os.path.join(ROOT, "data", "demo")


def prepare(name: str) -> dict:
    """清空并准备 demo 环境，返回 env dict。name 用于日志文件名。"""
    shutil.rmtree(DEMO_DIR, ignore_errors=True)
    os.makedirs(DEMO_DIR, exist_ok=True)
    env = dict(os.environ)
    env.update({
        "RENDERFLOW_DB_PATH": os.path.join(DEMO_DIR, "renderflow.db"),
        "RENDERFLOW_DATA_DIR": DEMO_DIR,
        "RENDERFLOW_LOG_DIR": os.path.join(DEMO_DIR, "logs"),
        "RENDERFLOW_POLL_INTERVAL_MS": "50",
        "RENDERFLOW_LEASE_SECONDS": "2",
        "RENDERFLOW_HEARTBEAT_SECONDS": "0.5",
        "RENDERFLOW_STEP_DURATION_MS": "400",
        "RENDERFLOW_RETRY_BACKOFF_BASE_S": "0.3",
        "RENDERFLOW_RETRY_BACKOFF_MAX_S": "1.0",
        "RENDERFLOW_ALLOW_FAULT_INJECTION": "1",
        "no_proxy": "localhost,127.0.0.1",
    })
    return env


def say(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def start_worker(env, tag: str) -> subprocess.Popen:
    log = open(os.path.join(DEMO_DIR, f"worker_{tag}.log"), "w")
    proc = subprocess.Popen(
        [sys.executable, "-m", "renderflow.worker"],
        cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
    )
    say(f"Worker 子进程启动 pid={proc.pid} (日志 worker_{tag}.log)")
    return proc


def stop_worker(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
            say(f"Worker pid={proc.pid} 已优雅停止")
        except subprocess.TimeoutExpired:
            proc.kill()
            say(f"Worker pid={proc.pid} 被强杀")


def open_db(env):
    from renderflow import db
    conn = db.connect(_settings_like(env))
    return conn


def _settings_like(env):
    from renderflow.config import Settings
    return Settings(
        db_path=env["RENDERFLOW_DB_PATH"], data_dir=env["RENDERFLOW_DATA_DIR"],
        log_dir=env["RENDERFLOW_LOG_DIR"],
    )


def wait_task_terminal(conn, dao, task_id: str, timeout: float = 60.0) -> dict:
    """轮询任务到终态（用已打开的连接），打印进度叙述。"""
    TERMINAL = ("SUCCEEDED", "FAILED", "CANCELLED")
    t0 = time.time()
    while time.time() - t0 < timeout:
        task = dao.get_task(conn, task_id)
        steps = dao.list_steps(conn, task_id)
        done = sum(1 for s in steps if s["status"] == "SUCCEEDED")
        running = [s["step_type"] for s in steps if s["status"] == "RUNNING"]
        retrying = [s["step_type"] for s in steps if s["status"] == "RETRY_WAIT"]
        failed = [s["step_type"] for s in steps if s["status"] == "FAILED"]
        line = f"status={task['status']} progress={done}/{len(steps)}" \
               + (f" running={running}" if running else "") \
               + (f" retry_wait={retrying}" if retrying else "") \
               + (f" failed={failed}" if failed else "")
        say(line)
        if task["status"] in TERMINAL:
            return dict(task)
        time.sleep(0.4)
    raise TimeoutError(f"task {task_id} not terminal in {timeout}s")


def env_holder(env: dict) -> dict:
    """把 env 存到模块级变量，供 create_task 使用同一环境。"""
    global _CURRENT_ENV
    _CURRENT_ENV = env
    return env


_CURRENT_ENV: dict | None = None


def create_task(payload: dict) -> str:
    """用 DAO 层直连 DB 创建任务（不起 HTTP 服务），返回 task_id。"""
    from renderflow import dao
    env = _CURRENT_ENV or {}
    conn = open_db(env)
    try:
        return dao.create_task(
            conn,
            payload["input"],
            payload.get("step_durations") or {},
            workflow_version(),
            log_dir=env.get("RENDERFLOW_LOG_DIR"),
            fault_injection=payload.get("fault_injection") or {},
        )
    finally:
        conn.close()


def workflow_version() -> str:
    from renderflow import workflow
    return workflow.WORKFLOW_VERSION


def init_db(env) -> None:
    from renderflow import db
    conn = db.connect(_settings_like(env))
    db.init_db(conn)
    conn.close()


def banner(no: int, title: str) -> None:
    print("=" * 62, flush=True)
    print(f"DEMO {no}: {title}", flush=True)
    print("=" * 62, flush=True)


def finish(ok: bool, summary: str) -> None:
    print(("DEMO OK: " if ok else "DEMO FAILED: ") + summary, flush=True)
    sys.exit(0 if ok else 1)
