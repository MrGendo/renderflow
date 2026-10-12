"""数据访问层：全部 SQL（含原子领取）都集中在此，别处不得手写业务 SQL。"""

import json
import time
import uuid

from . import events, retry, workflow
from .db import run_tx
from .enums import ErrorCode, StepStatus, TaskStatus
from .exceptions import DaoConflictError, LeaseLostError
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


def task_faults_of(task_row) -> dict:
    """读取信封里的 fault_injection（随任务持久化，Worker 重启后仍生效）。"""
    return json.loads(task_row["input_json"]).get("fault_injection") or {}


def get_task_summary(conn, task_id: str):
    return summarize_task(get_task(conn, task_id), list_steps(conn, task_id))


# ---------------------------------------------------------------- 创建


def create_task(
    conn, task_input: dict, step_durations: dict, workflow_version: str, log_dir: str | None = None,
    fault_injection: dict | None = None,
) -> str:
    """创建任务行(PENDING) + 6 条步骤行(PENDING) + TASK_CREATED 事件，单事务。

    fault_injection 已在 API 层通过生效门校验才会传入（存信封随任务持久化）。
    """
    task_id = new_id()
    faults = fault_injection or {}
    input_json = json.dumps(
        {"input": task_input, "step_durations": step_durations, "fault_injection": faults},
        ensure_ascii=False, sort_keys=True,
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
            # 故障注入可按步覆盖 max_attempts（演示重试上限用，见 TASK2 第 4 节）
            max_attempts = int(faults.get(spec.step_type, {}).get("max_attempts", spec.max_attempts))
            c.execute(
                "INSERT INTO task_steps (step_id, task_id, step_type, status, dependencies, input_hash,"
                " step_version, config_hash, max_attempts, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    new_id(), task_id, spec.step_type, StepStatus.PENDING.value,
                    json.dumps(list(spec.dependencies)), workflow.compute_input_hash(spec.step_type, initial_input),
                    spec.step_version, workflow.compute_config_hash(spec.step_version, spec.config),
                    max_attempts, now + i * 1e-6,  # 微调保证领取顺序稳定
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


def _deps_satisfied(conn, task_id: str, deps: list[str]) -> bool:
    """严格验证所有依赖步骤都是 SUCCEEDED（依赖恢复/重评估共用）。"""
    if not deps:
        return True
    placeholders = ",".join("?" * len(deps))
    done = conn.execute(
        f"SELECT COUNT(*) AS c FROM task_steps WHERE task_id = ? AND step_type IN ({placeholders})"
        f" AND status = ?",
        (task_id, *deps, StepStatus.SUCCEEDED.value),
    ).fetchone()["c"]
    return done == len(deps)


def _promote_ready_steps(conn, task_id: str, now: float) -> int:
    """把依赖全 SUCCEEDED 的 PENDING 步骤推进为 READY（须在事务内调用）。"""
    promoted = 0
    for step in conn.execute(
        "SELECT step_id, dependencies FROM task_steps WHERE task_id = ? AND status = ?",
        (task_id, StepStatus.PENDING.value),
    ).fetchall():
        if _deps_satisfied(conn, task_id, json.loads(step["dependencies"])):
            ensure_step_transition(StepStatus.PENDING.value, StepStatus.READY.value)
            conn.execute(
                f"UPDATE task_steps SET status = ? WHERE step_id = ? AND status = ?",
                (StepStatus.READY.value, step["step_id"], StepStatus.PENDING.value),
            )
            promoted += 1
    return promoted


def _promote_retry_wait_steps(conn, task_id: str, now: float, log_dir: str | None = None) -> int:
    """把到期（next_retry_at <= now）且依赖仍满足的 RETRY_WAIT 步骤推进为 READY。

    不能仅凭到期就推进：上游若因手动重试等回到非 SUCCEEDED，依赖必须重新满足。
    """
    promoted = 0
    for step in conn.execute(
        "SELECT step_id, dependencies, attempt_count, next_retry_at FROM task_steps"
        " WHERE task_id = ? AND status = ?",
        (task_id, StepStatus.RETRY_WAIT.value),
    ).fetchall():
        if step["next_retry_at"] is not None and step["next_retry_at"] > now:
            continue
        if not _deps_satisfied(conn, task_id, json.loads(step["dependencies"])):
            continue
        ensure_step_transition(StepStatus.RETRY_WAIT.value, StepStatus.READY.value)
        conn.execute(
            "UPDATE task_steps SET status = ?, next_retry_at = NULL WHERE step_id = ? AND status = ?",
            (StepStatus.READY.value, step["step_id"], StepStatus.RETRY_WAIT.value),
        )
        events.insert_event(
            conn, task_id, "STEP_READY", step_id=step["step_id"],
            status=StepStatus.READY.value, detail={"from": "RETRY_WAIT", "attempt_count": step["attempt_count"]},
            log_dir=log_dir, created_at=now,
        )
        promoted += 1
    return promoted


def _next_attempt_no(conn, step_id: str, attempt_count: int) -> int:
    """下一个 attempt 编号：手动重试会把 attempt_count 归零但历史行保留，
    编号必须继续递增（取两者较大者 +1），避免撞 UNIQUE(step_id, attempt_no)。"""
    max_no = conn.execute(
        "SELECT MAX(attempt_no) AS m FROM step_attempts WHERE step_id = ?", (step_id,)
    ).fetchone()["m"] or 0
    return max(attempt_count, max_no) + 1


def claim_next_step(conn, worker_id: str, lease_seconds: float, log_dir: str | None = None):
    """原子领取：单事务推进 READY → SELECT → UPDATE(RUNNING/租约) → INSERT attempt。

    返回领取信息 dict；无可领步骤返回 None。
    """
    def _fn(c):
        # 到期重试先行：RETRY_WAIT → READY（事件 STEP_READY），与领取同事务保证不留中间窗口
        for waiting in c.execute(
            "SELECT DISTINCT task_id FROM task_steps WHERE status = ? ORDER BY task_id",
            (StepStatus.RETRY_WAIT.value,),
        ).fetchall():
            _promote_retry_wait_steps(c, waiting["task_id"], utcnow(), log_dir=log_dir)
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
        attempt_no = _next_attempt_no(c, row["step_id"], row["attempt_count"])
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
    """心跳续租：仅当前持有者且 lease_version 匹配才生效。

    不匹配（租约被回收器/新 Worker 夺走）→ 抛 LeaseLostError，调用方须立即中止执行。
    """
    now = utcnow()

    def _fn(c):
        cur = c.execute(
            "UPDATE task_steps SET heartbeat_at = ?, lease_until = ?"
            " WHERE step_id = ? AND worker_id = ? AND lease_version = ? AND status = ?",
            (now, now + lease_seconds, step_id, worker_id, lease_version, StepStatus.RUNNING.value),
        )
        if cur.rowcount == 0:
            raise LeaseLostError(f"续租被拒: step={step_id} worker={worker_id} lease_version={lease_version}")
        return True

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
            " WHERE step_id = ? AND worker_id = ? AND lease_version = ? AND status = ?",
            (
                StepStatus.SUCCEEDED.value, output_ref, output_checksum, now,
                step_id, worker_id, lease_version, StepStatus.RUNNING.value,
            ),
        )
        if cur.rowcount == 0:
            # fencing 拒绝：worker_id 或 lease_version 不匹配，结果属于旧持有者
            raise LeaseLostError(
                f"提交被 fencing 拒绝: step={step_id} worker={worker_id} lease_version={lease_version}"
            )
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
    backoff_base_s: float = 1.0, backoff_max_s: float = 30.0,
) -> str:
    """W2 失败处理流（重试引擎在 dao 层闭环，Worker 只传错误分类）：

    1. attempt 置 FAILED（error_code/message/duration）
    2. 可重试且 attempt_count < max_attempts → step RETRY_WAIT + next_retry_at + STEP_RETRY_SCHEDULED
    3. 否则 step FAILED + 下游递归 SKIPPED(BLOCKED_BY_UPSTREAM) + 任务 FAILED/finished_at
    返回 "RETRY_WAIT" 或 "FAILED"；worker_id/lease_version 不匹配 → LeaseLostError。
    """
    def _fn(c):
        now = utcnow()
        step = get_step(c, step_id)
        if (
            step is None or step["status"] != StepStatus.RUNNING.value
            or step["worker_id"] != worker_id or step["lease_version"] != lease_version
        ):
            raise LeaseLostError(
                f"失败提交被 fencing 拒绝: step={step_id} worker={worker_id} lease_version={lease_version}"
            )
        attempt = c.execute("SELECT * FROM step_attempts WHERE attempt_id = ?", (attempt_id,)).fetchone()
        duration_ms = int((now - attempt["started_at"]) * 1000) if attempt["started_at"] else None
        c.execute(
            "UPDATE step_attempts SET status = ?, finished_at = ?, duration_ms = ?,"
            " error_code = ?, error_message = ?, log_ref = ? WHERE attempt_id = ?",
            (StepStatus.FAILED.value, now, duration_ms, error_code, error_message, log_ref, attempt_id),
        )

        if retry.is_retryable(error_code) and step["attempt_count"] < step["max_attempts"]:
            ensure_step_transition(StepStatus.RUNNING.value, StepStatus.RETRY_WAIT.value)
            backoff = retry.backoff_seconds(step["attempt_count"], backoff_base_s, backoff_max_s)
            c.execute(
                "UPDATE task_steps SET status = ?, next_retry_at = ?, error_code = ?, error_message = ?,"
                " heartbeat_at = NULL, lease_until = NULL"
                " WHERE step_id = ? AND worker_id = ? AND lease_version = ? AND status = ?",
                (
                    StepStatus.RETRY_WAIT.value, now + backoff, error_code, error_message,
                    step_id, worker_id, lease_version, StepStatus.RUNNING.value,
                ),
            )
            events.insert_event(
                c, step["task_id"], "STEP_RETRY_SCHEDULED", step_id=step_id, attempt_id=attempt_id,
                worker_id=worker_id, status=StepStatus.RETRY_WAIT.value, duration_ms=duration_ms,
                error_code=error_code,
                detail={"attempt_no": attempt["attempt_no"], "backoff_s": round(backoff, 4),
                        "next_retry_at": now + backoff, "message": error_message},
                log_dir=log_dir, created_at=now,
            )
            _refresh_task_status(c, step["task_id"], now, log_dir)
            return "RETRY_WAIT"

        ensure_step_transition(StepStatus.RUNNING.value, StepStatus.FAILED.value)
        c.execute(
            "UPDATE task_steps SET status = ?, finished_at = ?, error_code = ?, error_message = ?,"
            " heartbeat_at = NULL, lease_until = NULL"
            " WHERE step_id = ? AND worker_id = ? AND lease_version = ? AND status = ?",
            (
                StepStatus.FAILED.value, now, error_code, error_message,
                step_id, worker_id, lease_version, StepStatus.RUNNING.value,
            ),
        )
        events.insert_event(
            c, step["task_id"], "STEP_FAILED", step_id=step_id, attempt_id=attempt_id,
            worker_id=worker_id, status=StepStatus.FAILED.value, duration_ms=duration_ms,
            error_code=error_code, detail={"message": error_message}, log_dir=log_dir, created_at=now,
        )
        _skip_downstream(c, step["task_id"], step["step_type"], "BLOCKED_BY_UPSTREAM", now, log_dir)
        _refresh_task_status(c, step["task_id"], now, log_dir)
        return "FAILED"

    return run_tx(conn, _fn)


