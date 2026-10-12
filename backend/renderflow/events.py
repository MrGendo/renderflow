"""task_events 表写入 + JSON 行式文件日志（{log_dir}/events.jsonl）。

文件日志是尽力而为的诊断流，以 DB 的 task_events 为准。
"""

import json
import os
import time


def insert_event(
    conn,
    task_id: str,
    event_type: str,
    *,
    step_id: str | None = None,
    attempt_id: str | None = None,
    worker_id: str | None = None,
    status: str | None = None,
    duration_ms: int | None = None,
    error_code: str | None = None,
    detail: dict | None = None,
    log_dir: str | None = None,
    created_at: float | None = None,
) -> int:
    """写一条事件行（须已在调用方事务内），返回 event_id。"""
    ts = created_at if created_at is not None else time.time()
    cur = conn.execute(
        "INSERT INTO task_events (task_id, step_id, attempt_id, worker_id, event_type, status,"
        " duration_ms, error_code, detail_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            task_id, step_id, attempt_id, worker_id, event_type, status,
            duration_ms, error_code,
            json.dumps(detail, ensure_ascii=False, sort_keys=True) if detail is not None else None,
            ts,
        ),
    )
    event_id = cur.lastrowid
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)
        record = {
            "event_id": event_id, "created_at": ts, "task_id": task_id, "step_id": step_id,
            "attempt_id": attempt_id, "worker_id": worker_id, "event_type": event_type,
            "status": status, "duration_ms": duration_ms, "error_code": error_code, "detail": detail,
        }
        with open(os.path.join(log_dir, "events.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return event_id
