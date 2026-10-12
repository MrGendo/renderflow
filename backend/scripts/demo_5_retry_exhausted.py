"""DEMO 5：达到重试上限后失败——随后手动重试一次成功。"""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import demo_common as dc


def main() -> None:
    dc.banner(5, "重试上限耗尽后失败 + 手动重试恢复")
    env = dc.env_holder(dc.prepare("demo5"))
    dc.init_db(env)

    # max_attempts=1 + fail_first_n_attempts=1 → 第 1 次失败即耗尽，任务 FAILED
    task_id = dc.create_task({
        "input": {"assets": [{"name": "exhaust.png", "size": 320}]},
        "fault_injection": {"generate_audio": {
            "fail_first_n_attempts": 1, "failure_type": "TRANSIENT_ERROR", "max_attempts": 1,
        }},
    })
    dc.say(f"任务已创建（generate_audio 上限 1 次、首试必败）: {task_id}")

    from renderflow import dao
    w = dc.start_worker(env, "demo5")
    conn = dc.open_db(env)
    try:
        task = dc.wait_task_terminal(conn, dao, task_id, timeout=60)
        steps = {s["step_type"]: s for s in dao.list_steps(conn, task_id)}
        dc.say(f"任务终态: {task['status']}  错误摘要: {task['error_summary']}")
        dc.say("各步骤: " + ", ".join(f"{t}={s['status']}" for t, s in steps.items())
               + "（下游 compose/export 被标记 SKIPPED，避免任务永久 RUNNING）")

        ga = steps["generate_audio"]
        atts = dao.list_attempts(conn, ga["step_id"])
        dc.say(f"generate_audio 尝试历史: " + ", ".join(f"#{a['attempt_no']}:{a['status']}" for a in atts))

        # 手动重试（API 层逻辑：依赖校验 + 幂等）
        result = dao.retry_step(conn, ga["step_id"], log_dir=env["RENDERFLOW_LOG_DIR"])
        dc.say(f"手动重试已受理: {dict(result)}")
        # 注意：fault 是 fail_first_n_attempts=1——首次已失败过，重试的新 attempt_no=2 将成功
        task2 = dc.wait_task_terminal(conn, dao, task_id, timeout=60)
        atts2 = dao.list_attempts(conn, ga["step_id"])
        dc.say("重试后尝试历史: " + ", ".join(f"#{a['attempt_no']}:{a['status']}" for a in atts2))
        ok = (task["status"] == "FAILED"
              and steps["compose_video"]["status"] == "SKIPPED"
              and task2["status"] == "SUCCEEDED"
              and len(atts2) == 2)
        dc.finish(ok, f"task={task_id} FAILED(重试耗尽) → 手动重试 → SUCCEEDED, 历史完整保留")
    finally:
        conn.close()
        dc.stop_worker(w)


if __name__ == "__main__":
    main()