def _downstream_bfs(conn, task_id: str, root_step_type: str) -> list:
    """沿依赖边收集 root 的全部传递下游步骤行（不含 root 自身）。"""
    steps = conn.execute(
        "SELECT step_id, step_type, status, dependencies FROM task_steps WHERE task_id = ?", (task_id,)
    ).fetchall()
    affected, queue = [], [root_step_type]
    seen = {root_step_type}  # task 内 step_type 唯一，按类型去重即可
    while queue:
        node = queue.pop()
        for row in steps:
            deps = json.loads(row["dependencies"])
            if node in deps and row["step_type"] not in seen:
                seen.add(row["step_type"])
                affected.append(row)
                queue.append(row["step_type"])
    return affected


def _skip_downstream(conn, task_id: str, failed_step_type: str, error_code: str, now: float, log_dir: str | None) -> None:
    """上游失败后递归把未终态下游标 SKIPPED，避免任务永久 RUNNING。"""
    for row in _downstream_bfs(conn, task_id, failed_step_type):
        if row["status"] in (StepStatus.SUCCEEDED.value, StepStatus.FAILED.value, StepStatus.SKIPPED.value):
            continue  # 终态不覆盖（例如并行分支已成功）
        ensure_step_transition(row["status"], StepStatus.SKIPPED.value)
        conn.execute(
            "UPDATE task_steps SET status = ?, error_code = ?, error_message = NULL, finished_at = ?,"
            " next_retry_at = NULL WHERE step_id = ? AND status = ?",
            (StepStatus.SKIPPED.value, error_code, now, row["step_id"], row["status"]),
        )
        events.insert_event(
            conn, task_id, "STEP_SKIPPED", step_id=row["step_id"],
            status=StepStatus.SKIPPED.value, error_code=error_code,
            detail={"blocked_by": failed_step_type}, log_dir=log_dir, created_at=now,
        )


