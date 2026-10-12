"""Worker 主循环：回收过期租约 → 领取 → 缓存检查 → 执行 → 提交。

`python -m renderflow.worker` 启动。SIGTERM/SIGINT 优雅停止：设 stop_flag，
executor 分片 sleep 间隙检查后退出。
"""

import json
import os
import signal
import socket
import sys
import time
import uuid

from . import dao, db, events, executor, storage, workflow
from .config import Settings, get_settings
from .db import run_tx
from .exceptions import LeaseLostError, StepCancelledError


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


def _record_fenced(conn, settings: Settings, step: dict, worker_id: str, phase: str) -> None:
    """fencing 拒绝后记账 STEP_FENCED（独立事务，尽力而为）。

    不回滚新持有者已写入的任何状态——旧 Worker 到此为止彻底放弃该步骤。
    """
    def _fn(c):
        events.insert_event(
            c, step["task_id"], "STEP_FENCED", step_id=step["step_id"], attempt_id=step["attempt_id"],
            worker_id=worker_id, detail={"phase": phase, "lease_version": step["lease_version"]},
            log_dir=settings.log_dir,
        )

    try:
        run_tx(conn, _fn)
    except Exception as err:  # 事件是尽力而为的诊断流，失败不影响放弃语义
        print(f"[{worker_id}] STEP_FENCED 事件写入失败: {err!r}", file=sys.stderr)


