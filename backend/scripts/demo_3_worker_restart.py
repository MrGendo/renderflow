"""DEMO 3：Worker 进程中断恢复——真实 SIGKILL + 租约过期 + 新 Worker 接管。"""

import os
import signal
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
import demo_common as dc


def main() -> None:
    dc.banner(3, "重启 Worker 后恢复（执行中 SIGKILL → 租约过期 → 新 Worker 接管）")
    env = dc.env_holder(dc.prepare("demo3"))
    dc.init_db(env)

    task_id = dc.create_task({
        "input": {"assets": [{"name": "crash.png", "size": 768}]},
        "step_durations": {"generate_clips": 6000},  # 制造执行窗口
    })
    dc.say(f"任务已创建（generate_clips 执行 6s）: {task_id}")

    from renderflow import dao
    w1 = dc.start_worker(env, "demo3_w1")
    conn = dc.open_db(env)
    w2 = None
    try:
        # 等 generate_clips 进入 RUNNING
        deadline = time.time() + 20
        while time.time() < deadline:
            steps = {s["step_type"]: s for s in dao.list_steps(conn, task_id)}
            if steps["generate_clips"]["status"] == "RUNNING":
                break
            time.sleep(0.2)
        else:
            dc.finish(False, "generate_clips 未进入 RUNNING")
        dc.say("generate_clips 正在执行（持有租约）→ 现在真实 SIGKILL Worker 进程")
        os.kill(w1.pid, signal.SIGKILL)
        w1.wait(timeout=10)
        dc.say(f"Worker pid={w1.pid} 已被杀死（exit={w1.returncode}），步骤停留在 RUNNING，等待租约过期（lease=2s）")

        time.sleep(2.5)  # 等租约真正过期
        w2 = dc.start_worker(env, "demo3_w2")
        task = dc.wait_task_terminal(conn, dao, task_id, timeout=90)
        steps = {s["step_type"]: s for s in dao.list_steps(conn, task_id)}
        atts = dao.list_attempts(conn, steps["generate_clips"]["step_id"])
        dc.say("generate_clips 尝试历史:")
        for a in atts:
            dc.say(f"  attempt_no={a['attempt_no']} worker={a['worker_id'][:20]}… status={a['status']}"
                   + (f" error={a['error_code']}" if a["error_code"] else ""))
        evs = [e["event_type"] for e in dao.list_events(conn, task_id, limit=300)]
        # 已成功步骤未被重复执行
        parse_atts = dao.list_attempts(conn, steps["parse_assets"]["step_id"])
        ok = (task["status"] == "SUCCEEDED"
              and any(a["error_code"] == "WORKER_INTERRUPTED" for a in atts)
              and "LEASE_EXPIRED" in evs and "STEP_RECOVERED" in evs
              and len(parse_atts) == 1)
        dc.finish(ok, f"task={task_id} status=SUCCEEDED, 中断 attempt 已补记 WORKER_INTERRUPTED, "
                      f"LEASE_EXPIRED+STEP_RECOVERED 事件在案, 已成功步骤未重跑")
    finally:
        conn.close()
        dc.stop_worker(w1)
        if w2:
            dc.stop_worker(w2)


if __name__ == "__main__":
    main()
