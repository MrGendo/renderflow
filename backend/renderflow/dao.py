"""数据访问层：全部 SQL（含原子领取）都集中在此，别处不得手写业务 SQL。"""

import json
import time
import uuid

from . import events, workflow
from .db import run_tx
from .enums import ErrorCode, StepStatus, TaskStatus
from .state_machine import TERMINAL_GUARD_SQL, ensure_step_transition, summarize_task

_STEP_NOT_FOUND = object()


def utcnow() -> float:
    return time.time()


def new_id() -> str:
    return uuid.uuid4().hex


# ---------------------------------------------------------------- 查询


def get_task(conn, task_id: str):
    return conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()


def get_step(conn, step_id: str):
    return conn.execute("SELECT * FROM task_steps WHERE step_id = ?", (step_id,)).fetchone()


def list_steps(conn, task_id: str):
    return conn.execute(
        "SELECT * FROM task_steps WHERE task_id = ? ORDER BY created_at", (task_id,)
    ).fetchall()


def list_attempts(conn, step_id: str):
    return conn.execute(
        "SELECT * FROM step_attempts WHERE step_id = ? ORDER BY attempt_no", (step_id,)
    ).fetchall()


def list_events(conn, task_id: str, limit: int | None = None):
    sql = "SELECT * FROM task_events WHERE task_id = ? ORDER BY event_id"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql, (task_id,)).fetchall()


def list_tasks(conn, page: int, page_size: int, status: str | None = None):
    """分页 + 可选状态筛选，返回 (total, 本页行)，按创建时间倒序。"""
    where, params = "", []
    if status:
        where = " WHERE status = ?"
        params.append(status)
    total = conn.execute(f"SELECT COUNT(*) AS c FROM tasks{where}", params).fetchone()["c"]
    rows = conn.execute(
        f"SELECT * FROM tasks{where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (*params, page_size, (page - 1) * page_size),
    ).fetchall()
    return total, rows


def task_input_of(task_row) -> tuple[dict, dict]:
    """tasks.input_json 存信封 {"input": 业务输入, "step_durations": 覆盖表}。"""
    payload = json.loads(task_row["input_json"])
    return payload.get("input", {}), payload.get("step_durations") or {}


def get_task_summary(conn, task_id: str):
    return summarize_task(get_task(conn, task_id), list_steps(conn, task_id))


# ---------------------------------------------------------------- 创建


def create_task(conn, task_input: dict, step_durations: dict, workflow_version: str, log_dir: str | None = None) -> str:
    """创建任务行(PENDING) + 6 条步骤行(PENDING) + TASK_CREATED 事件，单事务。"""
    task_id = new_id()
    input_json = json.dumps(
        {"input": task_input, "step_durations": step_durations}, ensure_ascii=False, sort_keys=True
    )

    def _fn(c):
        now = utcnow()
        c.execute(
            "INSERT INTO tasks (task_id, status, input_json, created_at, updated_at, workflow_version)"
            " VALUES (?,?,?,?,?,?)",
            (task_id, TaskStatus.PENDING.value, input_json, now, now, workflow_version),
        )
        for i, spec in enumerate(workflow.STEPS):
            # 创建时上游尚无产出，input_hash 用空上游构建的初始值；领取时重算真实值
            initial_input = workflow.build_step_input(spec.step_type, task_input, {})
            c.execute(
                "INSERT INTO task_steps (step_id, task_id, step_type, status, dependencies, input_hash,"
                " step_version, config_hash, max_attempts, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    new_id(), task_id, spec.step_type, StepStatus.PENDING.value,
                    json.dumps(list(spec.dependencies)), workflow.compute_input_hash(spec.step_type, initial_input),
                    spec.step_version, workflow.compute_config_hash(spec.step_version, spec.config),
                    spec.max_attempts, now + i * 1e-6,  # 微调保证领取顺序稳定
                ),
            )
        events.insert_event(
            c, task_id, "TASK_CREATED", status=TaskStatus.PENDING.value,
            detail={"workflow_version": workflow_version, "step_count": len(workflow.STEPS)},
            log_dir=log_dir, created_at=now,
        )
        return task_id

    return run_tx(conn, _fn)


