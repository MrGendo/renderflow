"""MOCK 步骤执行器：分片 sleep + 由 input_hash 种子生成的确定性伪输出。

W2 增量：故障注入（fail_first_n_attempts/always_fail/timeout_after_s/crash_worker）、
步骤超时熔断、控制流异常（LeaseLostError/StepCancelledError）直接上抛给 Worker。
"""

import os
import random
import time
from dataclasses import dataclass, field
from typing import Callable

from . import storage, workflow
from .exceptions import LeaseLostError, StepCancelledError

_TICK_S = 0.1  # 分片时长：100ms，片间隙是心跳/取消/中止检查点


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
    timeout_s: float = 30.0  # 步骤超时熔断（spec.timeout_s）
    faults: dict = field(default_factory=dict)  # 该步骤的故障注入配置


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
    """执行一个分片 sleep 的 MOCK 步骤。

    - on_tick 每片回调（Worker 用于续租心跳 + 取消检查）
    - LeaseLostError/StepCancelledError 是控制流信号，不吞、直接上抛
    - 故障注入仅在测试/演示生效（API 层 RENDERFLOW_ALLOW_FAULT_INJECTION 生效门已拦截）
    """
    fault = ctx.faults or {}
    # 模拟"执行卡死"：sleep 满 timeout_after_s 后以 TIMEOUT 失败（与超时熔断同码）
    fault_timeout_s = fault.get("timeout_after_s")
    try:
        _log(ctx, "STEP_START", attempt_id=ctx.attempt_id, duration_ms=ctx.duration_ms,
             faults=fault or None)
        start = time.monotonic()
        deadline = start + ctx.duration_ms / 1000.0
        hard_deadline = start + ctx.timeout_s
        fault_deadline = start + float(fault_timeout_s) if fault_timeout_s is not None else None
        if fault_deadline is not None:
            deadline = min(deadline, fault_deadline)
        tick_no = 0
        while True:
            now_m = time.monotonic()
            if now_m >= deadline:
                break
            if now_m >= hard_deadline:
                _log(ctx, "STEP_END", success=False, error="step timeout")
                return StepResult(
                    success=False, error_code="TIMEOUT",
                    error_message=f"步骤超过 timeout_s={ctx.timeout_s}s 被熔断",
                )
            time.sleep(min(_TICK_S, deadline - now_m, hard_deadline - now_m))
            tick_no += 1
            if fault.get("crash_worker") and ctx.attempt_no <= int(fault.get("crash_first_n_attempts", 1)):
                # 默认只崩 1 次（crash_first_n_attempts 可调），否则重启后永远无法完成任务
                _log(ctx, "WORKER_CRASH", exit_code=137)
                os._exit(137)  # 模拟进程被 SIGKILL：不做任何清理，租约留给回收器
            if on_tick:
                on_tick()
            _log(ctx, "TICK", tick=tick_no)
        if fault_deadline is not None and deadline == fault_deadline:
            # min() 精确返回 fault_deadline 即故障超时先到（而非正常完成）
            _log(ctx, "STEP_END", success=False, error="fault timeout_after_s")
            return StepResult(
                success=False, error_code="TIMEOUT",
                error_message=f"故障注入: 执行 {fault_timeout_s}s 后超时",
            )
        if fault.get("always_fail") or ctx.attempt_no <= int(fault.get("fail_first_n_attempts", 0)):
            code = fault.get("failure_type", "TRANSIENT_ERROR")
            _log(ctx, "STEP_END", success=False, error=f"fault injection: {code}")
            return StepResult(
                success=False, error_code=code,
                error_message=f"故障注入: attempt_no={ctx.attempt_no} 注入 {code}",
            )
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
    except (LeaseLostError, StepCancelledError):
        raise  # 控制流信号：Worker 负责放弃步骤 / 标记取消
    except Exception as err:
        _log(ctx, "STEP_END", success=False, error=str(err))
        return StepResult(success=False, error_code="INTERNAL_ERROR", error_message=repr(err))