def run_once(conn, settings: Settings, worker_id: str, loop_count: int = 0) -> bool:
    """推进一个步骤：回收过期租约 → 领取 → 重算输入 → 缓存检查 → 执行 → 提交。

    无事可做返回 False。loop_count 用于低频惰性清理（每 50 轮清一次过期缓存）。
    """
    if settings.cache_enabled and loop_count % 50 == 0:
        dao.cache_cleanup(conn)
    # 先回收真正过期的租约（租约即宽限期），有则直接接管执行；否则正常领取
    claimed = dao.recover_expired_leases(conn, worker_id, settings.lease_seconds, log_dir=settings.log_dir)
    if not claimed:
        claimed = dao.claim_next_step(conn, worker_id, settings.lease_seconds, log_dir=settings.log_dir)
    if not claimed:
        return False
    step = claimed
    log_ref = storage.run_log_relpath(step["task_id"], step["step_type"], step["attempt_no"])
    try:
        task = dao.get_task(conn, step["task_id"])
        task_input, durations = dao.task_input_of(task)
        faults = dao.task_faults_of(task).get(step["step_type"], {})
        deps = json.loads(step["dependencies"])
        upstream = _load_upstream_outputs(conn, step["task_id"], deps, settings.data_dir)

        # 领取后按真实上游产出重算 input_hash 回写（创建时是空上游的初始值）
        real_input = workflow.build_step_input(step["step_type"], task_input, upstream)
        input_hash = workflow.compute_input_hash(step["step_type"], real_input)
        dao.update_input_hash(conn, step["step_id"], input_hash)

        # 缓存命中检查（紧邻领取事务）：命中直接走状态机提交，不执行步骤
        key = workflow.cache_key(step["step_type"], input_hash, step["step_version"], step["config_hash"])
        if settings.cache_enabled:
            hit = dao.cache_lookup(conn, settings.data_dir, key)
            if hit is not None:
                storage.append_run_log(settings.data_dir, log_ref, {
                    "t": time.time(), "event": "CACHE_HIT", "cache_key": key,
                    "output_ref": hit["output_ref"],
                })
                try:
                    dao.complete_step_from_cache(
                        conn, step_id=step["step_id"], attempt_id=step["attempt_id"],
                        worker_id=worker_id, lease_version=step["lease_version"],
                        output_ref=hit["output_ref"], output_checksum=hit["output_checksum"],
                        cache_key=key, log_ref=log_ref, log_dir=settings.log_dir,
                    )
                except LeaseLostError:
                    _record_fenced(conn, settings, step, worker_id, "缓存命中提交")
                return True

        spec = workflow.get_step_spec(step["step_type"])
        ctx = executor.StepContext(
            task_id=step["task_id"], step_id=step["step_id"], attempt_id=step["attempt_id"],
            step_type=step["step_type"], attempt_no=step["attempt_no"], input_hash=input_hash,
            inputs=real_input,
            duration_ms=int(durations.get(step["step_type"], settings.step_duration_ms)),
            log_ref=log_ref, data_dir=settings.data_dir,
            timeout_s=spec.timeout_s, faults=faults,
            upload_dir=os.path.join(settings.data_dir, "uploads", step["task_id"]),
        )
        last_hb = {"at": 0.0}

        def on_tick():
            # 心跳按 heartbeat_seconds 节流；续租被拒立即上抛中止执行
            now = time.time()
            if now - last_hb["at"] >= settings.heartbeat_seconds:
                dao.touch_lease(
                    conn, step["step_id"], worker_id, step["lease_version"], settings.lease_seconds
                )
                last_hb["at"] = now
            # 取消检查点：已请求取消则尽快自然停止（RUNNING 不硬打断）
            if dao.task_cancel_requested(conn, step["task_id"]):
                raise StepCancelledError(f"任务已请求取消: {step['task_id']}")

        result = executor.execute(ctx, on_tick=on_tick)

        if result.success:
            output_ref, checksum = storage.commit_output(
                settings.data_dir, ctx.task_id, ctx.step_type, ctx.attempt_no, result.output
            )
            try:
                dao.complete_step_success(
                    conn, step_id=ctx.step_id, attempt_id=ctx.attempt_id, worker_id=worker_id,
                    lease_version=step["lease_version"], output_ref=output_ref, output_checksum=checksum,
                    log_ref=log_ref, log_dir=settings.log_dir,
                )
            except LeaseLostError:
                _record_fenced(conn, settings, step, worker_id, "成功结果提交")
                return True
            # 只有成功且校验通过的结果才进缓存
            if settings.cache_enabled:
                dao.cache_save(
                    conn, cache_key=key, step_type=ctx.step_type, step_version=step["step_version"],
                    config_hash=step["config_hash"], input_hash=input_hash,
                    output_ref=output_ref, output_checksum=checksum,
                    ttl_seconds=settings.cache_ttl_seconds,
                )
        else:
            try:
                dao.fail_step(
                    conn, step_id=ctx.step_id, attempt_id=ctx.attempt_id, worker_id=worker_id,
                    lease_version=step["lease_version"], error_code=result.error_code,
                    error_message=result.error_message, log_ref=log_ref, log_dir=settings.log_dir,
                    backoff_base_s=settings.retry_backoff_base_s, backoff_max_s=settings.retry_backoff_max_s,
                )
            except LeaseLostError:
                _record_fenced(conn, settings, step, worker_id, "失败结果提交")
    except StepCancelledError:
        try:
            dao.skip_running_step(
                conn, step_id=step["step_id"], attempt_id=step["attempt_id"], worker_id=worker_id,
                lease_version=step["lease_version"], log_dir=settings.log_dir,
            )
        except LeaseLostError:
            _record_fenced(conn, settings, step, worker_id, "取消中止提交")
    except LeaseLostError:
        _record_fenced(conn, settings, step, worker_id, "执行中心跳续租")
    except Exception as err:
        # 兜底：INTERNAL_ERROR 保守视为不可重试（README 说明），走同一失败引擎
        try:
            dao.fail_step(
                conn, step_id=step["step_id"], attempt_id=step["attempt_id"], worker_id=worker_id,
                lease_version=step["lease_version"], error_code="INTERNAL_ERROR",
                error_message=repr(err), log_ref=log_ref, log_dir=settings.log_dir,
                backoff_base_s=settings.retry_backoff_base_s, backoff_max_s=settings.retry_backoff_max_s,
            )
        except LeaseLostError:
            _record_fenced(conn, settings, step, worker_id, "异常兜底提交")
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
    loop_count = 0
    try:
        while not stop["flag"]:
            try:
                worked = run_once(conn, settings, worker_id, loop_count)
            except Exception as err:  # 单步异常不拖垮循环；步骤侧已在 run_once 内记账
                print(f"[{worker_id}] 步骤执行异常: {err!r}", file=sys.stderr, flush=True)
                worked = False
            loop_count += 1
            if not worked:
                time.sleep(settings.poll_interval_ms / 1000.0)
    finally:
        conn.close()
        print(f"[{worker_id}] Worker 已退出", flush=True)


if __name__ == "__main__":
    main()
