"""W2 取消测试：T20a 运行中取消 / T20b 终态任务拒绝取消 / T20c 取消后绝不误标成功。"""

import time

from conftest import run_to_terminal

from renderflow import dao, worker


def _create(client, assets=("hero.png",)):
    resp = client.post("/api/tasks", json={"input": {"assets": list(assets)}})
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]["task_id"]


def _cancel(client, task_id):
    return client.post(f"/api/tasks/{task_id}/cancel")


# ---------------------------------------------------------------- T20a


def test_t20a_cancel_running_task(conn, env, client):
    """跑完两步后取消：未开始步骤 SKIPPED(CANCELLED_BY_USER)，任务 CANCELLED。"""
    task_id = _create(client)
    worker.run_once(conn, env, "w")  # parse_assets
    worker.run_once(conn, env, "w")  # analyze_scenes

    resp = _cancel(client, task_id)
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["cancel_requested"] is True

    detail = client.get(f"/api/tasks/{task_id}").json()["data"]
    assert detail["status"] == "CANCELLED"
    assert detail["cancel_requested"] is True
    assert detail["finished_at"] is not None

    steps = {s["step_type"]: s for s in dao.list_steps(conn, task_id)}
    assert steps["parse_assets"]["status"] == "SUCCEEDED"  # 已完成的不动
    assert steps["analyze_scenes"]["status"] == "SUCCEEDED"
    for stype in ("generate_clips", "generate_audio", "compose_video", "export_result"):
        assert steps[stype]["status"] == "SKIPPED", stype
        assert steps[stype]["error_code"] == "CANCELLED_BY_USER", stype
        # 未开始步骤没有任何执行痕迹
        assert dao.list_attempts(conn, steps[stype]["step_id"]) == []
    events = [e["event_type"] for e in dao.list_events(conn, task_id)]
    assert "TASK_CANCEL_REQUESTED" in events and "TASK_CANCELLED" in events


# ---------------------------------------------------------------- T20b


def test_t20b_cancel_terminal_task_rejected(conn, env, client):
    task_id = _create(client)
    assert run_to_terminal(conn, env, task_id) == "SUCCEEDED"

    resp = _cancel(client, task_id)
    assert resp.status_code == 409
    body = resp.json()
    assert body["ok"] is False and body["error"]["code"] == "TASK_ALREADY_TERMINAL"
    # 原状态不受影响
    assert dao.get_task(conn, task_id)["status"] == "SUCCEEDED"


# ---------------------------------------------------------------- T20c


def test_t20c_cancelled_task_never_marked_succeeded(conn, env, client):
    """取消后继续轮询 Worker 多轮：任务保持 CANCELLED，绝不变成 SUCCEEDED。"""
    task_id = _create(client)
    worker.run_once(conn, env, "w")  # 仅 parse_assets 完成
    assert _cancel(client, task_id).status_code == 200

    for _ in range(20):
        worker.run_once(conn, env, "poll-worker")
        time.sleep(0.003)
        task = dao.get_task(conn, task_id)
        assert task["status"] == "CANCELLED", f"取消后状态异常: {task['status']}"
        assert task["finished_at"] is not None

    # 剩余步骤无新 attempts（取消后 Worker 不再领取）
    for step in dao.list_steps(conn, task_id):
        if step["step_type"] != "parse_assets":
            assert dao.list_attempts(conn, step["step_id"]) == []


# --------------------------------------------------- 补充：RUNNING 检查点取消


def test_cancel_running_step_checkpoint(conn, env, client, monkeypatch):
    """RUNNING 步骤不被硬打断：executor 分片检查点发现取消后自然中止。"""
    import threading
    monkeypatch.setenv("RENDERFLOW_STEP_DURATION_MS", "600")  # 拉长执行制造 RUNNING 窗口
    from renderflow import config
    config.reset_settings()
    settings = config.get_settings()

    task_id = _create(client)
    outcome = {}
    thread = threading.Thread(target=lambda: outcome.update(
        worked=worker.run_once(_worker_conn(settings), settings, "long-worker")))
    thread.start()
    # 等 parse_assets 真正进入 RUNNING（executor 已开始分片 sleep）
    deadline = time.time() + 5
    step_row = None
    while time.time() < deadline:
        for s in dao.list_steps(conn, task_id):
            if s["step_type"] == "parse_assets" and s["status"] == "RUNNING":
                step_row = s
                break
        if step_row:
            break
        time.sleep(0.01)
    assert step_row is not None, "步骤未进入 RUNNING"

    assert _cancel(client, task_id).status_code == 200
    thread.join(timeout=10)
    assert not thread.is_alive()

    step = dao.get_step(conn, step_row["step_id"])
    assert step["status"] == "SKIPPED"  # 检查点自然中止，标 SKIPPED
    assert step["error_code"] == "CANCELLED_BY_USER"
    assert dao.get_task(conn, task_id)["status"] == "CANCELLED"


def _worker_conn(settings):
    """执行线程用独立连接（与测试主连接并发使用会互相干扰事务状态）。"""
    from renderflow import db
    c = db.connect(settings)
    db.init_db(c)
    return c
