"""环境变量配置：全部带默认值，可通过 .env 或 RENDERFLOW_ 前缀环境变量覆盖。"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RENDERFLOW_", env_file=".env", extra="ignore")

    db_path: str = "./data/renderflow.db"
    data_dir: str = "./data"
    log_dir: str = "./data/logs"
    poll_interval_ms: int = 300
    step_duration_ms: int = 800
    lease_seconds: float = 15.0
    heartbeat_seconds: float = 5.0


@lru_cache
def get_settings() -> Settings:
    """进程内缓存配置；测试通过 reset_settings() 后重设环境变量再取。"""
    return Settings()


def reset_settings() -> None:
    get_settings.cache_clear()
