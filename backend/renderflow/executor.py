"""MOCK 步骤执行器：分片 sleep + 由 input_hash 种子生成的确定性伪输出。

W1 只实现成功路径；错误分类/故障注入在 W2 通过 execute 的失败分支填充。
"""

import random
import time
from dataclasses import dataclass, field
from typing import Callable

from . import storage, workflow

_TICK_S = 0.1  # 分片时长：100ms，W2 在片间隙插入心跳/取消检查


@dataclass
class StepContext:
    task_id: str
    step_id: str
    attempt_id: str
    step_type: str
    attempt_no: int
    input_hash: str
    inputs: dict
    duration_ms: int
    log_ref: str  # run.log 相对路径
    data_dir: str


@dataclass
class StepResult:
    success: bool
    error_code: str | None = None
    error_message: str | None = None
    output: dict | None = None  # 成功时待提交的 output.json 内容


def _rand(input_hash: str) -> random.Random:
    """以 input_hash 为种子：相同输入 → 相同伪内容（缓存测试的前提）。"""
    return random.Random(int(input_hash[:16], 16))


def _names(assets: list) -> list[str]:
    return [a if isinstance(a, str) else str(a.get("name", f"asset_{i}")) for i, a in enumerate(assets)]


def _mock_payload(ctx: StepContext) -> dict:
    """按步骤类型生成不同的确定性伪内容。"""
    rng = _rand(ctx.input_hash)
    cfg = workflow.get_step_spec(ctx.step_type).config
    if ctx.step_type == "parse_assets":
        names = _names(ctx.inputs.get("assets", []))
        return {"asset_names": names, "asset_count": len(names)}
    if ctx.step_type == "analyze_scenes":
        n = int(cfg["scene_count"])
        return {
            "scene_ids": [f"scene_{rng.randrange(1 << 32):08x}" for _ in range(n)],
            "cut_points": sorted(rng.randrange(1000, 9000) for _ in range(n - 1)),
        }
    if ctx.step_type == "generate_clips":
        n = int(cfg["clips_per_scene"]) * len(ctx.inputs.get("scenes", {}).get("scene_ids", []))
        return {
            "clip_ids": [f"clip_{rng.randrange(1 << 32):08x}" for _ in range(max(n, 1))],
            "durations_s": [round(rng.uniform(1.0, 5.0), 2) for _ in range(max(n, 1))],
        }
    if ctx.step_type == "generate_audio":
        count = len(ctx.inputs.get("scenes", {}).get("scene_ids", [])) or 1
        tracks = [round(rng.uniform(2.0, 10.0), 2) for _ in range(count)]
        return {"track_count": count, "track_durations_s": tracks, "sample_rate": cfg["sample_rate"]}
    if ctx.step_type == "compose_video":
        frames = int(cfg["fps"]) * 2
        return {"fps": cfg["fps"], "frame_count": frames, "codec": "mock-h264",
                "render_seed": rng.randrange(1 << 32)}
    if ctx.step_type == "export_result":
        return {"format": cfg["format"], "size_bytes": rng.randrange(1024, 1 << 20)}
    return {"seed": rng.randrange(1 << 32)}  # 兜底：未知步骤也给确定性输出


def _log(ctx: StepContext, event: str, **extra) -> None:
    storage.append_run_log(ctx.data_dir, ctx.log_ref, {"t": time.time(), "event": event, **extra})


def execute(ctx: StepContext, on_tick: Callable[[], None] | None = None) -> StepResult:
    """执行一个分片 sleep 的 MOCK 步骤。on_tick 每片回调（Worker 用于续租心跳）。"""
    try:
        _log(ctx, "STEP_START", attempt_id=ctx.attempt_id, duration_ms=ctx.duration_ms)
        deadline = time.monotonic() + ctx.duration_ms / 1000.0
        tick_no = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(_TICK_S, remaining))
            tick_no += 1
            if on_tick:
                on_tick()
            _log(ctx, "TICK", tick=tick_no)
        payload = {
            "step_type": ctx.step_type,
            "task_id": ctx.task_id,
            "attempt_no": ctx.attempt_no,
            "produced_at": time.time(),
            "inputs_digest": ctx.input_hash,
            "mock_payload": _mock_payload(ctx),
        }
        _log(ctx, "STEP_END", success=True, ticks=tick_no)
        return StepResult(success=True, output=payload)
    except Exception as err:  # W1 统一按 INTERNAL_ERROR 上抛给 Worker 记账
        _log(ctx, "STEP_END", success=False, error=str(err))
        return StepResult(success=False, error_code="INTERNAL_ERROR", error_message=repr(err))