# ---------------------------------------------------------------- 领取


def _promote_ready_steps(conn, task_id: str, now: float) -> int:
    """把依赖全 SUCCEEDED 的 PENDING 步骤推进为 READY（须在事务内调用）。"""
    promoted = 0
    for step in conn.execute(
        "SELECT step_id, dependencies FROM task_steps WHERE task_id = ? AND status = ?",
        (task_id, StepStatus.PENDING.value),
    ).fetchall():
        deps = json.loads(step["dependencies"])
        if not deps:
            ok = True
        else:
            placeholders = ",".join("?" * len(deps))
            done = conn.execute(
                f"SELECT COUNT(*) AS c FROM task_steps WHERE task_id = ? AND step_type IN ({placeholders})"
                f" AND status = ?",
                (task_id, *deps, StepStatus.SUCCEEDED.value),
            ).fetchone()["c"]
            ok = done == len(deps)  # 严格验证所有依赖都是 SUCCEEDED
        if ok:
            ensure_step_transition(StepStatus.PENDING.value, StepStatus.READY.value)
            conn.execute(
                f"UPDATE task_steps SET status = ? WHERE step_id = ? AND status = ?",
                (StepStatus.READY.value, step["step_id"], StepStatus.PENDING.value),
            )
            promoted += 1
    return promoted


def claim_next_step(conn, worker_id: str, lease_seconds: float, log_dir: str | None = None):
    """原子领取：单事务推进 READY → SELECT → UPDATE(RUNNING/租约) → INSERT attempt。

    返回领取信息 dict；无可领步骤返回 None。
    """
    def _fn(c):
        # 先挑最早的 READY 步骤所属任务做依赖推进，避免全表扫 PENDING
        ready = c.execute(
            "SELECT task_id FROM task_steps WHERE status = ? ORDER BY created_at LIMIT 1",
            (StepStatus.READY.value,),
        ).fetchone()
        if ready:
            _promote_ready_steps(c, ready["task_id"], utcnow())
        row = c.execute(
            "SELECT * FROM task_steps WHERE status = ? ORDER BY created_at LIMIT 1",
            (StepStatus.READY.value,),
        ).fetchone()
        if row is None:
            # 无 READY：对全部含 PENDING 的任务做依赖推进（首个无依赖步骤由此进入 READY）
            for pending in c.execute(
                "SELECT DISTINCT task_id FROM task_steps WHERE status = ? ORDER BY task_id",
                (StepStatus.PENDING.value,),
            ).fetchall():
                _promote_ready_steps(c, pending["task_id"], utcnow())
            row = c.execute(
                "SELECT * FROM task_steps WHERE status = ? ORDER BY created_at LIMIT 1",
                (StepStatus.READY.value,),
            ).fetchone()
            if row is None:
                return None

        now = utcnow()
        attempt_no = row["attempt_count"] + 1
        attempt_id = new_id()
        lease_version = row["lease_version"] + 1
        cur = c.execute(
            "UPDATE task_steps SET status = ?, worker_id = ?, lease_until = ?, lease_version = ?,"
            " heartbeat_at = ?, started_at = COALESCE(started_at, ?), attempt_count = ?,"
            " error_code = NULL, error_message = NULL"
            f" WHERE step_id = ? AND status = ? AND {TERMINAL_GUARD_SQL}",
            (
                StepStatus.RUNNING.value, worker_id, now + lease_seconds, lease_version,
                now, now, attempt_no, row["step_id"], StepStatus.READY.value,
            ),
        )
        if cur.rowcount == 0:
            return None  # 并发被抢，放弃本轮
        c.execute(
            "INSERT INTO step_attempts (attempt_id, step_id, attempt_no, worker_id, status, started_at)"
            " VALUES (?,?,?,?,?,?)",
            (attempt_id, row["step_id"], attempt_no, worker_id, StepStatus.RUNNING.value, now),
        )
        for event_type in ("STEP_CLAIMED", "STEP_STARTED"):
            events.insert_event(
                c, row["task_id"], event_type, step_id=row["step_id"], attempt_id=attempt_id,
                worker_id=worker_id, status=StepStatus.RUNNING.value,
                detail={"attempt_no": attempt_no, "lease_version": lease_version},
                log_dir=log_dir, created_at=now,
            )
        # 任务首次领取：PENDING → RUNNING（带终态守卫）
        c.execute(
            f"UPDATE tasks SET status = ?, started_at = COALESCE(started_at, ?), updated_at = ?"
            f" WHERE task_id = ? AND status = ? AND {TERMINAL_GUARD_SQL}",
            (TaskStatus.RUNNING.value, now, now, row["task_id"], TaskStatus.PENDING.value),
        )
        return dict(row) | {
            "attempt_no": attempt_no, "attempt_id": attempt_id, "lease_version": lease_version,
        }

    return run_tx(conn, _fn)


