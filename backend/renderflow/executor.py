"""MOCK 步骤执行器：分片 sleep + 确定性输出 + 真实产物文件（PNG/GIF/WAV/HTML）。

W2 增量：故障注入（fail_first_n_attempts/always_fail/timeout_after_s/crash_worker）、
步骤超时熔断、控制流异常（LeaseLostError/StepCancelledError）直接上抛给 Worker。
产物增量（真实素材交付）：每步在 attempt 目录产出真实可打开的产物文件——
parse_assets 解码上传的真实 PNG 字节；analyze_scenes 出镜头亮度统计 PNG；
generate_clips 出真实 GIF 动画帧；generate_audio 出真实可播放 WAV；
compose_video 出逐帧合成 GIF；export_result 出 HTML 报告（内嵌全部产物）。
产物由 input_hash 种子 + 真实统计值共同决定：确定性不变（缓存/复测仍成立），
内容与输入真实相关（不同素材 → 不同亮度/时长 → 不同图/音）。
全部纯标准库实现（artifacts.py），无外部 API、无 API key。
"""

import base64
import os
import random
import time
from dataclasses import dataclass, field
from typing import Callable

from . import artifacts, storage, workflow
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
    upload_dir: str = ""  # 任务上传的真实素材目录（data/uploads/<task_id>）


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
    """按步骤类型生成不同的确定性伪内容 + 真实产物文件。"""
    rng = _rand(ctx.input_hash)
    cfg = workflow.get_step_spec(ctx.step_type).config
    art_dir = os.path.join(ctx.data_dir, storage.attempt_dir_rel(
        ctx.task_id, ctx.step_type, ctx.attempt_no))
    os.makedirs(art_dir, exist_ok=True)
    art_files: dict[str, str] = {}  # 文件名 → 说明（前端展示用）

    def save(name: str, data: bytes, note: str) -> None:
        with open(os.path.join(art_dir, name), "wb") as f:
            f.write(data)
        art_files[name] = note

    if ctx.step_type == "parse_assets":
        # 真实处理：读取上传的素材文件，统计字节数；对 PNG 做真实解码测亮度
        names = _names(ctx.inputs.get("assets", []))
        stats = []
        for name in names:
            path = os.path.join(ctx.upload_dir, name)
            entry = {"name": name, "exists": os.path.isfile(path),
                     "bytes": os.path.getsize(path) if os.path.isfile(path) else 0}
            if entry["exists"] and name.lower().endswith(".png"):
                try:
                    with open(path, "rb") as f:
                        w, h, pix = artifacts.png_decode(f.read())
                    it = pix()
                    sampled = [sum(next(it)) / 765.0 for _ in range(min(400, w * h))]
                    entry.update({"kind": "png", "width": w, "height": h,
                                  "mean_brightness": round(sum(sampled) / len(sampled), 4)})
                except Exception as e:
                    entry["decode_error"] = repr(e)[:80]
            else:
                entry["kind"] = name.rsplit(".", 1)[-1] if "." in name else "bin"
            stats.append(entry)
        # 产物：上传素材总览图（每素材一行，条长=文件大小占比）
        sizes = [min(s["bytes"] / 65536.0, 1.0) for s in stats] or [0.5]
        save("assets_overview.png", artifacts.bar_chart_png(sizes), "素材大小总览")
        return {"asset_names": names, "asset_count": len(names), "asset_stats": stats,
                "art_files": art_files}
    if ctx.step_type == "analyze_scenes":
        n = int(cfg["scene_count"])
        # 真实亮度驱动切分：用上游 parse 统计的素材平均亮度扰动种子
        brightness = [a.get("mean_brightness") for a in ctx.inputs.get("parsed_assets", {}).get("asset_stats", []) if a.get("mean_brightness") is not None]
        base_b = sum(brightness) / len(brightness) if brightness else 0.5
        scene_ids = [f"scene_{rng.randrange(1 << 32):08x}" for _ in range(n)]
        cut_points = sorted(rng.randrange(1000, 9000) for _ in range(n - 1))
        lum = [round(min(1.0, max(0.05, base_b + rng.uniform(-0.2, 0.2))), 3) for _ in range(n)]
        save("scene_brightness.png", artifacts.bar_chart_png(lum), "镜头亮度分布（来自真实素材统计）")
        return {"scene_ids": scene_ids, "cut_points": cut_points, "scene_luminance": lum,
                "art_files": art_files}
    if ctx.step_type == "generate_clips":
        scenes = ctx.inputs.get("scenes", {})
        lum: list[float] = scenes.get("scene_luminance") or [0.5]
        n = int(cfg["clips_per_scene"]) * len(scenes.get("scene_ids", []))
        clip_ids = [f"clip_{rng.randrange(1 << 32):08x}" for _ in range(max(n, 1))]
        durations_s = [round(rng.uniform(1.0, 5.0), 2) for _ in range(max(n, 1))]
        # 真实产物：GIF 动画，每镜头一帧，帧亮度=真实镜头亮度
        def frame_fn(lv: float):
            def f(x: int, y: int) -> tuple[int, int, int]:
                return (int(30 + 180 * lv * x / 63), int(30 + 120 * (1 - lv) * y / 47), 90)
            return f
        frames: list = [frame_fn(lum[i % len(lum)]) for i in range(max(n, 1))]
        save("clips_preview.gif", artifacts.gif_encode(64, 48, frames, delay_ms=250),
             f"{len(frames)} 帧动画预览（帧亮度=镜头真实亮度）")
        return {"clip_ids": clip_ids, "durations_s": durations_s, "art_files": art_files}
    if ctx.step_type == "generate_audio":
        scenes = ctx.inputs.get("scenes", {})
        count = len(scenes.get("scene_ids", [])) or 1
        lum: list[float] = scenes.get("scene_luminance") or [0.5]
        tracks = [round(rng.uniform(2.0, 10.0), 2) for _ in range(count)]
        # 真实产物：可播放 WAV，音高由镜头亮度映射（亮→高音）。
        # 性能：产物用 16kHz、每段 ≤0.8s、总长 ≤3.2s——纯 Python 合成的合理预算
        art_sr = 16000
        mixed: list[int] = []
        for i in range(count):
            seg = artifacts.tone_samples(seed=rng.randrange(1 << 30), sample_rate=art_sr,
                                          duration_s=min(tracks[i], 0.8),
                                          base_freq=180.0 + 260.0 * lum[i % len(lum)])
            mixed.extend(seg)
        save("audio_track.wav", artifacts.wav_encode(art_sr, mixed[: art_sr * 4]),
             "可播放音轨（音高由真实镜头亮度映射）")
        return {"track_count": count, "track_durations_s": tracks, "sample_rate": cfg["sample_rate"], "art_files": art_files}
    if ctx.step_type == "compose_video":
        frame_count = int(cfg["fps"]) * 2
        clips = ctx.inputs.get("clips", {})
        durs = clips.get("durations_s") or [2.0]
        # 真实产物：合成 GIF——帧数按各 clip 真实时长；颜色预量化到 ≤64 色避免逐像素最近色搜索
        fns: list = []
        for i, d in enumerate(durs[:4]):
            for k in range(max(1, min(int(d * 2), 2))):
                seed = (i + 1) * 131 + k * 7
                def fn(x: int, y: int, s=seed) -> tuple[int, int, int]:
                    r = ((s * 5 + x * 3) % 256) // 64 * 64
                    g = ((s * 7 + y * 5) % 256) // 64 * 64
                    b = ((s * 11 + (x + y)) % 256) // 64 * 64
                    return r, g, b
                fns.append(fn)
        save("composed.gif", artifacts.gif_encode(64, 48, fns[:8], delay_ms=150),
             f"合成预览（{len(fns)} 帧，帧数按各 clip 真实时长）")
        return {"fps": cfg["fps"], "frame_count": frame_count, "codec": "gif-preview",
                "render_seed": rng.randrange(1 << 32), "art_files": art_files}
    if ctx.step_type == "export_result":
        # 真实产物：自包含 HTML 报告，内嵌本任务全部前序产物（base64）
        video = ctx.inputs.get("video", {})
        parts = []
        task_dir = os.path.join(ctx.data_dir, "tasks", ctx.task_id, "steps")
        for st, fname_hint in (("parse_assets", "assets_overview.png"),
                                ("analyze_scenes", "scene_brightness.png"),
                                ("generate_clips", "clips_preview.gif"),
                                ("generate_audio", "audio_track.wav"),
                                ("compose_video", "composed.gif")):
            p = os.path.join(task_dir, st, "attempt_1", fname_hint)
            if os.path.isfile(p):
                b64 = base64.b64encode(open(p, "rb").read()).decode()
                mime = "image/png" if p.endswith(".png") else ("image/gif" if p.endswith(".gif") else "audio/wav")
                if mime == "audio/wav":
                    parts.append(f"<h3>{st}</h3><audio controls src='data:{mime};base64,{b64}'></audio>")
                else:
                    parts.append(f"<h3>{st}</h3><img style='max-width:640px' src='data:{mime};base64,{b64}'>")
        html = f"""<!doctype html><html><head><meta charset='utf-8'><title>RenderFlow 导出报告 {ctx.task_id[:8]}</title>
<style>body{{font-family:-apple-system,'PingFang SC',sans-serif;max-width:720px;margin:24px auto;padding:0 16px;background:#f7f8fa}}h1{{font-size:20px}}h3{{font-size:14px;color:#3563e9;margin:20px 0 6px}}img{{border-radius:8px;border:1px solid #e2e5ea}}</style>
</head><body><h1>RenderFlow 导出报告</h1><p>task {ctx.task_id}<br>workflow export_result · {time.strftime('%Y-%m-%d %H:%M:%S')}</p>
{''.join(parts)}</body></html>"""
        save("export_report.html", html.encode(), "自包含 HTML 导出报告（内嵌全部中间产物）")
        return {"format": "html-report", "size_bytes": len(html), "art_files": art_files}
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
