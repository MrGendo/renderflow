"""W1 基础流程测试：T1 全流程 / T2 状态一致 / T3 依赖顺序 / T4 查询。"""

import hashlib
import json
import os

from renderflow import dao, worker
from conftest import run_to_terminal

INPUT = {"assets": ["hero.png", "bg.png", "cat.gif"]}


def _create(client, **overrides):
    body = {"input": INPUT} | overrides
    resp = client.post("/api/tasks", json=body)
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    return data["task_id"]


# ---------------------------------------------------------------- T1


def test_t1_full_flow_succeeds(conn, env, client):
    task_id = _create(client)

    status = run_to_terminal(conn, env, task_id)
    assert status == "SUCCEEDED"

    # 任务级：API 与 DB 均为 SUCCEEDED，进度 100
    detail = client.get(f"/api/tasks/{task_id}").json()["data"]
    assert detail["status"] == "SUCCEEDED"
    assert detail["reason_code"] == "SUCCEEDED"
    assert detail["progress_percent"] == 100
    assert detail["succeeded_steps"] == 6
    assert detail["finished_at"] is not None

    # 步骤级：6 步全 SUCCEEDED，输出文件存在且 sha256 与 DB 一致
    steps = dao.list_steps(conn, task_id)
    assert len(steps) == 6
    for step in steps:
        assert step["status"] == "SUCCEEDED"
        out_path = os.path.join(env.data_dir, step["output_ref"])
        assert os.path.exists(out_path), f"输出文件缺失: {out_path}"
        digest = hashlib.sha256(open(out_path, "rb").read()).hexdigest()
        assert digest == step["output_checksum"]
        # 尝试级：各 1 条且 SUCCEEDED
        attempts = dao.list_attempts(conn, step["step_id"])
        assert len(attempts) == 1
        assert attempts[0]["status"] == "SUCCEEDED"
        assert attempts[0]["output_ref"] == step["output_ref"]
        assert attempts[0]["duration_ms"] is not None
        # 执行日志存在且为 JSON 行式
        log_path = os.path.join(env.data_dir, attempts[0]["log_ref"])
        assert os.path.exists(log_path)
        lines = [json.loads(l) for l in open(log_path) if l.strip()]
        assert lines[0]["event"] == "STEP_START"
        assert lines[-1]["event"] == "STEP_END" and lines[-1]["success"] is True

    # 事件级：全链路关键事件齐备
    event_types = {e["event_type"] for e in dao.list_events(conn, task_id)}
    for required in ("TASK_CREATED", "STEP_CLAIMED", "STEP_STARTED", "STEP_SUCCEEDED", "TASK_SUCCEEDED"):
        assert required in event_types, f"缺少事件 {required}: {event_types}"


# ---------------------------------------------------------------- T2


def test_t2_status_consistency_and_monotonic_progress(conn, env, client):
    task_id = _create(client)
    progresses = []
    for _ in range(60):
        row = dao.get_task(conn, task_id)
        if row["status"] in ("SUCCEEDED", "FAILED", "CANCELLED"):
            break
        assert worker.run_once(conn, env, "test-worker") is True
        detail = client.get(f"/api/tasks/{task_id}").json()["data"]
        fresh = dao.get_task(conn, task_id)
        # API 返回 status 与 DB tasks.status 一致，且与 summarize 推导一致
        assert detail["status"] == fresh["status"]
        assert detail["derived_status"] == fresh["status"]
        progresses.append(detail["progress_percent"])
    assert progresses == sorted(progresses), f"进度非单调不减: {progresses}"
    assert progresses[-1] == 100
    assert dao.get_task(conn, task_id)["status"] == "SUCCEEDED"


# ---------------------------------------------------------------- T3


def test_t3_dependency_ordering(conn, env, client):
    task_id = _create(client)
    assert run_to_terminal(conn, env, task_id) == "SUCCEEDED"

    steps = {s["step_type"]: s for s in dao.list_steps(conn, task_id)}
    p, a = steps["parse_assets"], steps["analyze_scenes"]
    clips, audio = steps["generate_clips"], steps["generate_audio"]
    compose, export = steps["compose_video"], steps["export_result"]

    # analyze 晚于 parse 完成；clips/audio 晚于 analyze 完成
    assert a["started_at"] >= p["finished_at"]
    assert clips["started_at"] >= a["finished_at"]
    assert audio["started_at"] >= a["finished_at"]
    # compose 开始时其两个依赖都已 SUCCEEDED（即完成时间都早于 compose 领取时间）
    assert compose["started_at"] >= clips["finished_at"]
    assert compose["started_at"] >= audio["finished_at"]
    assert compose["started_at"] >= max(clips["finished_at"], audio["finished_at"])
    # export 最后执行且最晚完成
    assert export["started_at"] >= compose["finished_at"]
    all_finished = [s["finished_at"] for s in steps.values()]
    assert export["finished_at"] == max(all_finished)


