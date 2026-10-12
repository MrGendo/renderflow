"""状态机：合法迁移表 + 守卫 + summarize_task 统一汇总。

API 与 Worker 共用本模块，禁止在两处复制状态推导逻辑。
所有 DAO 的终态更新 SQL 必须带 TERMINAL_GUARD_SQL，保证终态不可迁出。
"""

from typing import NamedTuple

from .enums import StepStatus, TaskStatus

ALLOWED_TASK_TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
    TaskStatus.PENDING: {TaskStatus.RUNNING, TaskStatus.FAILED, TaskStatus.CANCELLED},
    TaskStatus.RUNNING: {TaskStatus.RETRY_WAIT, TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED},
    TaskStatus.RETRY_WAIT: {TaskStatus.RUNNING, TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED},
    TaskStatus.SUCCEEDED: set(),
    # W2：手动重试是唯一允许的 FAILED 终态撤销路径（DAO 用显式 WHERE status='FAILED' 守卫）
    TaskStatus.FAILED: {TaskStatus.RUNNING},
    TaskStatus.CANCELLED: set(),
}

ALLOWED_STEP_TRANSITIONS: dict[StepStatus, set[StepStatus]] = {
    StepStatus.PENDING: {StepStatus.READY, StepStatus.SKIPPED},
    StepStatus.READY: {StepStatus.RUNNING, StepStatus.SKIPPED},
    # W2：RUNNING→SKIPPED 供取消检查点（executor 主动中止后标 CANCELLED_BY_USER）
    StepStatus.RUNNING: {StepStatus.SUCCEEDED, StepStatus.RETRY_WAIT, StepStatus.FAILED, StepStatus.SKIPPED},
    # W2：RETRY_WAIT→READY 供到期推进；FAILED→READY / SKIPPED→PENDING 供手动重试撤销终态
    StepStatus.RETRY_WAIT: {StepStatus.READY, StepStatus.RUNNING, StepStatus.FAILED, StepStatus.SKIPPED},
    StepStatus.SUCCEEDED: set(),
    StepStatus.FAILED: {StepStatus.READY},
    StepStatus.SKIPPED: {StepStatus.PENDING},
}

TERMINAL_TASK_STATUSES = (TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED)
TERMINAL_STEP_STATUSES = (StepStatus.SUCCEEDED, StepStatus.FAILED, StepStatus.SKIPPED)

# DAO 终态更新 SQL 统一拼接的守卫条件（单一来源，避免各处手抄不一致）
TERMINAL_GUARD_SQL = "status NOT IN ('SUCCEEDED','FAILED','CANCELLED')"


class StateTransitionError(Exception):
    """非法状态迁移。"""


def ensure_transition(table: dict, old: str, new: str, label: str) -> None:
    key = type(next(iter(table)))(old)
    target = type(key)(new)
    if key not in table:
        raise StateTransitionError(f"{label} 未知源状态: {old}")
    if target not in table[key]:
        raise StateTransitionError(f"{label} 不允许迁移 {old} -> {new}")


def ensure_task_transition(old: str, new: str) -> None:
    ensure_transition(ALLOWED_TASK_TRANSITIONS, old, new, "任务")


def ensure_step_transition(old: str, new: str) -> None:
    ensure_transition(ALLOWED_STEP_TRANSITIONS, old, new, "步骤")


class TaskSummary(NamedTuple):
    derived_status: str
    reason_code: str  # PENDING/RUNNING/WAITING_RETRY/WAITING_DEPENDENCIES/FAILED/SUCCEEDED/CANCELLED
    progress_percent: int
    is_terminal: bool


def summarize_task(task_row, step_rows) -> TaskSummary:
    """由任务行 + 步骤行推导任务状态。规则顺序即优先级，见 TASK1.md 第 3 节。"""
    steps = list(step_rows)
    total = len(steps)
    succeeded = sum(1 for s in steps if s["status"] == StepStatus.SUCCEEDED.value)
    # 等权进度：全部步骤计入分母，SKIPPED 不算成功
    progress = round(100 * succeeded / total) if total else 0
    statuses = [s["status"] for s in steps]

    # 1) 已请求取消且无 RUNNING 步骤 → CANCELLED
    if task_row["cancel_requested"] and StepStatus.RUNNING.value not in statuses:
        return TaskSummary(TaskStatus.CANCELLED.value, "CANCELLED", progress, True)

    # 2) 任一步骤 FAILED → 任务 FAILED（error_summary 由调用方从失败步骤取）
    failed = [s for s in steps if s["status"] == StepStatus.FAILED.value]
    if failed:
        return TaskSummary(TaskStatus.FAILED.value, "FAILED", progress, True)

    # 3) 任一步骤 RETRY_WAIT → 任务 RETRY_WAIT
    if StepStatus.RETRY_WAIT.value in statuses:
        return TaskSummary(TaskStatus.RETRY_WAIT.value, "WAITING_RETRY", progress, False)

    # 4) 全部 SUCCEEDED → SUCCEEDED
    if total and all(s == StepStatus.SUCCEEDED.value for s in statuses):
        return TaskSummary(TaskStatus.SUCCEEDED.value, "SUCCEEDED", progress, True)

    # 5) 有 RUNNING → RUNNING；否则有 READY/PENDING → 已开始的记 RUNNING 否则 PENDING
    if StepStatus.RUNNING.value in statuses:
        return TaskSummary(TaskStatus.RUNNING.value, "RUNNING", progress, False)
    if task_row["started_at"] is not None:
        return TaskSummary(TaskStatus.RUNNING.value, "WAITING_DEPENDENCIES", progress, False)
    return TaskSummary(TaskStatus.PENDING.value, "PENDING", progress, False)