def _reset_skipped_downstream(conn, task_id: str, retried_step_type: str, now: float, log_dir: str | None) -> list[str]:
    """手动重试成功重置后，把下游 SKIPPED 步骤放回 PENDING 重新评估。"""
    reset = []
    for row in _downstream_bfs(conn, task_id, retried_step_type):
        if row["status"] != StepStatus.SKIPPED.value:
            continue
        ensure_step_transition(StepStatus.SKIPPED.value, StepStatus.PENDING.value)
        conn.execute(
            "UPDATE task_steps SET status = ?, error_code = NULL, finished_at = NULL,"
            " next_retry_at = NULL WHERE step_id = ? AND status = ?",
            (StepStatus.PENDING.value, row["step_id"], StepStatus.SKIPPED.value),
        )
        events.insert_event(
            conn, task_id, "STEP_RESET_PENDING", step_id=row["step_id"], status=StepStatus.PENDING.value,
            detail={"resumed_by": retried_step_type}, log_dir=log_dir, created_at=now,
        )
        reset.append(row["step_type"])
    return reset


# ---------------------------------------------------------------- 租约过期回收


def recover_expired_leases(conn, worker_id: str, lease_seconds: float, log_dir: str | None = None):
    """回收一个真正过期（lease_until < now）的 RUNNING 步骤并重新原子领取。

    - 不能仅凭一次心跳缺失判死：租约即宽限期，只有 lease_until 过期才回收
    - 前一 attempt 由回收器补记 WORKER_INTERRUPTED（Worker 崩溃时自己没机会写）
    - UPDATE 带 lease_until < now 守卫：两个 Worker 同时回收只有一方成功
    返回与 claim_next_step 同构的领取信息；无可回收返回 None。
    """
    def _fn(c):
        now = utcnow()
        row = c.execute(
            "SELECT * FROM task_steps WHERE status = ? AND lease_until IS NOT NULL AND lease_until < ?"
            " ORDER BY created_at LIMIT 1",
            (StepStatus.RUNNING.value, now),
        ).fetchone()
        if row is None:
            return None
        prev = c.execute(
            "SELECT * FROM step_attempts WHERE step_id = ? AND status = ? ORDER BY attempt_no DESC LIMIT 1",
            (row["step_id"], StepStatus.RUNNING.value),
        ).fetchone()
        if prev is not None:
            duration_ms = int((now - prev["started_at"]) * 1000) if prev["started_at"] else None
            c.execute(
                "UPDATE step_attempts SET status = ?, finished_at = ?, duration_ms = ?,"
                " error_code = ?, error_message = ? WHERE attempt_id = ?",
                (
                    StepStatus.FAILED.value, now, duration_ms,
                    ErrorCode.WORKER_INTERRUPTED.value,
                    f"租约过期被回收: 原执行者 {row['worker_id']}", prev["attempt_id"],
                ),
            )
        attempt_no = _next_attempt_no(c, row["step_id"], row["attempt_count"])
        attempt_id = new_id()
        lease_version = row["lease_version"] + 1
        cur = c.execute(
            "UPDATE task_steps SET worker_id = ?, lease_until = ?, lease_version = ?, heartbeat_at = ?,"
            " attempt_count = ? WHERE step_id = ? AND status = ? AND lease_until < ?",
            (
                worker_id, now + lease_seconds, lease_version, now, attempt_no,
                row["step_id"], StepStatus.RUNNING.value, now,
            ),
        )
        if cur.rowcount == 0:
            return None  # 并发回收被抢，放弃
        c.execute(
            "INSERT INTO step_attempts (attempt_id, step_id, attempt_no, worker_id, status, started_at)"
            " VALUES (?,?,?,?,?,?)",
            (attempt_id, row["step_id"], attempt_no, worker_id, StepStatus.RUNNING.value, now),
        )
        events.insert_event(
            c, row["task_id"], "LEASE_EXPIRED", step_id=row["step_id"], attempt_id=prev["attempt_id"] if prev else None,
            worker_id=row["worker_id"], error_code=ErrorCode.WORKER_INTERRUPTED.value,
            detail={"expired_lease_until": row["lease_until"], "recovered_by": worker_id},
            log_dir=log_dir, created_at=now,
        )
        events.insert_event(
            c, row["task_id"], "STEP_RECOVERED", step_id=row["step_id"], attempt_id=attempt_id,
            worker_id=worker_id, status=StepStatus.RUNNING.value,
            detail={"attempt_no": attempt_no, "lease_version": lease_version},
            log_dir=log_dir, created_at=now,
        )
        # 兜底：手工构造的失联场景任务可能仍停在 PENDING
        c.execute(
            f"UPDATE tasks SET status = ?, started_at = COALESCE(started_at, ?), updated_at = ?"
            f" WHERE task_id = ? AND status = ? AND {TERMINAL_GUARD_SQL}",
            (TaskStatus.RUNNING.value, now, now, row["task_id"], TaskStatus.PENDING.value),
        )
        return dict(row) | {
            "attempt_no": attempt_no, "attempt_id": attempt_id, "lease_version": lease_version,
        }

    return run_tx(conn, _fn)