# ---------------------------------------------------------------- T4


def test_t4_query_endpoints(conn, env, client):
    # 任务 A/C 跑完；任务 B 最后创建、不再推进，保持 PENDING
    # （领取是全局的：若 B 先建，会在推进 C 时被顺带执行掉）
    task_a = _create(client)
    assert run_to_terminal(conn, env, task_a) == "SUCCEEDED"
    task_c = _create(client, step_durations={"generate_clips": 120})
    assert run_to_terminal(conn, env, task_c) == "SUCCEEDED"
    task_b = _create(client)

    # 列表：分页
    resp = client.get("/api/tasks?page=1&pageSize=2").json()["data"]
    assert resp["total"] == 3 and len(resp["items"]) == 2
    resp = client.get("/api/tasks?page=2&pageSize=2").json()["data"]
    assert resp["total"] == 3 and len(resp["items"]) == 1
    # 状态筛选
    succeeded = client.get("/api/tasks?status=SUCCEEDED&pageSize=10").json()["data"]
    assert succeeded["total"] == 2
    assert {i["task_id"] for i in succeeded["items"]} == {task_a, task_c}
    pending = client.get("/api/tasks?status=PENDING").json()["data"]
    assert pending["total"] == 1 and pending["items"][0]["task_id"] == task_b
    # 列表项字段
    item = succeeded["items"][0]
    assert item["progress_percent"] == 100 and item["succeeded_steps"] == 6 and item["total_steps"] == 6
    assert isinstance(item["duration_ms"], int) and item["duration_ms"] > 0
    assert item["created_at"]["iso"].endswith("Z") and "epoch" in item["created_at"]

    # 详情：全字段 + reason_code + 步骤概要
    detail = client.get(f"/api/tasks/{task_a}").json()["data"]
    for field in ("task_id", "status", "input", "workflow_version", "cancel_requested",
                  "error_summary", "created_at", "started_at", "finished_at", "reason_code"):
        assert field in detail
    assert detail["input"]["assets"] == INPUT["assets"]
    assert len(detail["steps"]) == 6

    # 步骤列表：全字段
    steps = client.get(f"/api/tasks/{task_a}/steps").json()["data"]
    assert len(steps) == 6
    by_type = {s["step_type"]: s for s in steps}
    assert by_type["parse_assets"]["dependencies"] == []
    assert sorted(by_type["compose_video"]["dependencies"]) == ["generate_audio", "generate_clips"]
    assert by_type["export_result"]["status"] == "SUCCEEDED"
    assert by_type["export_result"]["input_hash"] and by_type["export_result"]["config_hash"]

    # attempts：含 log_ref/output_ref
    step_id = by_type["parse_assets"]["step_id"]
    attempts = client.get(f"/api/steps/{step_id}/attempts").json()["data"]
    assert len(attempts) == 1
    assert attempts[0]["status"] == "SUCCEEDED"
    assert attempts[0]["log_ref"] and attempts[0]["output_ref"]

    # events：升序 + limit
    events = client.get(f"/api/tasks/{task_a}/events").json()["data"]
    assert events[0]["event_type"] == "TASK_CREATED"
    ids = [e["event_id"] for e in events]
    assert ids == sorted(ids)
    limited = client.get(f"/api/tasks/{task_a}/events?limit=2").json()["data"]
    assert len(limited) == 2 and limited[0]["event_type"] == "TASK_CREATED"

    # 404 情形
    r = client.get("/api/tasks/nonexistent")
    assert r.status_code == 404 and r.json()["ok"] is False
    r = client.get("/api/steps/nonexistent/attempts")
    assert r.status_code == 404 and r.json()["error"]["code"] == "NOT_FOUND"

    # 400：空 assets / 非法状态筛选
    r = client.post("/api/tasks", json={"input": {"assets": []}})
    assert r.status_code == 400 and r.json()["error"]["code"] == "VALIDATION_ERROR"
    r = client.get("/api/tasks?status=BOGUS")
    assert r.status_code == 400


def test_t4_health_ready(client):
    assert client.get("/api/health").json() == {"ok": True, "data": {"status": "up"}}
    r = client.get("/api/ready")
    assert r.status_code == 200 and r.json()["data"]["status"] == "ready"
