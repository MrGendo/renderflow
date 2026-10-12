"""W2 并发测试：T17 两 Worker 唯一领取 / T18 并发手动重试幂等 / T19 写锁 busy 容忍。"""

import threading
import time

from conftest import TERMINAL, create_with_faults, run_to_terminal

from renderflow import dao, db, worker


# ---------------------------------------------------------------- T17


def test_t17_two_workers_claim_exactly_once(conn, env, client):
    """两线程各跑一个 Worker 实例（独立连接），每个步骤恰好被领取一次。"""
    resp = client.post("/api/tasks", json={"input": {"assets": ["hero.png", "bg.png"]}})
    task_id = resp.json()["data"]["task_id"]

    def _loop(worker_id: str):
        c = db.connect(env)
        try:
            while dao.get_task(c, task_id)["status"] not in TERMINAL:
                if not worker.run_once(c, env, worker_id):
                    time.sleep(0.003)
        finally:
            c.close()

    t1 = threading.Thread(target=_loop, args=("worker-A",))
    t2 = threading.Thread(target=_loop, args=("worker-B",))
    t1.start(); t2.start()
    t1.join(timeout=30); t2.join(timeout=30)
    assert not t1.is_alive() and not t2.is_alive(), "Worker 线程超时未退出"

    task = dao.get_task(conn, task_id)
    assert task["status"] == "SUCCEEDED"
    for step in dao.list_steps(conn, task_id):
        attempts = dao.list_attempts(conn, step["step_id"])
        nos = [a["attempt_no"] for a in attempts]
        # 恰好一次领取：1 条 attempt、编号唯一从 1 起（无重复、无回收重跑）
        assert len(attempts) == 1, f"{step['step_type']} 被领取 {len(attempts)} 次"
        assert nos == [1]
        assert attempts[0]["status"] == "SUCCEEDED"


# ---------------------------------------------------------------- T18


def test_t18_concurrent_manual_retry_idempotent(conn, env, client):
    """对同一 FAILED 步骤并发 10 个 retry 请求：最终状态一致，只产生一组新 attempts。"""
    task_id = create_with_faults(client, {
        "parse_assets": {"always_fail": True, "failure_type": "INVALID_INPUT", "max_attempts": 1},
    })
    assert run_to_terminal(conn, env, task_id) == "FAILED"
    step = {s["step_type"]: s for s in dao.list_steps(conn, task_id)}["parse_assets"]
    downstream = [s["step_type"] for s in dao.list_steps(conn, task_id)
                  if s["step_type"] != "parse_assets"]
    # 失败后下游被 SKIPPED；retry 应把它们放回 PENDING
    assert all(s["status"] == "SKIPPED" for s in dao.list_steps(conn, task_id)
               if s["step_type"] != "parse_assets")

    barrier = threading.Barrier(10)
    results, errors = [], []

    def _retry(i: int):
        try:
            barrier.wait()
            r = client.post(f"/api/steps/{step['step_id']}/retry")
            results.append((i, r.status_code, r.json()))
        except Exception as err:  # noqa: BLE001
            errors.append(err)

    threads = [threading.Thread(target=_retry, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    assert not errors, f"并发请求异常: {errors!r}"

    # 全部请求成功（幂等），恰好一个是首次执行，其余是幂等重复
    assert all(code == 200 for _, code, _ in results), results
    datas = [body["data"] for _, _, body in results]
    firsts = [d for d in datas if not d["idempotent_repeat"]]
    repeats = [d for d in datas if d["idempotent_repeat"]]
    assert len(firsts) == 1 and len(repeats) == 9
    assert all(d["manual_retry_count"] == 1 for d in datas)

    # DB 无中途态泄漏：步骤 READY、attempt_count 归零、任务回 RUNNING、下游回 PENDING
    fresh = dao.get_step(conn, step["step_id"])
    assert fresh["status"] == "READY" and fresh["attempt_count"] == 0
    assert fresh["manual_retry_count"] == 1
    assert dao.get_task(conn, task_id)["status"] == "RUNNING"
    statuses = {s["step_type"]: s["status"] for s in dao.list_steps(conn, task_id)}
    assert all(statuses[t] == "PENDING" for t in downstream)

    # 推进到终态后：只多出一组新 attempt（attempt_no 延续不覆盖历史）
    assert run_to_terminal(conn, env, task_id) == "FAILED"  # always_fail 仍生效
    attempts = dao.list_attempts(conn, step["step_id"])
    assert [a["attempt_no"] for a in attempts] == [1, 2]
    assert len({a["attempt_no"] for a in attempts}) == len(attempts)  # 无重复编号


# ---------------------------------------------------------------- T19


def test_t19_busy_write_lock_tolerated_by_retry(conn, env, client):
    """别的连接 BEGIN IMMEDIATE 挂住写锁 → Worker 领取靠 busy 重试渡过而非崩溃。"""
    resp = client.post("/api/tasks", json={"input": {"assets": ["hero.png"]}})
    task_id = resp.json()["data"]["task_id"]

    blocker = db.connect(env)
    blocker.execute("BEGIN IMMEDIATE")  # 模拟长写事务持有写锁

    outcome = {}

    def _run_worker():
        c = db.connect(env)
        try:
            outcome["worked"] = worker.run_once(c, env, "busy-worker")
        except Exception as err:  # noqa: BLE001
            outcome["error"] = err
        finally:
            c.close()

    t = threading.Thread(target=_run_worker)
    t.start()
    time.sleep(0.3)          # 让 Worker 先经历若干次 busy 退避
    blocker.execute("ROLLBACK")  # 释放写锁
    blocker.close()
    t.join(timeout=15)
    assert not t.is_alive(), "Worker 在锁释放后仍未完成"
    assert "error" not in outcome, outcome
    assert outcome["worked"] is True  # 渡过锁竞争并完成了本轮推进
    step = {s["step_type"]: s for s in dao.list_steps(conn, task_id)}["parse_assets"]
    assert step["status"] == "SUCCEEDED"  # 领取 + 执行 + 提交全流程无恙
