"""Runtime settings, read from environment variables and an optional ``.env`` file."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TIBERO_",
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
    )

    host: str
    port: int = 8629
    dbname: str
    user: str
    password: SecretStr

    read_only: bool = True
    max_rows: int = Field(default=500, ge=1, le=100_000)
    query_timeout: int = Field(default=30, ge=0, description="seconds, 0 = no limit")

    jdbc_jar: Path | None = None
    java_home: Path | None = Field(default=None, validation_alias="JAVA_HOME")

    @property
    def jdbc_url(self) -> str:
        return f"jdbc:tibero:thin:@{self.host}:{self.port}:{self.dbname}"
