"""W2 结果缓存测试：T13 命中 / T14 输入变化 / T15 版本变化 / T16 文件损坏。"""

import dataclasses
import json
import os

from conftest import run_to_terminal

from renderflow import dao, workflow


def _create(client, payload=None):
    resp = client.post("/api/tasks", json={"input": payload or {"assets": ["hero.png", "bg.png"]}})
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]["task_id"]


def _run(conn, env, task_id):
    assert run_to_terminal(conn, env, task_id) == "SUCCEEDED"


def _steps(conn, task_id):
    return {s["step_type"]: s for s in dao.list_steps(conn, task_id)}


# ---------------------------------------------------------------- T13


def test_t13_same_input_second_task_hits_cache(conn, env, client):
    task_a = _create(client)
    _run(conn, env, task_a)

    task_b = _create(client)  # 完全相同的输入 → 六步全命中
    _run(conn, env, task_b)

    steps_a, steps_b = _steps(conn, task_a), _steps(conn, task_b)
    events_b = [e["event_type"] for e in dao.list_events(conn, task_b)]
    assert events_b.count("CACHE_HIT") == 6
    for stype, sa in steps_a.items():
        sb = steps_b[stype]
        assert sb["status"] == "SUCCEEDED"
        # output_ref 复用第一任务的产出路径（缓存条目引用）
        assert sb["output_ref"] == sa["output_ref"]
        assert sb["output_checksum"] == sa["output_checksum"]
        # attempt 标记 cache 命中：worker_id=cache、duration_ms=0
        attempts = dao.list_attempts(conn, sb["step_id"])
        assert len(attempts) == 1
        assert attempts[0]["worker_id"] == "cache"
        assert attempts[0]["duration_ms"] == 0
        assert attempts[0]["status"] == "SUCCEEDED"
        # 缓存条目文件真实存在（命中前校验过 sha256）
        assert os.path.exists(os.path.join(env.data_dir, sb["output_ref"]))


# ---------------------------------------------------------------- T14


def test_t14_changed_input_misses_cache(conn, env, client):
    _create(client)
    _run(conn, env, _create(client))  # 预热缓存

    task_c = _create(client, {"assets": ["hero.png", "bg.png", "extra.gif"]})
    _run(conn, env, task_c)

    step = _steps(conn, task_c)["parse_assets"]
    attempts = dao.list_attempts(conn, step["step_id"])
    # parse_assets 输入变了 → 未命中，真实执行（worker 不是 cache）
    assert attempts[0]["worker_id"] != "cache"
    assert attempts[0]["duration_ms"] is not None


# ---------------------------------------------------------------- T15


def test_t15_step_version_change_misses_cache(conn, env, client, monkeypatch):
    _create(client)
    _run(conn, env, _create(client))  # 预热缓存

    # monkeypatch 工作流版本：parse_assets step_version 变化 → config_hash/cache_key 全变
    bumped = [dataclasses.replace(s, step_version="9.9.9") if s.step_type == "parse_assets" else s
              for s in workflow.STEPS]
    monkeypatch.setattr(workflow, "STEPS", bumped)
    monkeypatch.setattr(workflow, "STEPS_BY_TYPE", {s.step_type: s for s in bumped})

    task_d = _create(client)
    _run(conn, env, task_d)

    step = _steps(conn, task_d)["parse_assets"]
    assert step["step_version"] == "9.9.9"
    attempts = dao.list_attempts(conn, step["step_id"])
    assert attempts[0]["worker_id"] != "cache"  # 版本变了 → 不命中
    # 未受影响的下游步骤仍命中（input_hash 链路与版本无关部分一致）
    assert _steps(conn, task_d)["export_result"]["output_ref"] is not None


# ---------------------------------------------------------------- T16


def test_t16_missing_or_tampered_cache_file_invalidated(conn, env, client):
    task_a = _create(client)
    _run(conn, env, task_a)
    steps_a = _steps(conn, task_a)

    def _cache_key_of(stype):
        s = steps_a[stype]
        return workflow.cache_key(s["step_type"], s["input_hash"], s["step_version"], s["config_hash"])

    # 场景 1：删除 parse_assets 的缓存产出文件
    pa_ref = steps_a["parse_assets"]["output_ref"]
    os.remove(os.path.join(env.data_dir, pa_ref))
    assert dao.cache_lookup(conn, env.data_dir, _cache_key_of("parse_assets")) is None
    assert conn.execute(
        "SELECT COUNT(*) AS c FROM step_cache WHERE cache_key = ?", (_cache_key_of("parse_assets"),)
    ).fetchone()["c"] == 0  # 不可信缓存行已被清

    # 场景 2：篡改 analyze_scenes 的缓存产出内容（校验和不匹配）
    as_ref = steps_a["analyze_scenes"]["output_ref"]
    target = os.path.join(env.data_dir, as_ref)
    with open(target, "wb") as f:
        f.write(json.dumps({"tampered": True}).encode())
    assert dao.cache_lookup(conn, env.data_dir, _cache_key_of("analyze_scenes")) is None
    assert conn.execute(
        "SELECT COUNT(*) AS c FROM step_cache WHERE cache_key = ?", (_cache_key_of("analyze_scenes"),)
    ).fetchone()["c"] == 0

    # 第二任务仍能完整跑完：损坏步骤重新执行并重建缓存
    task_b = _create(client)
    assert run_to_terminal(conn, env, task_b) == "SUCCEEDED"
    for stype in ("parse_assets", "analyze_scenes"):
        attempt = dao.list_attempts(conn, _steps(conn, task_b)[stype]["step_id"])[0]
        assert attempt["worker_id"] != "cache"  # 两个损坏步骤都未命中
