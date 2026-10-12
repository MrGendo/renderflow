"""DEMO 2：首次失败后自动重试恢复成功——TRANSIENT_ERROR + 指数退避。"""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import demo_common as dc


def main() -> None:
    dc.banner(2, "首次失败后恢复成功（generate_clips 前两次 TRANSIENT 失败）")
    env = dc.env_holder(dc.prepare("demo2"))
    dc.init_db(env)

    task_id = dc.create_task({
        "input": {"assets": [{"name": "retry.png", "size": 768}]},
        "fault_injection": {"generate_clips": {
            "fail_first_n_attempts": 2, "failure_type": "TRANSIENT_ERROR",
        }},
    })
    dc.say(f"任务已创建（注入: generate_clips 前 2 次尝试失败）: {task_id}")

    w = dc.start_worker(env, "demo2")
    conn = dc.open_db(env)
    try:
        from renderflow import dao
        task = dc.wait_task_terminal(conn, dao, task_id, timeout=90)
        steps = {s["step_type"]: s for s in dao.list_steps(conn, task_id)}
        atts = dao.list_attempts(conn, steps["generate_clips"]["step_id"])
        dc.say("generate_clips 尝试历史:")
        for a in atts:
            dc.say(f"  attempt_no={a['attempt_no']} status={a['status']}"
                   + (f" error={a['error_code']}" if a["error_code"] else "")
                   + f" duration_ms={a['duration_ms']}")
        evs = [e["event_type"] for e in dao.list_events(conn, task_id, limit=300)]
        ok = (task["status"] == "SUCCEEDED"
              and len(atts) == 3
              and sum(1 for a in atts if a["status"] == "FAILED") == 2
              and "STEP_RETRY_SCHEDULED" in evs)
        dc.finish(ok, f"task={task_id} status=SUCCEEDED, generate_clips 3 次尝试(2败1成), 重试事件已记录")


    finally:
        conn.close()
        dc.stop_worker(w)


if __name__ == "__main__":
    main()
