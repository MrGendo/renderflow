"""重试引擎：错误分类 → 可重试映射 + 指数退避（带抖动）计算。

Worker 失败路径统一调用本模块判定，别处不得内联重试规则。
"""

import random

from .enums import ErrorCode

# 可重试错误分类：瞬时/超时/资源不可用/Worker 中断（回收器补记）
RETRYABLE_CODES = frozenset({
    ErrorCode.TRANSIENT_ERROR.value,
    ErrorCode.TIMEOUT.value,
    ErrorCode.RESOURCE_UNAVAILABLE.value,
    ErrorCode.WORKER_INTERRUPTED.value,
})

# 不可重试：输入非法/产出非法/内部错误（INTERNAL_ERROR 保守视为不可重试，见 README）
NON_RETRYABLE_CODES = frozenset({
    ErrorCode.INVALID_INPUT.value,
    ErrorCode.OUTPUT_INVALID.value,
    ErrorCode.INTERNAL_ERROR.value,
})


def is_retryable(error_code: str) -> bool:
    return error_code in RETRYABLE_CODES


def backoff_seconds(attempt_count: int, base_s: float, max_s: float) -> float:
    """指数退避 + 抖动：min(base * 2^(attempt-1), max) + uniform(0, base*0.3)。

    attempt_count 为已失败次数（第 1 次失败后退避约 base）。
    """
    capped = min(base_s * (2 ** (max(attempt_count - 1, 0))), max_s)
    return capped + random.uniform(0.0, base_s * 0.3)