# ---------------------------------------------------------------- 结果缓存


def cache_lookup(conn, data_dir: str, cache_key: str, log_dir: str | None = None) -> dict | None:
    """查缓存：命中必须同时满足未过期 + 输出文件存在 + sha256 匹配。

    过期/损坏的缓存行就地删除（惰性清理），绝不返回不可信结果。
    """
    def _fn(c):
        row = c.execute("SELECT * FROM step_cache WHERE cache_key = ?", (cache_key,)).fetchone()
        if row is None:
            return None
        now = utcnow()
        if row["expires_at"] is None or row["expires_at"] <= now:
            c.execute("DELETE FROM step_cache WHERE cache_key = ?", (cache_key,))
            return None
        try:
            from . import storage  # 局部导入避免与顶层 import 顺序耦合
            storage.load_output(data_dir, row["output_ref"], row["output_checksum"])
        except Exception:
            # 文件丢失或校验和不匹配：缓存不可信，删除该行（下次执行重建）
            c.execute("DELETE FROM step_cache WHERE cache_key = ?", (cache_key,))
            return None
        return {"output_ref": row["output_ref"], "output_checksum": row["output_checksum"]}

    return run_tx(conn, _fn)


def cache_save(
    conn, *, cache_key: str, step_type: str, step_version: str, config_hash: str, input_hash: str,
    output_ref: str, output_checksum: str, ttl_seconds: float,
) -> None:
    """只有成功且校验通过的结果才进缓存（INSERT OR REPLACE 刷新 TTL）。"""
    now = utcnow()

    def _fn(c):
        c.execute(
            "INSERT OR REPLACE INTO step_cache (cache_key, step_type, step_version, config_hash,"
            " input_hash, output_ref, output_checksum, created_at, expires_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                cache_key, step_type, step_version, config_hash, input_hash,
                output_ref, output_checksum, now, now + ttl_seconds,
            ),
        )

    run_tx(conn, _fn)


