"""DEMO 4：结果缓存命中——相同输入第二任务直接复用，改输入则不命中。"""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import demo_common as dc


def main() -> None:
    dc.banner(4, "命中缓存的任务（任务A正常执行 → 任务B相同输入命中缓存）")
    env = dc.env_holder(dc.prepare("demo4"))
    dc.init_db(env)

    from renderflow import dao
    assets_a = [{"name": "cache.png", "size": 640}]

    task_a = dc.create_task({"input": {"assets": assets_a}})
    dc.say(f"任务 A 已创建: {task_a}")
    w = dc.start_worker(env, "demo4")
    conn = dc.open_db(env)
    try:
        ta = dc.wait_task_terminal(conn, dao, task_a, timeout=60)
        assert ta["status"] == "SUCCEEDED", ta["status"]
        dc.say("任务 A 完成（结果已进入缓存）")

        # 任务 B：相同输入 → 应命中缓存
        task_b = dc.create_task({"input": {"assets": assets_a}})
        dc.say(f"任务 B 已创建（相同输入）: {task_b}")
        tb = dc.wait_task_terminal(conn, dao, task_b, timeout=60)

        evs_b = [e["event_type"] for e in dao.list_events(conn, task_b, limit=300)]
        cache_hits = sum(1 for e in evs_b if e == "CACHE_HIT")
        steps_b = {s["step_type"]: s for s in dao.list_steps(conn, task_b)}
        # 缓存命中步骤的 attempt duration 应为 0
        zero_dur = []
        for st, s in steps_b.items():
            for a in dao.list_attempts(conn, s["step_id"]):
                if a["status"] == "SUCCEEDED" and (a["duration_ms"] or 0) == 0:
                    zero_dur.append(st)
        dc.say(f"任务 B CACHE_HIT 事件数: {cache_hits}/6, 零耗时步骤: {zero_dur}")

        # 任务 C：改输入（加一个素材）→ 不命中
        task_c = dc.create_task({"input": {"assets": assets_a + [{"name": "extra.png", "size": 1}]}})
        dc.say(f"任务 C 已创建（输入变化）: {task_c}")
        tc = dc.wait_task_terminal(conn, dao, task_c, timeout=60)
        evs_c = [e["event_type"] for e in dao.list_events(conn, task_c, limit=300)]
        hits_c = sum(1 for e in evs_c if e == "CACHE_HIT")
        dc.say(f"任务 C CACHE_HIT 事件数: {hits_c}（应为 0）")

        ok = (tb["status"] == "SUCCEEDED" and cache_hits == 6
              and tc["status"] == "SUCCEEDED" and hits_c == 0)
        dc.finish(ok, f"A={task_a[:8]} 正常, B={task_b[:8]} 缓存命中 6/6, C={task_c[:8]} 输入变化 0 命中")
    finally:
        conn.close()
        dc.stop_worker(w)


if __name__ == "__main__":
    main()
