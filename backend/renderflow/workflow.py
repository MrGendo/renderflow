"""固定六步 DAG 定义与步骤规格。

input_hash = sha256(canonical_json(step_type + 归一化输入))；
config_hash = sha256(canonical_json(step_version + config))。
"""

import hashlib
import json
from dataclasses import dataclass, field
from typing import Callable

WORKFLOW_VERSION = "1.0.0"


class WorkflowDefinitionError(Exception):
    """DAG 定义非法（有环或依赖缺失）。"""


def canonical_json(obj) -> str:
    """确定性 JSON 序列化，保证同结构数据哈希稳定。"""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def compute_input_hash(step_type: str, normalized_input: dict) -> str:
    return sha256_hex(canonical_json([step_type, normalized_input]))


def compute_config_hash(step_version: str, config: dict) -> str:
    return sha256_hex(canonical_json([step_version, config]))


def cache_key(step_type: str, input_hash: str, step_version: str, config_hash: str) -> str:
    """缓存键：任一维度变化（输入/版本/配置）即失效。v1 前缀留升级余地。"""
    return sha256_hex("|".join(["v1", step_type, input_hash, step_version, config_hash]))


# build_input：下游步骤的输入只引用上游输出的 mock_payload（确定性部分），
# 不含 task_id/produced_at 等易变字段——否则跨任务的缓存命中（W2）永远不可能成立。


def _parse_assets_input(task_input: dict, upstream: dict) -> dict:
    # demo 配置透传（batch_pause_ms 等演示模式参数，随任务持久化）
    return {"assets": task_input.get("assets", []), "demo": task_input.get("demo") or {}}


def _analyze_scenes_input(task_input: dict, upstream: dict) -> dict:
    return {"parsed_assets": upstream.get("parse_assets", {})}


def _generate_clips_input(task_input: dict, upstream: dict) -> dict:
    return {"scenes": upstream.get("analyze_scenes", {})}


def _generate_audio_input(task_input: dict, upstream: dict) -> dict:
    return {"scenes": upstream.get("analyze_scenes", {})}


def _compose_video_input(task_input: dict, upstream: dict) -> dict:
    return {"clips": upstream.get("generate_clips", {}), "audio": upstream.get("generate_audio", {})}


def _export_result_input(task_input: dict, upstream: dict) -> dict:
    return {"video": upstream.get("compose_video", {})}


@dataclass(frozen=True)
class StepSpec:
    step_type: str
    step_version: str
    dependencies: tuple[str, ...]
    timeout_s: float = 30.0
    max_attempts: int = 3
    retry_backoff_base_s: float = 1.0
    retry_backoff_max_s: float = 30.0
    config: dict = field(default_factory=dict)
    build_input: Callable[[dict, dict], dict] = lambda task_input, upstream: {}


STEPS: list[StepSpec] = [
    StepSpec(
        step_type="parse_assets",
        step_version="1.0.0",
        dependencies=(),
        config={"max_assets": 100},
        build_input=_parse_assets_input,
    ),
    StepSpec(
        step_type="analyze_scenes",
        step_version="1.0.0",
        dependencies=("parse_assets",),
        config={"scene_count": 4},
        build_input=_analyze_scenes_input,
    ),
    StepSpec(
        step_type="generate_clips",
        step_version="1.0.0",
        dependencies=("analyze_scenes",),
        config={"clips_per_scene": 2},
        build_input=_generate_clips_input,
    ),
    StepSpec(
        step_type="generate_audio",
        step_version="1.0.0",
        dependencies=("analyze_scenes",),
        config={"sample_rate": 44100},
        build_input=_generate_audio_input,
    ),
    StepSpec(
        step_type="compose_video",
        step_version="1.0.0",
        dependencies=("generate_clips", "generate_audio"),
        timeout_s=60.0,
        config={"fps": 30},
        build_input=_compose_video_input,
    ),
    StepSpec(
        step_type="export_result",
        step_version="1.0.0",
        dependencies=("compose_video",),
        config={"format": "mp4"},
        build_input=_export_result_input,
    ),
]

STEPS_BY_TYPE: dict[str, StepSpec] = {s.step_type: s for s in STEPS}


def validate_workflow(steps: list[StepSpec] = STEPS) -> None:
    """校验依赖都存在且无环；非法定义直接抛错，启动即失败。"""
    known = {s.step_type for s in steps}
    for spec in steps:
        for dep in spec.dependencies:
            if dep not in known:
                raise WorkflowDefinitionError(f"步骤 {spec.step_type} 依赖了不存在的步骤 {dep}")
    # Kahn 拓扑排序：排不完即存在环
    indegree = {s.step_type: len(s.dependencies) for s in steps}
    ready = [t for t, d in indegree.items() if d == 0]
    seen = 0
    while ready:
        node = ready.pop()
        seen += 1
        for spec in steps:
            if node in spec.dependencies:
                indegree[spec.step_type] -= 1
                if indegree[spec.step_type] == 0:
                    ready.append(spec.step_type)
    if seen != len(steps):
        raise WorkflowDefinitionError("工作流定义存在环")


validate_workflow()  # 导入即校验，坏定义在启动第一时间暴露


def get_step_spec(step_type: str) -> StepSpec:
    return STEPS_BY_TYPE[step_type]


def build_step_input(step_type: str, task_input: dict, upstream_outputs: dict) -> dict:
    """upstream_outputs: {step_type: 上游 output.json 的 mock_payload}。"""
    return get_step_spec(step_type).build_input(task_input, upstream_outputs)
