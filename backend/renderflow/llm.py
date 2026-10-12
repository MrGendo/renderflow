"""真实 LLM 客户端（OpenAI 兼容 chat/completions）+ Mock 模式 + 结构化输出校验。

设计要点：
- 无 Key 时自动 Mock 模式（llm_mode=mock），产物与事件**明确标记** mock，绝不冒充真实模型。
- LLM 调用在 Worker 线程池中并发执行 + 主线程分片等待：心跳/租约/取消检查点不中断
  （否则 30s 的模型调用会超过 15s 租约被回收器误判）。
- 可观测：模型名、耗时、尝试次数、错误分类、Token 用量（API 返回时）、输出摘要、重试原因
  全部落 run.log 与 task_events；日志不落 API Key。
- at-least-once 诚实声明：调用成功但进程在持久化前崩溃 → 恢复后重试会再次调用模型
  （外部副作用无法消除，靠幂等 prompt + 结果缓存降低概率，不宣称 exactly-once）。
"""

import json
import os
import random
import re
import time
import urllib.error
import urllib.request

from . import artifacts


class LLMError(Exception):
    """分类错误：error_code 决定是否可重试（对齐 ErrorCode 枚举语义）。"""

    def __init__(self, error_code: str, message: str, retryable: bool, http_status: int | None = None):
        self.error_code = error_code
        self.retryable = retryable
        self.http_status = http_status
        super().__init__(message)


# ---------------------------------------------------------------- 配置

def llm_settings() -> dict:
    """从环境读取 LLM 配置（独立于 Settings，便于测试 monkeypatch env）。"""
    return {
        "base_url": os.environ.get("RENDERFLOW_LLM_BASE_URL", "").rstrip("/"),
        "api_key": os.environ.get("RENDERFLOW_LLM_API_KEY", ""),
        "model": os.environ.get("RENDERFLOW_LLM_MODEL", ""),
        "timeout_s": float(os.environ.get("RENDERFLOW_LLM_TIMEOUT_S", "60")),
        "max_llm_attempts": int(os.environ.get("RENDERFLOW_LLM_MAX_ATTEMPTS", "2")),
    }


def llm_mode() -> str:
    cfg = llm_settings()
    return "real" if (cfg["base_url"] and cfg["api_key"] and cfg["model"]) else "mock"


# ---------------------------------------------------------------- 场景分析：真实调用

PROMPT_TEMPLATE = """你是一个动画镜头分析师。根据以下真实素材解析结果，输出镜头分析。

素材解析结果（parse_assets 产出，含真实 PNG 字节级解码统计）：
{assets_json}

要求：
1. 只输出一个 JSON 对象，不要输出任何其他文字或代码块标记。
2. 结构：{{"scenes": [{{"id": "scene_XX", "description": "中文镜头描述", "luminance_hint": 0.0-1.0, "estimated_duration_s": 数字}}]}}
3. scenes 数组长度必须为 {scene_count}。
4. luminance_hint 参考（但不必须等于）素材平均亮度 {mean_brightness:.3f}；estimated_duration_s 在 1-8 之间。
"""


def build_prompt(assets_stats: list[dict], scene_count: int, mean_brightness: float) -> str:
    slim = [{"name": a["name"], "kind": a.get("kind"), "bytes": a.get("bytes"),
             "mean_brightness": a.get("mean_brightness")} for a in assets_stats]
    return PROMPT_TEMPLATE.format(
        assets_json=json.dumps(slim, ensure_ascii=False),
        scene_count=scene_count, mean_brightness=mean_brightness,
    )


