"""FastAPI 应用：任务创建/查询 + 健康检查。uvicorn renderflow.api:app 启动。"""

import json
import sqlite3
from contextlib import asynccontextmanager

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import dao, db, workflow
from .config import Settings, get_settings
from .exceptions import DaoConflictError
from .schemas import TaskCreateRequest, err, ok, ts
from .state_machine import summarize_task


class ApiError(Exception):
    """业务错误：携带 HTTP 状态码与错误码，统一转成包装结构。"""

    def __init__(self, status_code: int, code: str, message: str):
        self.status_code = status_code
        self.code = code
        self.message = message
        super().__init__(message)


def _task_json(row, steps) -> dict:
    """任务全字段序列化 + summarize 的推导信息（进度/reason 一律来自后端）。"""
    summary = summarize_task(row, steps)
    succeeded = sum(1 for s in steps if s["status"] == "SUCCEEDED")
    input_payload, durations = dao.task_input_of(row)
    return {
        "task_id": row["task_id"],
        "status": row["status"],
        "derived_status": summary.derived_status,
        "reason_code": summary.reason_code,
        "progress_percent": summary.progress_percent,
        "succeeded_steps": succeeded,
        "total_steps": len(steps),
        "input": input_payload,
        "step_durations": durations,
        # 故障注入随任务持久化，详情 echo 出来便于演示（生效门在创建时把关）
        "fault_injection": dao.task_faults_of(row),
        "workflow_version": row["workflow_version"],
        "cancel_requested": bool(row["cancel_requested"]),
        "error_summary": row["error_summary"],
        "created_at": ts(row["created_at"]),
        "updated_at": ts(row["updated_at"]),
        "started_at": ts(row["started_at"]),
        "finished_at": ts(row["finished_at"]),
        "steps": [_step_overview(s) for s in steps],
    }


def _step_overview(row) -> dict:
    return {
        "step_id": row["step_id"],
        "step_type": row["step_type"],
        "status": row["status"],
        "attempt_count": row["attempt_count"],
        "max_attempts": row["max_attempts"],
        "error_code": row["error_code"],
    }


def _step_full(row) -> dict:
    return {
        "step_id": row["step_id"],
        "task_id": row["task_id"],
        "step_type": row["step_type"],
        "status": row["status"],
        "dependencies": json.loads(row["dependencies"]),
        "input_hash": row["input_hash"],
        "step_version": row["step_version"],
        "config_hash": row["config_hash"],
        "output_ref": row["output_ref"],
        "output_checksum": row["output_checksum"],
        "attempt_count": row["attempt_count"],
        "max_attempts": row["max_attempts"],
        "next_retry_at": ts(row["next_retry_at"]),
        "worker_id": row["worker_id"],
        "heartbeat_at": ts(row["heartbeat_at"]),
        "lease_until": ts(row["lease_until"]),
        "lease_version": row["lease_version"],
        "created_at": ts(row["created_at"]),
        "started_at": ts(row["started_at"]),
        "finished_at": ts(row["finished_at"]),
        "error_code": row["error_code"],
        "error_message": row["error_message"],
    }


def _attempt_json(row) -> dict:
    return {
        "attempt_id": row["attempt_id"],
        "step_id": row["step_id"],
        "attempt_no": row["attempt_no"],
        "worker_id": row["worker_id"],
        "status": row["status"],
        "started_at": ts(row["started_at"]),
        "finished_at": ts(row["finished_at"]),
        "duration_ms": row["duration_ms"],
        "error_code": row["error_code"],
        "error_message": row["error_message"],
        "log_ref": row["log_ref"],
        "output_ref": row["output_ref"],
    }


def _event_json(row) -> dict:
    return {
        "event_id": row["event_id"],
        "task_id": row["task_id"],
        "step_id": row["step_id"],
        "attempt_id": row["attempt_id"],
        "worker_id": row["worker_id"],
        "event_type": row["event_type"],
        "status": row["status"],
        "duration_ms": row["duration_ms"],
        "error_code": row["error_code"],
        "detail": json.loads(row["detail_json"]) if row["detail_json"] else None,
        "created_at": ts(row["created_at"]),
    }


