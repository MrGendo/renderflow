"""输出文件存储：原子提交（tmp→fsync→sha256→os.replace）与读取校验。"""

import hashlib
import json
import os

from .enums import ErrorCode


class StorageError(Exception):
    def __init__(self, message: str, error_code: ErrorCode = ErrorCode.OUTPUT_INVALID):
        self.error_code = error_code
        super().__init__(message)


def attempt_dir_rel(task_id: str, step_type: str, attempt_no: int) -> str:
    """版本化不可变目录：不同尝试互不覆盖。返回相对 data_dir 的路径。"""
    return f"tasks/{task_id}/steps/{step_type}/attempt_{attempt_no}"


def output_relpath(task_id: str, step_type: str, attempt_no: int) -> str:
    return f"{attempt_dir_rel(task_id, step_type, attempt_no)}/output.json"


def run_log_relpath(task_id: str, step_type: str, attempt_no: int) -> str:
    return f"{attempt_dir_rel(task_id, step_type, attempt_no)}/run.log"


def commit_output(data_dir: str, task_id: str, step_type: str, attempt_no: int, payload: dict) -> tuple[str, str]:
    """原子提交输出：写 tmp → flush+fsync → 计算 sha256 → os.replace。

    返回 (output_ref 相对路径, checksum)。DB 登记失败时由调用方负责清理
    tmp 残留（W1 简单处理，W2 完善）。
    """
    rel = output_relpath(task_id, step_type, attempt_no)
    abs_path = os.path.join(data_dir, rel)
    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    # canonical 序列化保证同内容字节级一致，校验和才有意义
    data = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    tmp_path = abs_path + ".tmp"
    with open(tmp_path, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    checksum = hashlib.sha256(data).hexdigest()
    os.replace(tmp_path, abs_path)  # 原子改名，读方永远看到完整文件
    return rel, checksum


def load_output(data_dir: str, output_ref: str, expected_checksum: str | None) -> dict:
    """读取输出文件并校验 sha256；不符抛 StorageError(OUTPUT_INVALID)。"""
    abs_path = os.path.join(data_dir, output_ref)
    if not os.path.exists(abs_path):
        raise StorageError(f"输出文件不存在: {output_ref}")
    with open(abs_path, "rb") as f:
        data = f.read()
    if expected_checksum is not None and hashlib.sha256(data).hexdigest() != expected_checksum:
        raise StorageError(f"输出校验和不匹配: {output_ref}")
    return json.loads(data)


def append_run_log(data_dir: str, log_ref: str, event: dict) -> None:
    """向本次尝试的 run.log 追加一条 JSON 行（executor 逐事件调用）。"""
    abs_path = os.path.join(data_dir, log_ref)
    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    with open(abs_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
