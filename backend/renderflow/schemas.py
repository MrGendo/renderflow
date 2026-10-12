"""Pydantic 请求模型 + 统一响应包装与时间戳序列化。

统一包装：成功 {"ok":true,"data":...}；错误 {"ok":false,"error":{"code","message"}}。
"""

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class TaskInput(BaseModel):
    """任务业务输入：assets 必填非空，其余业务字段开放（extra=allow）。"""

    model_config = ConfigDict(extra="allow")
    assets: list[Any] = Field(min_length=1)


class TaskCreateRequest(BaseModel):
    input: TaskInput
    step_durations: dict[str, int] | None = None  # 按步骤覆盖 MOCK 时长（毫秒）
    workflow_version: str | None = None


def ok(data: Any) -> dict:
    return {"ok": True, "data": data}


def err(code: str, message: str) -> dict:
    return {"ok": False, "error": {"code": code, "message": message}}


def ts(value: float | None) -> dict | None:
    """时间戳双格式输出：DB 存 UTC unix epoch，API 层附 ISO8601（带 Z）+ 原始数值。"""
    if value is None:
        return None
    iso = datetime.fromtimestamp(value, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    return {"iso": iso, "epoch": value}