def create_app(settings: Settings | None = None) -> FastAPI:
    st = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        conn = db.connect(st)
        db.init_db(conn)  # 幂等：应用与 Worker 启动时各执行一次
        app.state.conn = conn
        app.state.settings = st
        yield
        conn.close()

    app = FastAPI(title="RenderFlow API", version="0.1.0", lifespan=lifespan)

    @app.exception_handler(ApiError)
    async def _api_error(_req: Request, exc: ApiError):
        return JSONResponse(status_code=exc.status_code, content=err(exc.code, exc.message))

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_req: Request, exc: RequestValidationError):
        return JSONResponse(status_code=400, content=err("VALIDATION_ERROR", str(exc.errors()[:3])))

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_req: Request, exc: StarletteHTTPException):
        code = {404: "NOT_FOUND", 409: "STATE_CONFLICT", 400: "BAD_REQUEST"}.get(exc.status_code, "HTTP_ERROR")
        return JSONResponse(status_code=exc.status_code, content=err(code, str(exc.detail)))

    @app.exception_handler(sqlite3.Error)
    async def _db_error(_req: Request, exc: sqlite3.Error):
        return JSONResponse(status_code=500, content=err("INTERNAL_ERROR", f"数据库错误: {exc}"))

    @app.exception_handler(DaoConflictError)
    async def _dao_conflict(_req: Request, exc: DaoConflictError):
        return JSONResponse(status_code=409, content=err(exc.code, exc.message))

    def _conn() -> sqlite3.Connection:
        return app.state.conn

    def _get_task_or_404(task_id: str):
        row = dao.get_task(_conn(), task_id)
        if row is None:
            raise ApiError(404, "NOT_FOUND", f"任务不存在: {task_id}")
        return row

    @app.post("/api/tasks")
    def create_task(body: TaskCreateRequest):
        # 故障注入生效门：环境开关未开则忽略该字段并在响应中注明
        faults, ignored = body.fault_injection or {}, False
        if faults and not st.allow_fault_injection:
            faults, ignored = {}, True
        task_id = dao.create_task(
            _conn(), body.input.model_dump(), body.step_durations or {},
            body.workflow_version or workflow.WORKFLOW_VERSION, log_dir=st.log_dir,
            fault_injection=faults or None,
        )
        steps = dao.list_steps(_conn(), task_id)
        return ok({
            "task_id": task_id, "steps": [_step_overview(s) for s in steps],
            "fault_injection": faults, "fault_injection_ignored": ignored,
        })

    @app.get("/api/tasks")
    def list_tasks(
        page: int = Query(1, ge=1),
        page_size: int = Query(20, ge=1, le=100, alias="pageSize"),
        status: str | None = None,
    ):
        if status and status not in {"PENDING", "RUNNING", "RETRY_WAIT", "FAILED", "SUCCEEDED", "CANCELLED"}:
            raise ApiError(400, "VALIDATION_ERROR", f"非法状态筛选: {status}")
        total, rows = dao.list_tasks(_conn(), page, page_size, status)
        now = dao.utcnow()
        items = []
        for row in rows:
            steps = dao.list_steps(_conn(), row["task_id"])
            summary = summarize_task(row, steps)
            end = row["finished_at"] or now
            items.append({
                "task_id": row["task_id"],
                "status": row["status"],
                "reason_code": summary.reason_code,
                "progress_percent": summary.progress_percent,
                "succeeded_steps": sum(1 for s in steps if s["status"] == "SUCCEEDED"),
                "total_steps": len(steps),
                "duration_ms": int((end - row["created_at"]) * 1000) if row["created_at"] else None,
                "created_at": ts(row["created_at"]),
                "finished_at": ts(row["finished_at"]),
            })
        return ok({"total": total, "page": page, "pageSize": page_size, "items": items})

    @app.get("/api/tasks/{task_id}")
    def get_task(task_id: str):
        row = _get_task_or_404(task_id)
        return ok(_task_json(row, dao.list_steps(_conn(), task_id)))

    @app.get("/api/tasks/{task_id}/steps")
    def list_steps(task_id: str):
        _get_task_or_404(task_id)
        return ok([_step_full(s) for s in dao.list_steps(_conn(), task_id)])

    @app.get("/api/tasks/{task_id}/events")
    def list_events(task_id: str, limit: int = Query(100, ge=1, le=1000)):
        _get_task_or_404(task_id)
        return ok([_event_json(e) for e in dao.list_events(_conn(), task_id, limit)])

    @app.get("/api/steps/{step_id}/attempts")
    def list_attempts(step_id: str):
        step = dao.get_step(_conn(), step_id)
        if step is None:
            raise ApiError(404, "NOT_FOUND", f"步骤不存在: {step_id}")
        return ok([_attempt_json(a) for a in dao.list_attempts(_conn(), step_id)])

    @app.get("/api/attempts/{attempt_id}")
    def get_attempt(attempt_id: str):
        """单次尝试详情（含所在 task_id，供前端尝试详情页直达）。"""
        row = _conn().execute(
            "SELECT a.*, s.task_id AS task_id FROM step_attempts a"
            " JOIN task_steps s ON s.step_id = a.step_id WHERE a.attempt_id = ?",
            (attempt_id,),
        ).fetchone()
        if row is None:
            raise ApiError(404, "NOT_FOUND", f"执行尝试不存在: {attempt_id}")
        return ok(_attempt_json(row) | {"task_id": row["task_id"]})

    @app.post("/api/steps/{step_id}/retry")
    def retry_step(step_id: str):
        """手动重试：FAILED/RETRY_WAIT → READY，attempt_count 归零（历史保留）。

        并发重复请求幂等（事务串行化 + manual_retry_count 判重）；
        依赖不满足 / 状态不允许由 DAO 抛 DaoConflictError → 409。
        """
        if dao.get_step(_conn(), step_id) is None:
            raise ApiError(404, "NOT_FOUND", f"步骤不存在: {step_id}")
        result = dao.retry_step(_conn(), step_id, log_dir=st.log_dir)
        return ok(result)

    @app.post("/api/tasks/{task_id}/cancel")
    def cancel_task(task_id: str):
        """请求取消：未开始步骤立即 SKIPPED(CANCELLED_BY_USER)，RUNNING 走检查点。"""
        _get_task_or_404(task_id)
        result = dao.request_cancel(_conn(), task_id, log_dir=st.log_dir)
        return ok(result)

    @app.get("/api/health")
    def health():
        return ok({"status": "up"})

    @app.get("/api/ready")
    def ready():
        try:
            _conn().execute("SELECT COUNT(*) FROM schema_migrations").fetchone()
            return ok({"status": "ready"})
        except sqlite3.Error as e:
            return JSONResponse(status_code=503, content=err("RESOURCE_UNAVAILABLE", f"数据库未就绪: {e}"))

    return app


app = create_app()
