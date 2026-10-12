"""LLM 节点测试：Mock 模式、真实调用（本地假 OpenAI 端点）、错误分类重试、输出校验、Worker 崩溃恢复。

真实调用不打外部 API——起一个本地 HTTP 服务器伪装 OpenAI 兼容端点，
验证：请求头 Authorization/请求体结构正确、429 限流重试、非法输出重试、
Token 用量落 run.log 与 payload、Mock/真实结果标记互不冒充。
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from renderflow import dao, db, worker
from renderflow import llm as llm_mod
from tests.conftest import run_to_terminal


# ---------------------------------------------------------------- 本地假 OpenAI 服务

class FakeLLMState:
    def __init__(self):
        self.requests = []          # 收到的 (headers, body)
        self.fail_first = 0         # 前 N 次返回 429
        self.bad_json_first = 0     # 前 N 次返回非法输出
        self.usage = {"prompt_tokens": 111, "completion_tokens": 222, "total_tokens": 333}
        self.model_name = "fake-gpt-test"


STATE = FakeLLMState()


class FakeHandler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        STATE.requests.append((dict(self.headers), body))
        n = len(STATE.requests)
        if n <= STATE.fail_first:
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error": {"message": "rate limited"}}')
            return
        if n <= STATE.fail_first + STATE.bad_json_first:
            content = "这不是 JSON"
        else:
            content = json.dumps({"scenes": [
                {"id": f"scene_{i:02d}", "description": f"真实端点镜头{i}",
                 "luminance_hint": 0.3 + i * 0.1, "estimated_duration_s": 2.0 + i}
                for i in range(4)]}, ensure_ascii=False)
        resp = {"choices": [{"message": {"content": content}}],
                "usage": STATE.usage, "model": STATE.model_name}
        data = json.dumps(resp).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):  # 静默
        pass


@pytest.fixture
def fake_llm(monkeypatch):
    """起本地假端点并注入 env（真实模式三件套齐全 → llm_mode=real）。"""
    srv = HTTPServer(("127.0.0.1", 0), FakeHandler)
    port = srv.server_address[1]
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    STATE.__init__()
    for k, v in {
        "RENDERFLOW_LLM_BASE_URL": f"http://127.0.0.1:{port}/v1",
        "RENDERFLOW_LLM_API_KEY": "sk-test-fake-key-000",
        "RENDERFLOW_LLM_MODEL": "fake-gpt-test",
        "RENDERFLOW_LLM_TIMEOUT_S": "10",
        "RENDERFLOW_LLM_MAX_ATTEMPTS": "3",
    }.items():
        monkeypatch.setenv(k, v)
    yield f"http://127.0.0.1:{port}"
    srv.shutdown()


def _events(conn, task_id):
    return [e["event_type"] for e in dao.list_events(conn, task_id, limit=500)]


# ---------------------------------------------------------------- Mock 模式

def test_llm_mock_mode_marked(conn, client):
    """无 Key → Mock 模式：结果带 llm_source=mock 标记，绝不冒充 real_model。"""
    for k in ("RENDERFLOW_LLM_BASE_URL", "RENDERFLOW_LLM_API_KEY", "RENDERFLOW_LLM_MODEL"):
        os_unset = __import__("os").environ.pop(k, None)
    assert llm_mod.llm_mode() == "mock"
    task_id = dao.create_task(conn, {"assets": ["a.png", "b.png"]}, {}, "1.0.0")
    assert run_to_terminal(conn, __import__("renderflow.config", fromlist=["get_settings"]).get_settings(), task_id) == "SUCCEEDED"
    steps = {s["step_type"]: s for s in dao.list_steps(conn, task_id)}
    as_row = steps["analyze_scenes"]
    out = __import__("renderflow.storage", fromlist=["load_output"]).load_output(
        __import__("renderflow.config", fromlist=["get_settings"]).get_settings().data_dir,
        as_row["output_ref"], as_row["output_checksum"])
    payload = out["mock_payload"]
    assert payload["llm"]["llm_source"] == "mock"
    assert payload["llm"]["llm_mode"] == "mock"
    assert payload["scene_descriptions"], "Mock 也必须有结构化描述"
    assert all("[MOCK]" in d for d in payload["scene_descriptions"]), "Mock 描述必须显式标注"
    # 下游真实消费：clips 时长来自 LLM durations
    clips_row = steps["generate_clips"]
    cout = __import__("renderflow.storage", fromlist=["load_output"]).load_output(
        __import__("renderflow.config", fromlist=["get_settings"]).get_settings().data_dir,
        clips_row["output_ref"], clips_row["output_checksum"])
    assert cout["mock_payload"]["durations_s"], "下游应消费场景时长"


# ---------------------------------------------------------------- 真实模式（本地假端点）

def test_llm_real_success_with_usage(fake_llm, conn, client, env):
    """真实调用成功：Bearer 头/prompt 结构正确、Token 用量与模型名落 payload、事件可观测。"""
    task_id = dao.create_task(conn, {"assets": ["x.png", "y.png"]}, {}, "1.0.0")
    assert run_to_terminal(conn, env, task_id) == "SUCCEEDED"
    headers, body = STATE.requests[-1]
    assert headers.get("Authorization") == "Bearer sk-test-fake-key-000"
    assert body["model"] == "fake-gpt-test"
    assert "mean_brightness" in body["messages"][0]["content"]  # prompt 含真实解析统计
    steps = {s["step_type"]: s for s in dao.list_steps(conn, task_id)}
    as_row = steps["analyze_scenes"]
    out = __import__("renderflow.storage", fromlist=["load_output"]).load_output(
        env.data_dir, as_row["output_ref"], as_row["output_checksum"])
    llm_meta = out["mock_payload"]["llm"]
    assert llm_meta["llm_source"] == "real_model"
    assert llm_meta["llm_model"] == "fake-gpt-test"
    assert llm_meta["llm_usage"]["total_tokens"] == 333  # 真实端点返回的用量
    assert llm_meta["llm_duration_ms"] >= 0
    assert out["mock_payload"]["scene_descriptions"][0] == "真实端点镜头0"
    # run.log 有 LLM_CALL_OK 且不含 key
    log_path = __import__("os").path.join(env.data_dir, as_row["output_ref"]).replace("output.json", "run.log")
    log_text = open(log_path).read()
    assert "LLM_CALL_OK" in log_text
    assert "sk-test-fake-key-000" not in log_text  # 密钥绝不落日志


def test_llm_rate_limit_then_success(fake_llm, conn, client, env, monkeypatch):
    """429 限流 → LLM 内部重试 → 成功；重试原因落日志。"""
    monkeypatch.setenv("RENDERFLOW_LLM_MAX_ATTEMPTS", "3")
    STATE.fail_first = 1
    task_id = dao.create_task(conn, {"assets": ["r.png"]}, {}, "1.0.0")
    assert run_to_terminal(conn, env, task_id) == "SUCCEEDED"
    assert len(STATE.requests) >= 2, "应有至少两次调用（1 次被限流）"
    steps = {s["step_type"]: s for s in dao.list_steps(conn, task_id)}
    as_row = steps["analyze_scenes"]
    log_path = __import__("os").path.join(env.data_dir, as_row["output_ref"]).replace("output.json", "run.log")
    log_text = open(log_path).read()
    assert "LLM_CALL_FAIL" in log_text and "RESOURCE_UNAVAILABLE" in log_text
    assert "LLM_CALL_OK" in log_text


def test_llm_invalid_output_retried(fake_llm, conn, client, env, monkeypatch):
    """非法输出（非 JSON）→ OUTPUT_INVALID 可重试 → 重试后成功。"""
    monkeypatch.setenv("RENDERFLOW_LLM_MAX_ATTEMPTS", "3")
    STATE.bad_json_first = 1
    task_id = dao.create_task(conn, {"assets": ["j.png"]}, {}, "1.0.0")
    assert run_to_terminal(conn, env, task_id) == "SUCCEEDED"
    log_path = None
    for s in dao.list_steps(conn, task_id):
        if s["step_type"] == "analyze_scenes":
            log_path = __import__("os").path.join(env.data_dir, s["output_ref"]).replace("output.json", "run.log")
    assert log_path is not None
    log_text = open(log_path).read()
    assert "OUTPUT_INVALID" in log_text


def test_llm_exhausted_fails_step_then_manual_retry(fake_llm, conn, client, env, monkeypatch):
    """限流耗尽（LLM 内部 max_attempts=1）→ 步骤级重试接管；步骤上限=1 时 FAILED → 手动重试成功。"""
    monkeypatch.setenv("RENDERFLOW_LLM_MAX_ATTEMPTS", "1")
    monkeypatch.setenv("RENDERFLOW_RETRY_BACKOFF_BASE_S", "0.01")
    monkeypatch.setenv("RENDERFLOW_RETRY_BACKOFF_MAX_S", "0.05")
    from renderflow.config import reset_settings
    reset_settings()
    STATE.fail_first = 99  # 持续限流：LLM 内部不重试 + 步骤上限 1 → 必 FAILED
    task_id = dao.create_task(conn, {"assets": ["e.png"]}, {}, "1.0.0",
                              fault_injection={"analyze_scenes": {"max_attempts": 1}})
    assert run_to_terminal(conn, env, task_id) == "FAILED"
    steps = {s["step_type"]: s for s in dao.list_steps(conn, task_id)}
    assert steps["analyze_scenes"]["error_code"] == "RESOURCE_UNAVAILABLE"
    assert steps["generate_clips"]["status"] == "SKIPPED"  # 下游不被执行
    # 故障修复后手动重试（限流解除）
    STATE.fail_first = 0
    dao.retry_step(conn, steps["analyze_scenes"]["step_id"])
    assert run_to_terminal(conn, env, task_id) == "SUCCEEDED"


def test_llm_worker_killed_during_call_recovers(fake_llm, conn, client, env):
    """Worker 崩溃恢复：LLM 结果落库前被 SIGKILL → 租约过期 → 新 Worker 重执行成功。

    at-least-once 说明：模型可能已被调用过一次（外部副作用无法消除），
    恢复靠重执行而非结果延续——此测试固化该语义。
    """
    import os
    import signal
    import subprocess
    import sys
    task_id = dao.create_task(conn, {"assets": ["k.png"]}, {}, "1.0.0",
                              fault_injection={"analyze_scenes": {"crash_worker": True}})
    wenv = dict(os.environ)
    wenv.update({
        "RENDERFLOW_DB_PATH": str(env.db_path), "RENDERFLOW_DATA_DIR": str(env.data_dir),
        "RENDERFLOW_LOG_DIR": str(env.log_dir), "RENDERFLOW_POLL_INTERVAL_MS": "30",
        "RENDERFLOW_LEASE_SECONDS": "1.2", "RENDERFLOW_HEARTBEAT_SECONDS": "0.4",
        "RENDERFLOW_STEP_DURATION_MS": "50",
        "RENDERFLOW_RETRY_BACKOFF_BASE_S": "0.01", "RENDERFLOW_RETRY_BACKOFF_MAX_S": "0.05",
        "RENDERFLOW_ALLOW_FAULT_INJECTION": "1",
    })
    cwd = os.getcwd()
    w1 = subprocess.Popen([sys.executable, "-m", "renderflow.worker"],
                          cwd=cwd, env=wenv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    w2 = None
    try:
        deadline = time.time() + 20
        while time.time() < deadline:
            if w1.poll() is not None:
                break
            time.sleep(0.2)
        assert w1.returncode in (-9, 137), "Worker 应已在 LLM 步骤中自杀"
        w2 = subprocess.Popen([sys.executable, "-m", "renderflow.worker"],
                              cwd=cwd, env=wenv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + 30
        while time.time() < deadline:
            if dao.get_task(conn, task_id)["status"] in ("SUCCEEDED", "FAILED", "CANCELLED"):
                break
            time.sleep(0.3)
        assert dao.get_task(conn, task_id)["status"] == "SUCCEEDED"
        steps = {s["step_type"]: s for s in dao.list_steps(conn, task_id)}
        atts = dao.list_attempts(conn, steps["analyze_scenes"]["step_id"])
        assert any(a["error_code"] == "WORKER_INTERRUPTED" for a in atts), "旧 attempt 应补记中断"
        assert any(a["status"] == "SUCCEEDED" for a in atts)
    finally:
        for p in (w1, w2):
            if p is not None and p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    p.kill()
