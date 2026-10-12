"""W2 重试引擎测试：T5 首败后成 / T6 不可重试直接败 / T7 重试上限 / T8 上游失败下游不执行。"""

from conftest import create_with_faults, run_to_terminal

from renderflow import dao


def _steps_by_type(conn, task_id):
    return {s["step_type"]: s for s in dao.list_steps(conn, task_id)}


def _event_types(conn, task_id):
    return [e["event_type"] for e in dao.list_events(conn, task_id)]


# ---------------------------------------------------------------- T5


def test_t5_transient_fail_then_succeed(conn, env, client):
    """首败（TRANSIENT）→ RETRY_WAIT → 到期重领成功；attempts 历史全保留。"""
    task_id = create_with_faults(client, {
        "parse_assets": {"fail_first_n_attempts": 1, "failure_type": "TRANSIENT_ERROR"},
    })

    assert run_to_terminal(conn, env, task_id) == "SUCCEEDED"

    step = _steps_by_type(conn, task_id)["parse_assets"]
    assert step["status"] == "SUCCEEDED"
    attempts = dao.list_attempts(conn, step["step_id"])
    # 两条历史：第 1 次 FAILED(TRANSIENT_ERROR)，第 2 次 SUCCEEDED
    assert [a["attempt_no"] for a in attempts] == [1, 2]
    assert attempts[0]["status"] == "FAILED" and attempts[0]["error_code"] == "TRANSIENT_ERROR"
    assert attempts[1]["status"] == "SUCCEEDED" and attempts[1]["error_code"] is None
    # 事件链：调度重试 → 到期回 READY → 最终成功
    events = _event_types(conn, task_id)
    assert "STEP_RETRY_SCHEDULED" in events
    assert "STEP_READY" in events
    assert "TASK_RETRY_WAIT" in events
    assert "TASK_SUCCEEDED" in events


# ---------------------------------------------------------------- T6


def test_t6_invalid_input_fails_without_retry(conn, env, client):
    """INVALID_INPUT 不可重试：单次 attempt 直接 FAILED，下游递归 SKIPPED。"""
    task_id = create_with_faults(client, {
        "parse_assets": {"fail_first_n_attempts": 1, "failure_type": "INVALID_INPUT"},
    })

    assert run_to_terminal(conn, env, task_id) == "FAILED"

    steps = _steps_by_type(conn, task_id)
    step = steps["parse_assets"]
    assert step["status"] == "FAILED"
    assert step["error_code"] == "INVALID_INPUT"
    # 只有 1 次 attempt——没有进入重试通道
    attempts = dao.list_attempts(conn, step["step_id"])
    assert len(attempts) == 1 and attempts[0]["status"] == "FAILED"
    events = _event_types(conn, task_id)
    assert "STEP_RETRY_SCHEDULED" not in events
    # 下游全部 SKIPPED 且原因正确，任务带 finished_at
    for stype in ("analyze_scenes", "generate_clips", "generate_audio", "compose_video", "export_result"):
        assert steps[stype]["status"] == "SKIPPED", stype
        assert steps[stype]["error_code"] == "BLOCKED_BY_UPSTREAM", stype
    task = dao.get_task(conn, task_id)
    assert task["finished_at"] is not None
    assert "STEP_SKIPPED" in events and "TASK_FAILED" in events


# ---------------------------------------------------------------- T7


def test_t7_always_fail_hits_max_attempts(conn, env, client):
    """always_fail + max_attempts=2：恰好 2 次后 FAILED，下游 SKIPPED，任务 FAILED。"""
    task_id = create_with_faults(client, {
        "parse_assets": {"always_fail": True, "failure_type": "TRANSIENT_ERROR", "max_attempts": 2},
    })

    assert run_to_terminal(conn, env, task_id) == "FAILED"

    step = _steps_by_type(conn, task_id)["parse_assets"]
    assert step["status"] == "FAILED"
    assert step["attempt_count"] == 2  # 领取时递增，恰好达到上限
    attempts = dao.list_attempts(conn, step["step_id"])
    assert len(attempts) == 2
    assert all(a["status"] == "FAILED" for a in attempts)
    # 第 1 次失败调度了重试，第 2 次到达上限转 FAILED
    assert _event_types(conn, task_id).count("STEP_RETRY_SCHEDULED") == 1
    task = dao.get_task(conn, task_id)
    assert task["status"] == "FAILED" and task["finished_at"] is not None


# ---------------------------------------------------------------- T8


def test_t8_downstream_never_ready_while_upstream_failing(conn, env, client):
    """上游反复失败期间，下游从未进入 READY/RUNNING（逐轮轮询断言）。"""
    import time
    task_id = create_with_faults(client, {
        "parse_assets": {"always_fail": True, "failure_type": "TRANSIENT_ERROR"},
    })
    downstream_types = [s["step_type"] for s in dao.list_steps(conn, task_id)
                        if s["step_type"] != "parse_assets"]

    for _ in range(120):
        if dao.get_task(conn, task_id)["status"] in ("SUCCEEDED", "FAILED", "CANCELLED"):
            break
        from renderflow import worker
        if not worker.run_once(conn, env, "test-worker"):
            time.sleep(0.005)  # 等毫秒级退避到期
        for stype, srow in _steps_by_type(conn, task_id).items():
            if stype in downstream_types:
                assert srow["status"] not in ("READY", "RUNNING"), \
                    f"下游 {stype} 意外进入 {srow['status']}"

    steps = _steps_by_type(conn, task_id)
    assert dao.get_task(conn, task_id)["status"] == "FAILED"
    for stype in downstream_types:
        assert steps[stype]["status"] == "SKIPPED"  # 终局：被上游失败阻塞
