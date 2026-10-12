"""T9 真实版：真实 Worker 子进程 + 真实 SIGKILL 的中断恢复测试。

题目要求：『启动独立 Worker 子进程，实际终止进程，再验证重启恢复。
不要只通过把数据库状态手动修改为 RUNNING 来冒充进程中断测试。』
因此这里全部用 subprocess.Popen 起真实 Worker 进程（python -m renderflow.worker），
用 os.kill(pid, SIGKILL) 真实杀灭，等租约过期后重启新 Worker 验证恢复。

注意：本文件测试串行执行（真实进程测试不并行）。
"""

import os
import signal
import subprocess
import sys
import time

from renderflow import dao, db

TERMINAL = ("SUCCEEDED", "FAILED", "CANCELLED")
TICK = 0.15


def _proc_env(settings) -> dict:
    """子进程 Worker 的环境变量（指向 pytest fixture 的同一 DB/data 目录）。"""
    env = dict(os.environ)
    env.update({
        "RENDERFLOW_DB_PATH": str(settings.db_path),
        "RENDERFLOW_DATA_DIR": str(settings.data_dir),
        "RENDERFLOW_LOG_DIR": str(settings.log_dir),
        "RENDERFLOW_POLL_INTERVAL_MS": "30",
        "RENDERFLOW_LEASE_SECONDS": "1.2",
        "RENDERFLOW_HEARTBEAT_SECONDS": "0.4",
        "RENDERFLOW_STEP_DURATION_MS": "50",
        "RENDERFLOW_RETRY_BACKOFF_BASE_S": "0.01",
        "RENDERFLOW_RETRY_BACKOFF_MAX_S": "0.05",
        "RENDERFLOW_ALLOW_FAULT_INJECTION": "1",
        "no_proxy": "localhost,127.0.0.1",
    })
    return env


def start_worker(env, log_name: str, cwd) -> subprocess.Popen:
    """启动真实 Worker 子进程，stdout/stderr 落 tmp 文件。"""
    log_path = os.path.join(cwd, f"{log_name}.log")
    log = open(log_path, "w")
    proc = subprocess.Popen(
        [sys.executable, "-m", "renderflow.worker"],
        cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT,
    )
    return proc


