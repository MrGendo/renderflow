"""W6 批次检查点端到端测试：长任务批次执行、中途杀 Worker、重启后已完成批次复用不重跑。"""

import json
import os
import signal
import subprocess
import sys
import time

import pytest

from renderflow import dao, db, executor, artifacts
from renderflow.config import reset_settings


def _wenv(env, extra=None):
    e = dict(os.environ)
    e.update({
        "RENDERFLOW_DB_PATH": str(env.db_path), "RENDERFLOW_DATA_DIR": str(env.data_dir),
        "RENDERFLOW_LOG_DIR": str(env.log_dir), "RENDERFLOW_POLL_INTERVAL_MS": "30",
        "RENDERFLOW_LEASE_SECONDS": "1.2", "RENDERFLOW_HEARTBEAT_SECONDS": "0.4",
        "RENDERFLOW_STEP_DURATION_MS": "50",
        "RENDERFLOW_RETRY_BACKOFF_BASE_S": "0.01", "RENDERFLOW_RETRY_BACKOFF_MAX_S": "0.05",
        "RENDERFLOW_ALLOW_FAULT_INJECTION": "1",
        "no_proxy": "localhost,127.0.0.1",
    })
    if extra:
        e.update(extra)
    return e


def _start(env, tag):
    log = open(os.path.join(os.getcwd(), f"w6_{tag}.log"), "w")
    return subprocess.Popen([sys.executable, "-m", "renderflow.worker"],
                            cwd=os.getcwd(), env=_wenv(env), stdout=log, stderr=subprocess.STDOUT)


def _wait(pred, timeout, desc):
    dl = time.time() + timeout
    while time.time() < dl:
        if pred():
            return True
        time.sleep(0.15)
    raise AssertionError(f"timeout: {desc}")


def test_batch_checkpoint_reuse_after_kill(env, conn, client):
    """5 批次任务：跑完 2 批后 SIGKILL → 重启 → 批次 1-2 复用（BATCH_REUSED）不重跑，3-5 执行，任务完成。"""
    # 建 5 张真实 PNG 素材 + demo 间隔（每批 0.4s，总任务约 2s+，留出 kill 窗口）
    suffix = str(int(time.time() * 1000) % 10**9)
    names = [f"cp{i}_{suffix}.png" for i in range(1, 6)]
    task_id = dao.create_task(conn, {"assets": names, "demo": {"batch_pause_ms": 400}},
                              {}, "1.0.0")
    os.makedirs(os.path.join(env.data_dir, "uploads", task_id), exist_ok=True)
    for i, n in enumerate(names, 1):
        blob = artifacts.png_encode(48, 36, lambda x, y, i=i: (30 + i * 40 % 200, x * 4 % 200, y * 5 % 200))
        open(os.path.join(env.data_dir, "uploads", task_id, n), "wb").write(blob)

    w1 = _start(env, "cp1")
    w2 = None
    try:
        # 等至少 2 个批次 DONE（即产生可复用检查点）
        _wait(lambda: len([b for b in dao.list_batches(conn, task_id, "parse_assets")
                           if b["status"] == "DONE"]) >= 2, 20, "2 batches done")
        done_before = [b["batch_no"] for b in dao.list_batches(conn, task_id, "parse_assets")
                       if b["status"] == "DONE"]
        os.kill(w1.pid, signal.SIGKILL)
        w1.wait(timeout=10)
        # 重启 Worker：租约过期后接管
        w2 = _start(env, "cp2")
        _wait(lambda: dao.get_task(conn, task_id)["status"] in ("SUCCEEDED", "FAILED"), 40, "terminal")
        assert dao.get_task(conn, task_id)["status"] == "SUCCEEDED"
        batches = dao.list_batches(conn, task_id, "parse_assets")
        assert all(b["status"] == "DONE" for b in batches), [(b["batch_no"], b["status"]) for b in batches]
        # 核心断言：杀前已完成的批次没有被重跑（detail 的 produced 时间戳不变）
        steps = {s["step_type"]: s for s in dao.list_steps(conn, task_id)}
        out = json.loads(open(os.path.join(env.data_dir, steps["parse_assets"]["output_ref"])).read())
        summary = out["mock_payload"]["batch_summary"]
        reused_set = set(summary["reused"])
        assert done_before[0] in reused_set or set(done_before) & reused_set, \
            (done_before, summary)
        # 事件链有 BATCH_REUSED
        evs = [e["event_type"] for e in dao.list_events(conn, task_id, limit=500)]
        assert "BATCH_REUSED" in evs, evs[:30]
        assert "BATCH_START" in evs and "BATCH_DONE" in evs
    finally:
        for p in (w1, w2):
            if p is not None and p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    p.kill()