def call_llm_real(prompt: str, cfg: dict) -> tuple[str, dict]:
    """真实调用 OpenAI 兼容端点。返回 (content, usage)。抛 LLMError。"""
    url = cfg["base_url"] + "/chat/completions"
    body = json.dumps({
        "model": cfg["model"],
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
        "max_tokens": 1024,
    }).encode()
    req = urllib.request.Request(url, method="POST", data=body, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {cfg['api_key']}",
    })
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 直连，不走系统代理
    try:
        with opener.open(req, timeout=cfg["timeout_s"]) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        msg = f"HTTP {e.code}"
        try:
            err_body = e.read().decode()[:300]
            msg += f" {err_body}"
        except Exception:
            pass
        if e.code == 429:
            raise LLMError("RESOURCE_UNAVAILABLE", f"限流: {msg}", retryable=True, http_status=429)
        if e.code in (500, 502, 503, 504, 529):
            raise LLMError("TRANSIENT_ERROR", f"服务临时错误: {msg}", retryable=True, http_status=e.code)
        # 401/403/404 等属于配置错误，不盲目重试
        raise LLMError("INVALID_INPUT", f"调用被拒绝(检查 base_url/key/model): {msg}", retryable=False, http_status=e.code)
    except urllib.error.URLError as e:
        reason = str(getattr(e, "reason", e))
        if "timed out" in reason or "timeout" in reason:
            raise LLMError("TIMEOUT", f"模型调用超时(>{cfg['timeout_s']}s)", retryable=True)
        raise LLMError("TRANSIENT_ERROR", f"网络错误: {reason}", retryable=True)
    except TimeoutError:
        raise LLMError("TIMEOUT", f"模型调用超时(>{cfg['timeout_s']}s)", retryable=True)
    try:
        content = data["choices"][0]["message"]["content"]
        usage = data.get("usage") or {}
    except (KeyError, IndexError, TypeError) as e:
        raise LLMError("OUTPUT_INVALID", f"响应结构异常: {e!r}", retryable=True)
    if not content or not content.strip():
        raise LLMError("OUTPUT_INVALID", "模型返回空内容", retryable=True)
    return content, usage


def parse_scenes_json(content: str, scene_count: int) -> list[dict]:
    """解析并校验模型输出为合法场景结构；不合法抛 LLMError(OUTPUT_INVALID, retryable)。"""
    text = content.strip()
    m = re.search(r"\{.*\}", text, re.DOTALL)  # 容错：剥掉可能的 ```json 包裹
    if m:
        text = m.group(0)
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as e:
        raise LLMError("OUTPUT_INVALID", f"非合法 JSON: {e.msg}", retryable=True)
    scenes = obj.get("scenes") if isinstance(obj, dict) else None
    if not isinstance(scenes, list) or len(scenes) != scene_count:
        raise LLMError("OUTPUT_INVALID",
                       f"scenes 数量不符: 期望 {scene_count} 实得 {len(scenes) if isinstance(scenes, list) else type(scenes).__name__}",
                       retryable=True)
    out = []
    for i, s in enumerate(scenes):
        if not isinstance(s, dict) or not s.get("description"):
            raise LLMError("OUTPUT_INVALID", f"scene[{i}] 缺 description", retryable=True)
        try:
            lum = float(s.get("luminance_hint", 0.5))
            dur = float(s.get("estimated_duration_s", 3.0))
        except (TypeError, ValueError):
            raise LLMError("OUTPUT_INVALID", f"scene[{i}] 数值字段非法", retryable=True)
        out.append({
            "id": str(s.get("id") or f"scene_{i:02d}"),
            "description": str(s["description"])[:200],
            "luminance_hint": max(0.0, min(1.0, lum)),
            "estimated_duration_s": max(0.5, min(8.0, dur)),
        })
    return out


# ---------------------------------------------------------------- Mock 模式（明确标记）

def call_llm_mock(prompt: str, seed: int) -> str:
    """Mock：确定性生成合法场景 JSON。输出恒定标注 source=mock，绝不冒充真实模型。"""
    rng = random.Random(seed)
    m = re.search(r"长度必须为 (\d+)", prompt)
    n = int(m.group(1)) if m else 4
    mb = re.search(r"素材平均亮度 ([0-9.]+)", prompt)
    base = float(mb.group(1)) if mb else 0.5
    scenes = []
    for i in range(n):
        lum = max(0.05, min(1.0, base + rng.uniform(-0.2, 0.2)))
        scenes.append({
            "id": f"scene_{i:02d}",
            "description": f"[MOCK] 镜头{i + 1}：基于素材亮度{base:.2f}的确定性模拟描述 {rng.randrange(100, 999)}",
            "luminance_hint": round(lum, 3),
            "estimated_duration_s": round(rng.uniform(1.0, 6.0), 1),
        })
    return json.dumps({"scenes": scenes}, ensure_ascii=False)