def cache_cleanup(conn) -> int:
    """惰性清理过期缓存行（Worker 低频调用）。"""
    def _fn(c):
        cur = c.execute("DELETE FROM step_cache WHERE expires_at < ?", (utcnow(),))
        return cur.rowcount

    return run_tx(conn, _fn)


def complete_step_from_cache(
    conn, *, step_id: str, attempt_id: str, worker_id: str, lease_version: int,
    output_ref: str, output_checksum: str, cache_key: str,
    log_ref: str | None, log_dir: str | None = None,
) -> None:
    """缓存命中提交：与真实执行同一套状态机（attempt 标 cache 命中，duration_ms=0）。"""
    def _fn(c):
        now = utcnow()
        cur = c.execute(
            "UPDATE task_steps SET status = ?, output_ref = ?, output_checksum = ?, finished_at = ?,"
            " heartbeat_at = NULL, lease_until = NULL"
            " WHERE step_id = ? AND worker_id = ? AND lease_version = ? AND status = ?",
            (
                StepStatus.SUCCEEDED.value, output_ref, output_checksum, now,
                step_id, worker_id, lease_version, StepStatus.RUNNING.value,
            ),
        )
        if cur.rowcount == 0:
            raise LeaseLostError(f"缓存命中提交被 fencing 拒绝: step={step_id} worker={worker_id}")
        c.execute(
            "UPDATE step_attempts SET status = ?, worker_id = 'cache', finished_at = ?, duration_ms = 0,"
            " output_ref = ?, log_ref = ? WHERE attempt_id = ?",
            (StepStatus.SUCCEEDED.value, now, output_ref, log_ref, attempt_id),
        )
        step = get_step(c, step_id)
        for event_type in ("CACHE_HIT", "STEP_SUCCEEDED"):
            events.insert_event(
                c, step["task_id"], event_type, step_id=step_id, attempt_id=attempt_id,
                worker_id="cache", status=StepStatus.SUCCEEDED.value, duration_ms=0,
                detail={"cache_key": cache_key, "output_ref": output_ref},
                log_dir=log_dir, created_at=now,
            )
        _refresh_task_status(c, step["task_id"], now, log_dir)

    return run_tx(conn, _fn)


