"""DEMO 1：正常任务全流程——六步 DAG 顺序+并行推进到 SUCCEEDED。"""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import demo_common as dc


def main() -> None:
    dc.banner(1, "正常执行的任务（无故障）")
    env = dc.env_holder(dc.prepare("demo1"))
    dc.init_db(env)

    task_id = dc.create_task({
        "input": {"assets": [
            {"name": "hero.png", "size": 1024},
            {"name": "bg.png", "size": 2048},
            {"name": "sfx.mp3", "size": 512},
        ]},
    })
    dc.say(f"任务已创建: {task_id}")

    w = dc.start_worker(env, "demo1")
    conn = dc.open_db(env)
    try:
        from renderflow import dao
        task = dc.wait_task_terminal(conn, dao, task_id, timeout=60)
        steps = {s["step_type"]: s for s in dao.list_steps(conn, task_id)}
        dc.say("最终步骤状态: " + ", ".join(f"{t}={s['status']}" for t, s in steps.items()))
        atts_total = sum(len(dao.list_attempts(conn, s["step_id"])) for s in steps.values())
        dc.say(f"执行尝试总数: {atts_total}（6 步各 1 次）")
        ok = task["status"] == "SUCCEEDED" and all(s["status"] == "SUCCEEDED" for s in steps.values()) and atts_total == 6
        dc.finish(ok, f"task={task_id} status=SUCCEEDED, 6/6 步成功, attempts=6")


    finally:
        conn.close()
        dc.stop_worker(w)


if __name__ == "__main__":
    main()
