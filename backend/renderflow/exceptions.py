"""控制流异常：Worker 与 DAO 之间的租约/取消协作信号。

这些异常不在 executor 兜底 except 中吞掉，必须上抛给 Worker 分流处理。
"""

from .enums import ErrorCode


class LeaseLostError(Exception):
    """fencing 拒绝：提交时 worker_id/lease_version 与 DB 不匹配。

    旧 Worker 必须立即放弃该步骤，且不得回滚新持有者已写入的任何状态。
    """


class StepCancelledError(Exception):
    """任务已请求取消：executor 检查点主动中止当前分片执行。"""


class DaoConflictError(Exception):
    """DAO 层业务冲突（状态不允许/依赖不满足/任务已终态），API 层转 409。"""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


# 内部错误对用户可见的错误码复用 ErrorCode，避免字符串散落
INTERNAL_ERROR = ErrorCode.INTERNAL_ERROR.value