def wait_until(pred, timeout: float, desc: str):
    """轮询直至条件成立；超时抛 AssertionError（消息带 desc）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(TICK)
    raise AssertionError(f"timeout waiting: {desc}")


def steps_by_type(conn, task_id) -> dict:
    return {s["step_type"]: s for s in dao.list_steps(conn, task_id)}


def step_status(conn, task_id, step_type) -> str | None:
    row = steps_by_type(conn, task_id).get(step_type)
    return row["status"] if row is not None else None


def test_t9_worker_killed_mid_step_recovers(env, conn, client, tmp_path):
    """SIGKILL 执行中 Worker → 租约过期 → 新 Worker 接管 → 任务完成，已成功步骤不重跑。"""
    resp = client.post("/api/tasks", json={
        "input": {"assets": [{"name": "hero.png", "size": 1024}]},
        "step_durations": {"generate_clips": 5000},
    })
    assert resp.status_code == 200, resp.text
    task_id = resp.json()["data"]["task_id"]

    wenv = _proc_env(env)
    cwd = os.getcwd()
    w1 = start_worker(wenv, "t9_w1", cwd)
    w2 = None
    try:
        # 等 generate_clips 进入 RUNNING（前置 parse+analyze 用默认 50ms，很快）
        wait_until(lambda: step_status(conn, task_id, "generate_clips") == "RUNNING",
                   timeout=15, desc="generate_clips RUNNING")
        step = steps_by_type(conn, task_id)["generate_clips"]
        assert step["worker_id"] and step["lease_until"] > time.time()

        # 真实 SIGKILL：不做任何清理，模拟进程死亡
        os.kill(w1.pid, signal.SIGKILL)
        w1.wait(timeout=10)
        assert w1.returncode in (-9, 137), w1.returncode

        # 死后步骤仍 RUNNING（真实失联状态，不是手动改库伪造）
        after = steps_by_type(conn, task_id)["generate_clips"]
        assert after["status"] == "RUNNING"
        assert after["worker_id"] == step["worker_id"]

        # 重启新 Worker：租约 1.2s 过期 → 回收 → 接管执行
        w2 = start_worker(wenv, "t9_w2", cwd)
        wait_until(lambda: dao.get_task(conn, task_id)["status"] in TERMINAL,
                   timeout=30, desc="task terminal after recovery")
        task = dao.get_task(conn, task_id)
        assert task["status"] == "SUCCEEDED", task["status"]
        steps = steps_by_type(conn, task_id)
    finally:
        for p in (w1, w2):
            if p is not None and p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    p.kill()
    # 断言放在 finally 之外也能跑，但为了失败信息清晰放这里：
    atts_by_step = {st: dao.list_attempts(conn, s["step_id"]) for st, s in steps.items()}
    # 已成功步骤不重跑：parse/analyze 的 attempt 数不变（各 1 次成功）
    for st in ("parse_assets", "analyze_scenes"):
        atts = atts_by_step[st]
        assert len(atts) == 1, (st, [(a["attempt_no"], a["status"]) for a in atts])
        assert atts[0]["status"] == "SUCCEEDED"
    # 中断步骤：旧 attempt 补记 WORKER_INTERRUPTED，新 attempt 成功
    atts = atts_by_step["generate_clips"]
    statuses = [(a["attempt_no"], a["status"], a["error_code"]) for a in atts]
    assert len(atts) == 2, statuses
    assert any(a["status"] == "FAILED" and a["error_code"] == "WORKER_INTERRUPTED" for a in atts), statuses
    assert any(a["status"] == "SUCCEEDED" for a in atts), statuses
    # 任务终态与步骤全成功
    assert all(s["status"] == "SUCCEEDED" for s in steps.values())
    # 事件流验证恢复叙事
    types = [e["event_type"] for e in dao.list_events(conn, task_id, limit=300)]
    assert "LEASE_EXPIRED" in types, types
    assert "STEP_RECOVERED" in types, types
    assert "TASK_SUCCEEDED" in types


def test_t9b_fault_crash_then_restart(env, conn, client, tmp_path):
    """crash_worker 注入自杀 + 重启：attempt 历史完整保留、最终成功。"""
    resp = client.post("/api/tasks", json={
        "input": {"assets": [{"name": "loop.png", "size": 512}]},
        "step_durations": {"analyze_scenes": 1500},
        "fault_injection": {"analyze_scenes": {"crash_worker": True, "crash_after_attempt": 1}},
    })
    assert resp.status_code == 200, resp.text
    task_id = resp.json()["data"]["task_id"]

    wenv = _proc_env(env)
    cwd = os.getcwd()
    w1 = start_worker(wenv, "t9b_w1", cwd)
    w2 = None
    try:
        # 第一次 Worker 在执行 analyze_scenes 期间 os._exit(137) 自杀
        wait_until(lambda: w1.poll() is not None, timeout=20, desc="worker1 self-crash")
        assert w1.returncode in (-9, 137), w1.returncode
        # 重启 Worker，最终任务成功
        w2 = start_worker(wenv, "t9b_w2", cwd)
        wait_until(lambda: dao.get_task(conn, task_id)["status"] in TERMINAL,
                   timeout=30, desc="task terminal")
        assert dao.get_task(conn, task_id)["status"] == "SUCCEEDED"
        steps = steps_by_type(conn, task_id)
    finally:
        for p in (w1, w2):
            if p is not None and p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    p.kill()
    atts = dao.list_attempts(conn, steps["analyze_scenes"]["step_id"])
    statuses = [(a["attempt_no"], a["status"], a["error_code"]) for a in atts]
    assert len(atts) >= 2, statuses
    assert any(a["error_code"] == "WORKER_INTERRUPTED" for a in atts), statuses
    assert any(a["status"] == "SUCCEEDED" for a in atts), statuses


def test_t9c_two_real_workers_parallel(env, conn, client, tmp_path):
    """两个真实 Worker 子进程并行：每 step 恰好 1 次成功 attempt，共 6 次 STEP_CLAIMED。"""
    resp = client.post("/api/tasks", json={
        "input": {"assets": [{"name": "par.png", "size": 256}]},
    })
    assert resp.status_code == 200, resp.text
    task_id = resp.json()["data"]["task_id"]

    wenv = _proc_env(env)
    wenv["RENDERFLOW_STEP_DURATION_MS"] = "200"  # 拉长执行时长，制造真实并行竞争窗口
    wenv["RENDERFLOW_LEASE_SECONDS"] = "5"
    wenv["RENDERFLOW_HEARTBEAT_SECONDS"] = "1"
    cwd = os.getcwd()
    w1 = start_worker(wenv, "t9c_w1", cwd)
    w2 = start_worker(wenv, "t9c_w2", cwd)
    try:
        wait_until(lambda: dao.get_task(conn, task_id)["status"] in TERMINAL,
                   timeout=30, desc="task terminal")
        assert dao.get_task(conn, task_id)["status"] == "SUCCEEDED"
        steps = steps_by_type(conn, task_id)
        for s in steps.values():
            atts = dao.list_attempts(conn, s["step_id"])
            ok = [a for a in atts if a["status"] == "SUCCEEDED"]
            assert len(ok) == 1, (s["step_type"], [(a["attempt_no"], a["status"]) for a in atts])
        evs = dao.list_events(conn, task_id, limit=500)
        claimed = [e for e in evs if e["event_type"] == "STEP_CLAIMED"]
        assert len(claimed) == 6, len(claimed)  # 6 步各被领取一次，无重复领取
    finally:
        for p in (w1, w2):
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    p.kill()


def test_t9d_data_survives_restart(env, conn, client, tmp_path):
    """中断 → 重启完成 → 重连 DB：历史与终态可查（T12 真实版）。"""
    resp = client.post("/api/tasks", json={
        "input": {"assets": [{"name": "surv.png", "size": 128}]},
        "step_durations": {"generate_audio": 4000},
    })
    assert resp.status_code == 200, resp.text
    task_id = resp.json()["data"]["task_id"]

    wenv = _proc_env(env)
    cwd = os.getcwd()
    w1 = start_worker(wenv, "t9d_w1", cwd)
    w2 = None
    try:
        wait_until(lambda: step_status(conn, task_id, "generate_audio") == "RUNNING",
                   timeout=15, desc="generate_audio RUNNING")
        os.kill(w1.pid, signal.SIGKILL)
        w1.wait(timeout=10)
        w2 = start_worker(wenv, "t9d_w2", cwd)
        wait_until(lambda: dao.get_task(conn, task_id)["status"] in TERMINAL,
                   timeout=30, desc="task terminal")
        assert dao.get_task(conn, task_id)["status"] == "SUCCEEDED"
    finally:
        for p in (w1, w2):
            if p is not None and p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    p.kill()

    # 重连数据库（模拟 API/Worker 进程重启后查询）
    conn2 = db.connect(env)
    try:
        task = dao.get_task(conn2, task_id)
        assert task["status"] == "SUCCEEDED"
        steps = {s["step_type"]: s for s in dao.list_steps(conn2, task_id)}
        assert len(steps) == 6
        assert all(s["status"] == "SUCCEEDED" for s in steps.values())
        assert steps["generate_audio"]["output_ref"]
        # API 视角（TestClient 重建 = 模拟新 API 进程读同一 DB）
        r = client.get(f"/api/tasks/{task_id}")
        assert r.status_code == 200
        assert r.json()["data"]["status"] == "SUCCEEDED"
    finally:
        conn2.close()