def touch_lease(conn, step_id: str, worker_id: str, lease_version: int, lease_seconds: float) -> bool:
    """心跳续租：仅当前持有者且 lease_version 匹配才生效（W2 fencing 基础）。"""
    now = utcnow()

    def _fn(c):
        cur = c.execute(
            "UPDATE task_steps SET heartbeat_at = ?, lease_until = ?"
            " WHERE step_id = ? AND worker_id = ? AND lease_version = ? AND status = ?",
            (now, now + lease_seconds, step_id, worker_id, lease_version, StepStatus.RUNNING.value),
        )
        return cur.rowcount > 0

    return run_tx(conn, _fn)


def update_input_hash(conn, step_id: str, input_hash: str) -> None:
    """领取后按真实上游产出重算的 input_hash 回写（创建时存的是初始值）。"""
    run_tx(conn, lambda c: c.execute(
        "UPDATE task_steps SET input_hash = ? WHERE step_id = ?", (input_hash, step_id)
    ))


# ---------------------------------------------------------------- 提交/失败


def _refresh_task_status(conn, task_id: str, now: float, log_dir: str | None) -> None:
    """步骤状态变化后重新 summarize 并同步 tasks.status（带终态守卫 + 状态机校验）。"""
    task = get_task(conn, task_id)
    summary = summarize_task(task, list_steps(conn, task_id))
    if summary.derived_status == task["status"]:
        conn.execute("UPDATE tasks SET updated_at = ? WHERE task_id = ?", (now, task_id))
        return
    from .state_machine import ensure_task_transition  # 局部导入避免循环依赖
    ensure_task_transition(task["status"], summary.derived_status)
    finished_at = now if summary.is_terminal else None
    error_summary = None
    if summary.derived_status == TaskStatus.FAILED.value:
        bad = conn.execute(
            "SELECT error_code, error_message FROM task_steps WHERE task_id = ? AND status = ?"
            " ORDER BY finished_at LIMIT 1",
            (task_id, StepStatus.FAILED.value),
        ).fetchone()
        if bad:
            error_summary = f"{bad['error_code']}: {bad['error_message']}"
    conn.execute(
        f"UPDATE tasks SET status = ?, updated_at = ?,"
        f" finished_at = COALESCE(finished_at, ?), error_summary = ?"
        f" WHERE task_id = ? AND {TERMINAL_GUARD_SQL}",
        (summary.derived_status, now, finished_at, error_summary, task_id),
    )
    events.insert_event(
        conn, task_id, f"TASK_{summary.derived_status}", status=summary.derived_status,
        error_code=ErrorCode.INTERNAL_ERROR.value if error_summary else None,
        detail={
            "reason_code": summary.reason_code,
            "progress_percent": summary.progress_percent,
            "error_summary": error_summary,
        },
        log_dir=log_dir, created_at=now,
    )


