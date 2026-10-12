"""测试夹具：每测试独立的临时 DB/data 目录，毫秒级执行时长。"""

import pytest
from fastapi.testclient import TestClient

from renderflow import config, dao, db, worker
from renderflow.config import Settings, get_settings

TERMINAL = ("SUCCEEDED", "FAILED", "CANCELLED")


@pytest.fixture
def env(tmp_path, monkeypatch):
    """独立临时目录 + 毫秒级时长，并重置配置缓存让新环境变量生效。"""
    data = tmp_path / "data"
    monkeypatch.setenv("RENDERFLOW_DATA_DIR", str(data))
    monkeypatch.setenv("RENDERFLOW_DB_PATH", str(data / "renderflow.db"))
    monkeypatch.setenv("RENDERFLOW_LOG_DIR", str(data / "logs"))
    monkeypatch.setenv("RENDERFLOW_STEP_DURATION_MS", "50")
    monkeypatch.setenv("RENDERFLOW_POLL_INTERVAL_MS", "20")
    monkeypatch.setenv("RENDERFLOW_LEASE_SECONDS", "5")
    monkeypatch.setenv("RENDERFLOW_HEARTBEAT_SECONDS", "1")
    config.reset_settings()
    yield get_settings()
    config.reset_settings()


@pytest.fixture
def conn(env):
    c = db.connect(env)
    db.init_db(c)
    yield c
    c.close()


@pytest.fixture
def client(env):
    from renderflow.api import create_app

    with TestClient(create_app(env)) as c:
        yield c


def run_to_terminal(conn, settings: Settings, task_id: str, max_steps: int = 60) -> str:
    """循环调用 worker 单步推进直到任务进入终态（直接调函数，不起子进程）。"""
    for _ in range(max_steps):
        row = dao.get_task(conn, task_id)
        if row["status"] in TERMINAL:
            return row["status"]
        worker.run_once(conn, settings, "test-worker")
    return dao.get_task(conn, task_id)["status"]