# ---------------------------------------------------------------- 取消


def task_cancel_requested(conn, task_id: str) -> bool:
    row = conn.execute("SELECT cancel_requested FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    return bool(row and row["cancel_requested"])


def skip_running_step(
    conn, *, step_id: str, attempt_id: str, worker_id: str, lease_version: int,
    log_dir: str | None = None,
) -> None:
    """取消检查点：executor 主动中止的 RUNNING 步骤标 SKIPPED(CANCELLED_BY_USER)。"""
    def _fn(c):
        now = utcnow()
        cur = c.execute(
            "UPDATE task_steps SET status = ?, error_code = 'CANCELLED_BY_USER', finished_at = ?,"
            " next_retry_at = NULL, heartbeat_at = NULL, lease_until = NULL"
            " WHERE step_id = ? AND worker_id = ? AND lease_version = ? AND status = ?",
            (
                StepStatus.SKIPPED.value, now,
                step_id, worker_id, lease_version, StepStatus.RUNNING.value,
            ),
        )
        if cur.rowcount == 0:
            raise LeaseLostError(f"取消提交被 fencing 拒绝: step={step_id} worker={worker_id}")
        c.execute(
            "UPDATE step_attempts SET status = ?, finished_at = ?, error_code = 'CANCELLED_BY_USER'"
            " WHERE attempt_id = ?",
            (StepStatus.SKIPPED.value, now, attempt_id),
        )
        step = get_step(c, step_id)
        events.insert_event(
            c, step["task_id"], "STEP_SKIPPED", step_id=step_id, attempt_id=attempt_id,
            worker_id=worker_id, status=StepStatus.SKIPPED.value, error_code="CANCELLED_BY_USER",
            detail={"reason": "executor 取消检查点中止"}, log_dir=log_dir, created_at=now,
        )
        _refresh_task_status(c, step["task_id"], now, log_dir)

    return run_tx(conn, _fn)


def request_cancel(conn, task_id: str, log_dir: str | None = None) -> dict:
    """请求取消：写 cancel_requested + 事件；未开始的步骤立即 SKIPPED。

    RUNNING 步骤不立即打断（executor 检查点自然停止）；任务在全步骤终态后由
    summarize 推导为 CANCELLED。重复取消幂等（不再重复写事件）。
    """
    def _fn(c):
        now = utcnow()
        task = get_task(c, task_id)
        if task["status"] in ("SUCCEEDED", "FAILED", "CANCELLED"):
            raise DaoConflictError("TASK_ALREADY_TERMINAL", f"任务已终态: {task['status']}")
        if not task["cancel_requested"]:
            c.execute(
                "UPDATE tasks SET cancel_requested = 1, updated_at = ? WHERE task_id = ?",
                (now, task_id),
            )
            events.insert_event(
                c, task_id, "TASK_CANCEL_REQUESTED", status=task["status"],
                detail={"requested_at": now}, log_dir=log_dir, created_at=now,
            )
        # 未开始（PENDING/READY/RETRY_WAIT）的步骤直接进入终态；RUNNING 留给检查点
        for row in c.execute(
            "SELECT step_id, status FROM task_steps WHERE task_id = ?"
            " AND status IN (?,?,?)",
            (task_id, StepStatus.PENDING.value, StepStatus.READY.value, StepStatus.RETRY_WAIT.value),
        ).fetchall():
            ensure_step_transition(row["status"], StepStatus.SKIPPED.value)
            c.execute(
                "UPDATE task_steps SET status = ?, error_code = 'CANCELLED_BY_USER', finished_at = ?,"
                " next_retry_at = NULL WHERE step_id = ? AND status = ?",
                (StepStatus.SKIPPED.value, now, row["step_id"], row["status"]),
            )
            events.insert_event(
                c, task_id, "STEP_SKIPPED", step_id=row["step_id"],
                status=StepStatus.SKIPPED.value, error_code="CANCELLED_BY_USER",
                detail={"reason": "用户取消"}, log_dir=log_dir, created_at=now,
            )
        _refresh_task_status(c, task_id, now, log_dir)
        return {"task_id": task_id, "cancel_requested": True, "status": get_task(c, task_id)["status"]}

    return run_tx(conn, _fn)


# ---------------------------------------------------------------- 手动重试


def retry_step(conn, step_id: str, log_dir: str | None = None) -> dict:
    """手动重试：FAILED/RETRY_WAIT → READY，attempt_count 归零（attempt_no 继续递增，
    历史 attempts 行全保留），manual_retry_count 计数。

    - 依赖必须全 SUCCEEDED，否则 409 DEPENDENCY_NOT_SATISFIED
    - 任务若 FAILED → 回 RUNNING（唯一终态撤销路径，显式 WHERE status='FAILED' 守卫）
    - 下游 SKIPPED 步骤递归放回 PENDING 重新评估
    - 幂等：并发重复请求在 BEGIN IMMEDIATE 下串行化，后续请求看到步骤已被
      本轮手动重试过（非终态 + manual_retry_count>0）时返回相同语义结果
    """
    def _fn(c):
        now = utcnow()
        step = get_step(c, step_id)
        if step is None:
            raise DaoConflictError("NOT_FOUND", f"步骤不存在: {step_id}")
        task = get_task(c, step["task_id"])

        if step["status"] in (StepStatus.FAILED.value, StepStatus.RETRY_WAIT.value):
            deps = json.loads(step["dependencies"])
            bad = [
                d for d in deps
                if c.execute(
                    "SELECT status FROM task_steps WHERE task_id = ? AND step_type = ?",
                    (step["task_id"], d),
                ).fetchone()["status"] != StepStatus.SUCCEEDED.value
            ]
            if bad:
                raise DaoConflictError("DEPENDENCY_NOT_SATISFIED", f"依赖未全部成功: {bad}")
            ensure_step_transition(step["status"], StepStatus.READY.value)
            manual_retry_count = step["manual_retry_count"] + 1
            c.execute(
                "UPDATE task_steps SET status = ?, attempt_count = 0, manual_retry_count = ?,"
                " next_retry_at = NULL, error_code = NULL, error_message = NULL, worker_id = NULL,"
                " heartbeat_at = NULL, lease_until = NULL, finished_at = NULL"
                " WHERE step_id = ? AND status = ?",
                (StepStatus.READY.value, manual_retry_count, step_id, step["status"]),
            )
            events.insert_event(
                c, step["task_id"], "STEP_RETRY_MANUAL", step_id=step_id,
                status=StepStatus.READY.value,
                detail={"manual_retry_count": manual_retry_count,
                        "previous_attempt_count": step["attempt_count"]},
                log_dir=log_dir, created_at=now,
            )
            if task["status"] == TaskStatus.FAILED.value:
                # 终态守卫例外：手动重试撤销 FAILED，清掉 finished_at/error_summary
                c.execute(
                    "UPDATE tasks SET status = ?, finished_at = NULL, error_summary = NULL, updated_at = ?"
                    " WHERE task_id = ? AND status = ?",
                    (TaskStatus.RUNNING.value, now, step["task_id"], TaskStatus.FAILED.value),
                )
            reset = _reset_skipped_downstream(c, step["task_id"], step["step_type"], now, log_dir)
            _refresh_task_status(c, step["task_id"], now, log_dir)
            return {
                "step_id": step_id, "status": StepStatus.READY.value, "attempt_count": 0,
                "manual_retry_count": manual_retry_count, "downstream_reset": reset,
                "task_status": get_task(c, step["task_id"])["status"], "idempotent_repeat": False,
            }

        if step["status"] in (StepStatus.READY.value, StepStatus.RUNNING.value) and step["manual_retry_count"] > 0:
            # 幂等分支：本轮手动重试已生效（步骤被重置回执行通道），重复请求返回相同语义
            return {
                "step_id": step_id, "status": step["status"], "attempt_count": step["attempt_count"],
                "manual_retry_count": step["manual_retry_count"], "downstream_reset": [],
                "task_status": task["status"], "idempotent_repeat": True,
            }

        raise DaoConflictError("STATE_CONFLICT", f"步骤状态 {step['status']} 不允许手动重试")

    return run_tx(conn, _fn)
