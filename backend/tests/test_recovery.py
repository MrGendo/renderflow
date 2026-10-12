"""W2 恢复测试（进程内模拟）：T10 租约过期重领 / T11 旧 Worker fencing 拒绝。"""

import time

import pytest

from renderflow import dao, worker
from renderflow.exceptions import LeaseLostError


def _claim_first(conn, env, worker_id="ghost-worker"):
    """领取第一个步骤（模拟一个随后失联的 Worker）。"""
    claimed = dao.claim_next_step(conn, worker_id, env.lease_seconds, log_dir=env.log_dir)
    assert claimed is not None
    return claimed


# ---------------------------------------------------------------- T10


def test_t10_expired_lease_recovered(conn, env, client):
    """手动构造失联（RUNNING + 过期租约）→ Worker 一轮即回收重领并续跑。"""
    resp = client.post("/api/tasks", json={"input": {"assets": ["hero.png"]}})
    task_id = resp.json()["data"]["task_id"]

    claimed = _claim_first(conn, env)  # ghost 领取 parse_assets（attempt 1 RUNNING）
    step_id = claimed["step_id"]
    # 模拟失联：租约已过期但步骤仍 RUNNING（Worker 被 SIGKILL 后的 DB 状态）
    expired = time.time() - 1.0
    conn.execute(
        "UPDATE task_steps SET lease_until = ? WHERE step_id = ?", (expired, step_id)
    )

    worked = worker.run_once(conn, env, "recovery-worker")
    assert worked is True

    step = dao.get_step(conn, step_id)
    assert step["status"] == "SUCCEEDED"  # 回收后被新 Worker 续跑完成
    assert step["lease_version"] == claimed["lease_version"] + 1
    assert step["worker_id"] == "recovery-worker"
    attempts = dao.list_attempts(conn, step_id)
    assert [a["attempt_no"] for a in attempts] == [1, 2]
    # 前一 attempt 由回收器补记 WORKER_INTERRUPTED
    assert attempts[0]["status"] == "FAILED"
    assert attempts[0]["error_code"] == "WORKER_INTERRUPTED"
    assert attempts[1]["status"] == "SUCCEEDED"
    events = [e["event_type"] for e in dao.list_events(conn, task_id)]
    assert "LEASE_EXPIRED" in events and "STEP_RECOVERED" in events


# ---------------------------------------------------------------- T11


def test_t11_stale_worker_fenced(conn, env, client):
    """伪造旧 Worker 提交：旧 lease_version 被拒（LeaseLostError），DB 不被覆盖。"""
    resp = client.post("/api/tasks", json={"input": {"assets": ["hero.png"]}})
    task_id = resp.json()["data"]["task_id"]
    claimed = _claim_first(conn, env)
    step_id = claimed["step_id"]
    old_version = claimed["lease_version"]

    # 模拟租约被夺走：回收器/新 Worker 已把 lease_version 推进
    conn.execute(
        "UPDATE task_steps SET lease_version = ? WHERE step_id = ?", (old_version + 1, step_id)
    )

    # 旧 Worker 带着过期的 fencing token 提交成功结果 → 必须被拒
    with pytest.raises(LeaseLostError):
        dao.complete_step_success(
            conn, step_id=step_id, attempt_id=claimed["attempt_id"], worker_id="ghost-worker",
            lease_version=old_version, output_ref="tasks/x/steps/y/attempt_1/output.json",
            output_checksum="deadbeef", log_ref=None, log_dir=env.log_dir,
        )
    # 旧 Worker 提交失败结果同样被拒
    with pytest.raises(LeaseLostError):
        dao.fail_step(
            conn, step_id=step_id, attempt_id=claimed["attempt_id"], worker_id="ghost-worker",
            lease_version=old_version, error_code="TRANSIENT_ERROR",
            error_message="stale", log_ref=None, log_dir=env.log_dir,
        )

    # DB 状态未被旧结果覆盖：步骤仍在 RUNNING、无产出、attempt 未终态
    step = dao.get_step(conn, step_id)
    assert step["status"] == "RUNNING"
    assert step["output_ref"] is None and step["output_checksum"] is None
    attempt = dao.list_attempts(conn, step_id)[0]
    assert attempt["status"] == "RUNNING" and attempt["output_ref"] is None
    # 任务也不因旧提交产生任何派生变化
    assert dao.get_task(conn, task_id)["status"] == "RUNNING"