def complete_step_success(
    conn, *, step_id: str, attempt_id: str, worker_id: str, lease_version: int,
    output_ref: str, output_checksum: str, log_ref: str | None, log_dir: str | None = None,
) -> bool:
    """成功提交：守卫 WHERE lease_version=? AND status='RUNNING'，再刷新任务汇总。"""
    def _fn(c):
        now = utcnow()
        attempt = c.execute("SELECT * FROM step_attempts WHERE attempt_id = ?", (attempt_id,)).fetchone()
        duration_ms = int((now - attempt["started_at"]) * 1000) if attempt["started_at"] else None
        cur = c.execute(
            "UPDATE task_steps SET status = ?, output_ref = ?, output_checksum = ?, finished_at = ?,"
            " heartbeat_at = NULL, lease_until = NULL"
            " WHERE step_id = ? AND lease_version = ? AND status = ?",
            (
                StepStatus.SUCCEEDED.value, output_ref, output_checksum, now,
                step_id, lease_version, StepStatus.RUNNING.value,
            ),
        )
        if cur.rowcount == 0:
            return False  # 租约被夺或状态已变，拒绝提交（W2 完整 fencing 处理）
        c.execute(
            "UPDATE step_attempts SET status = ?, finished_at = ?, duration_ms = ?, output_ref = ?, log_ref = ?"
            " WHERE attempt_id = ?",
            (StepStatus.SUCCEEDED.value, now, duration_ms, output_ref, log_ref, attempt_id),
        )
        step = get_step(c, step_id)
        events.insert_event(
            c, step["task_id"], "STEP_SUCCEEDED", step_id=step_id, attempt_id=attempt_id,
            worker_id=worker_id, status=StepStatus.SUCCEEDED.value, duration_ms=duration_ms,
            detail={"output_ref": output_ref, "output_checksum": output_checksum},
            log_dir=log_dir, created_at=now,
        )
        _refresh_task_status(c, step["task_id"], now, log_dir)
        return True

    return run_tx(conn, _fn)


def fail_step(
    conn, *, step_id: str, attempt_id: str, worker_id: str, lease_version: int,
    error_code: str, error_message: str, log_ref: str | None, log_dir: str | None = None,
) -> bool:
    """W1 失败路径：executor 异常统一 INTERNAL_ERROR，直接置 FAILED（不重试）。"""
    def _fn(c):
        now = utcnow()
        attempt = c.execute("SELECT * FROM step_attempts WHERE attempt_id = ?", (attempt_id,)).fetchone()
        duration_ms = int((now - attempt["started_at"]) * 1000) if attempt["started_at"] else None
        cur = c.execute(
            "UPDATE task_steps SET status = ?, finished_at = ?, error_code = ?, error_message = ?,"
            " heartbeat_at = NULL, lease_until = NULL"
            " WHERE step_id = ? AND lease_version = ? AND status = ?",
            (
                StepStatus.FAILED.value, now, error_code, error_message,
                step_id, lease_version, StepStatus.RUNNING.value,
            ),
        )
        if cur.rowcount == 0:
            return False
        c.execute(
            "UPDATE step_attempts SET status = ?, finished_at = ?, duration_ms = ?,"
            " error_code = ?, error_message = ?, log_ref = ? WHERE attempt_id = ?",
            (StepStatus.FAILED.value, now, duration_ms, error_code, error_message, log_ref, attempt_id),
        )
        step = get_step(c, step_id)
        events.insert_event(
            c, step["task_id"], "STEP_FAILED", step_id=step_id, attempt_id=attempt_id,
            worker_id=worker_id, status=StepStatus.FAILED.value, duration_ms=duration_ms,
            error_code=error_code, detail={"message": error_message}, log_dir=log_dir, created_at=now,
        )
        _refresh_task_status(c, step["task_id"], now, log_dir)
        return True

    return run_tx(conn, _fn)