# ---------------------------------------------------------------- 统一入口（executor 调用）

def analyze_scenes_via_llm(assets_stats: list[dict], scene_count: int, mean_brightness: float,
                           input_hash: str, log_fn, should_stop=None) -> dict:
    """带重试与可观测记录的场景分析。返回 payload dict（含 llm 元数据）。

    - log_fn(event, **fields)：写 run.log
    - should_stop()：Worker 心跳/取消检查点（分片等待期间每 0.5s 调用）
    - 每次尝试独立计时；LLMError(retryable) 且未超次数 → 重试（退避 1s/2s）
    """
    mode = llm_mode()
    cfg = llm_settings()
    prompt = build_prompt(assets_stats, scene_count, mean_brightness)
    prompt_digest = format(sum(prompt.encode()) & 0xFFFFFFFF, "08x")  # 内容摘要（不落全文，防日志膨胀）

    attempts_meta = []
    max_attempts = cfg["max_llm_attempts"] if mode == "real" else 1
    last_err: LLMError | None = None
    for attempt in range(1, max_attempts + 1):
        t0 = time.monotonic()
        try:
            if mode == "real":
                content, usage = call_llm_real(prompt, cfg)
            else:
                time.sleep(0.05)  # 模拟一点延迟，保持步态一致
                content = call_llm_mock(prompt, int(input_hash[:16], 16))
                usage = {}
            scenes = parse_scenes_json(content, scene_count)
            dur_ms = int((time.monotonic() - t0) * 1000)
            meta = {
                "llm_mode": mode, "llm_model": cfg["model"] if mode == "real" else "mock-scene-analyzer",
                "llm_attempt": attempt, "llm_duration_ms": dur_ms,
                "llm_prompt_digest": prompt_digest,
                "llm_usage": {"prompt_tokens": usage.get("prompt_tokens"),
                              "completion_tokens": usage.get("completion_tokens"),
                              "total_tokens": usage.get("total_tokens")} if usage else None,
                "llm_output_summary": " | ".join(s["description"][:30] for s in scenes[:3]) + (" …" if len(scenes) > 3 else ""),
                "llm_source": "real_model" if mode == "real" else "mock",
            }
            log_fn("LLM_CALL_OK", **meta)
            attempts_meta.append(meta)
            return {
                "scene_ids": [s["id"] for s in scenes],
                "scene_luminance": [round(s["luminance_hint"], 3) for s in scenes],
                "scene_descriptions": [s["description"] for s in scenes],
                "scene_durations_s": [s["estimated_duration_s"] for s in scenes],
                "llm": meta,  # 消费方与 HTML 报告都从这里取真实标记
            }
        except LLMError as e:
            dur_ms = int((time.monotonic() - t0) * 1000)
            log_fn("LLM_CALL_FAIL", llm_mode=mode, llm_model=cfg["model"] or "mock",
                   llm_attempt=attempt, llm_duration_ms=dur_ms,
                   error_code=e.error_code, retryable=e.retryable,
                   http_status=e.http_status, error=str(e)[:200])
            attempts_meta.append({"attempt": attempt, "error_code": e.error_code, "duration_ms": dur_ms})
            last_err = e
            if not e.retryable or attempt >= max_attempts:
                break
            time.sleep(min(2 ** (attempt - 1), 2))  # 1s, 2s 退避；期间不阻塞心跳太久
    # 全部尝试失败：抛给 executor 走步骤级重试引擎（分类码原样传递）
    raise last_err  # type: ignore[misc]
