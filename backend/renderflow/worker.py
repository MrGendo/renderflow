"""Worker 主循环：领取 → 执行 → 提交。`python -m renderflow.worker` 启动。

SIGTERM/SIGINT 优雅停止：设 stop_flag，executor 分片 sleep 间隙检查后退出。
"""

import json
import os
import signal
import socket
import sys
import time
import uuid

from . import dao, db, executor, storage, workflow
from .config import Settings, get_settings


def new_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def _load_upstream_outputs(conn, task_id: str, dependencies: list[str], data_dir: str) -> dict:
    """读取全部依赖步骤的产出（校验 sha256），只取确定性的 mock_payload 部分。"""
    upstream = {}
    for dep in dependencies:
        row = conn.execute(
            "SELECT step_type, output_ref, output_checksum FROM task_steps"
            " WHERE task_id = ? AND step_type = ? AND status = 'SUCCEEDED'",
            (task_id, dep),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"依赖步骤未完成: {dep}")  # 领取前已严格校验，此处兜底
        output = storage.load_output(data_dir, row["output_ref"], row["output_checksum"])
        upstream[dep] = output["mock_payload"]
    return upstream


def run_once(conn, settings: Settings, worker_id: str) -> bool:
    """推进一个步骤：领取 → 重算输入 → 执行 → 提交。无事可做返回 False。"""
    claimed = dao.claim_next_step(conn, worker_id, settings.lease_seconds, log_dir=settings.log_dir)
    if not claimed:
        return False
    step = claimed
    log_ref = storage.run_log_relpath(step["task_id"], step["step_type"], step["attempt_no"])
    try:
        task = dao.get_task(conn, step["task_id"])
        task_input, durations = dao.task_input_of(task)
        deps = json.loads(step["dependencies"])
        upstream = _load_upstream_outputs(conn, step["task_id"], deps, settings.data_dir)

        # 领取后按真实上游产出重算 input_hash 回写（创建时是空上游的初始值）
        real_input = workflow.build_step_input(step["step_type"], task_input, upstream)
        input_hash = workflow.compute_input_hash(step["step_type"], real_input)
        dao.update_input_hash(conn, step["step_id"], input_hash)

        ctx = executor.StepContext(
            task_id=step["task_id"], step_id=step["step_id"], attempt_id=step["attempt_id"],
            step_type=step["step_type"], attempt_no=step["attempt_no"], input_hash=input_hash,
            inputs=real_input,
            duration_ms=int(durations.get(step["step_type"], settings.step_duration_ms)),
            log_ref=log_ref, data_dir=settings.data_dir,
        )
        on_tick = lambda: dao.touch_lease(
            conn, step["step_id"], worker_id, step["lease_version"], settings.lease_seconds
        )
        result = executor.execute(ctx, on_tick=on_tick)

        if result.success:
            output_ref, checksum = storage.commit_output(
                settings.data_dir, ctx.task_id, ctx.step_type, ctx.attempt_no, result.output
            )
            ok = dao.complete_step_success(
                conn, step_id=ctx.step_id, attempt_id=ctx.attempt_id, worker_id=worker_id,
                lease_version=step["lease_version"], output_ref=output_ref, output_checksum=checksum,
                log_ref=log_ref, log_dir=settings.log_dir,
            )
            if not ok:
                print(f"[{worker_id}] 提交被拒（租约/状态不符）: {ctx.step_id}", file=sys.stderr)
        else:
            dao.fail_step(
                conn, step_id=ctx.step_id, attempt_id=ctx.attempt_id, worker_id=worker_id,
                lease_version=step["lease_version"], error_code=result.error_code,
                error_message=result.error_message, log_ref=log_ref, log_dir=settings.log_dir,
            )
    except Exception as err:
        # W1 统一 INTERNAL_ERROR：attempt/step 置 FAILED，任务经状态机汇总为 FAILED
        dao.fail_step(
            conn, step_id=step["step_id"], attempt_id=step["attempt_id"], worker_id=worker_id,
            lease_version=step["lease_version"], error_code="INTERNAL_ERROR",
            error_message=repr(err), log_ref=log_ref, log_dir=settings.log_dir,
        )
    return True


def main() -> None:
    settings = get_settings()
    conn = db.connect(settings)
    db.init_db(conn)
    worker_id = new_worker_id()
    stop = {"flag": False}

    def _stop(signum, _frame):
        stop["flag"] = True  # 完成当前步骤后退出

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    print(f"[{worker_id}] Worker 启动，db={settings.db_path}", flush=True)
    try:
        while not stop["flag"]:
            try:
                worked = run_once(conn, settings, worker_id)
            except Exception as err:  # 单步异常不拖垮循环；步骤侧已在 run_once 内记账
                print(f"[{worker_id}] 步骤执行异常: {err!r}", file=sys.stderr, flush=True)
                worked = False
            if not worked:
                time.sleep(settings.poll_interval_ms / 1000.0)
    finally:
        conn.close()
        print(f"[{worker_id}] Worker 已退出", flush=True)


if __name__ == "__main__":
    main()