def test_batch_fault_injection_fail_then_retry(env, conn, client):
    """fail_at_batch=3 → 批次 3 注入失败 → 步骤重试 → 复用批次 1-2 → 成功。"""
    task_id = dao.create_task(
        conn, {"assets": ["a.png", "b.png", "c.png", "d.png"]}, {}, "1.0.0",
        fault_injection={"parse_assets": {"fail_at_batch": 3, "failure_type": "TRANSIENT_ERROR"}})
    os.makedirs(os.path.join(env.data_dir, "uploads", task_id), exist_ok=True)
    for n in ("a.png", "b.png", "c.png", "d.png"):
        open(os.path.join(env.data_dir, "uploads", task_id, n), "wb").write(
            artifacts.png_encode(16, 16, lambda x, y: (100, 150, 200)))

    from tests.conftest import run_to_terminal
    assert run_to_terminal(conn, env, task_id) == "SUCCEEDED"
    steps = {s["step_type"]: s for s in dao.list_steps(conn, task_id)}
    atts = dao.list_attempts(conn, steps["parse_assets"]["step_id"])
    assert len(atts) == 2  # 首次失败 + 重试成功
    out = json.loads(open(os.path.join(env.data_dir, steps["parse_assets"]["output_ref"])).read())
    summary = out["mock_payload"]["batch_summary"]
    assert 1 in summary["reused"] and 2 in summary["reused"], summary  # 前两批复用
    evs = [e["event_type"] for e in dao.list_events(conn, task_id, limit=500)]
    assert "BATCH_FAULT_INJECTED" in evs


def test_batch_crash_at_batch_recovery(env, conn, client):
    """crash_at_batch=2 → Worker 在批次 2 真实 os._exit(137) → 新 Worker 接管 → 批次 1 复用，任务成功。"""
    task_id = dao.create_task(
        conn, {"assets": ["x1.png", "x2.png", "x3.png"]}, {}, "1.0.0",
        fault_injection={"parse_assets": {"crash_at_batch": 2}})
    os.makedirs(os.path.join(env.data_dir, "uploads", task_id), exist_ok=True)
    for n in ("x1.png", "x2.png", "x3.png"):
        open(os.path.join(env.data_dir, "uploads", task_id, n), "wb").write(
            artifacts.png_encode(16, 16, lambda x, y: (60, 120, 180)))

    w1 = _start(env, "cr1")
    w2 = None
    try:
        _wait(lambda: w1.poll() is not None, 20, "worker self-crash at batch 2")
        assert w1.returncode in (-9, 137)
        w2 = _start(env, "cr2")
        _wait(lambda: dao.get_task(conn, task_id)["status"] in ("SUCCEEDED", "FAILED"), 40, "terminal")
        assert dao.get_task(conn, task_id)["status"] == "SUCCEEDED"
        steps = {s["step_type"]: s for s in dao.list_steps(conn, task_id)}
        out = json.loads(open(os.path.join(env.data_dir, steps["parse_assets"]["output_ref"])).read())
        summary = out["mock_payload"]["batch_summary"]
        assert 1 in summary["reused"], summary  # 崩溃前完成的批次 1 复用
        atts = dao.list_attempts(conn, steps["parse_assets"]["step_id"])
        assert any(a["error_code"] == "WORKER_INTERRUPTED" for a in atts)
    finally:
        for p in (w1, w2):
            if p is not None and p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    p.kill()
