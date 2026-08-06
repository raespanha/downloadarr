from pathlib import Path

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)

    database_url: str = Field("sqlite+aiosqlite:///data/downloadarr.db",
                              validation_alias="DOWNLOADARR_DATABASE_URL")
    download_path: Path = Field(Path("/downloads"), validation_alias="DOWNLOADARR_DOWNLOAD_PATH")
    username: str = Field("downloadarr", validation_alias="DOWNLOADARR_USERNAME")
    password: SecretStr = Field(default=SecretStr("downloadarr"), repr=False,
                                validation_alias="DOWNLOADARR_PASSWORD")
    api_key: SecretStr | None = Field(default=None, repr=False,
                                      validation_alias="DOWNLOADARR_API_KEY")
    torbox_api_token: SecretStr = Field(default=SecretStr(""), repr=False,
                                        validation_alias="TORBOX_API_TOKEN")
    torbox_api_base: str = Field("https://api.torbox.app/v1/api",
                                 validation_alias="TORBOX_API_BASE")
    torbox_request_timeout: float = Field(30.0, validation_alias="TORBOX_REQUEST_TIMEOUT")
    provider_concurrency: int = Field(4, validation_alias="DOWNLOADARR_PROVIDER_CONCURRENCY")
    poll_interval: float = 5.0
    queued_poll_interval: float = 30.0
    max_poll_backoff: float = 300.0

    @field_validator("provider_concurrency")
    @classmethod
    def valid_concurrency(cls, value: int) -> int:
        if not 1 <= value <= 64:
            raise ValueError("provider_concurrency must be between 1 and 64")
        return value

    @field_validator("torbox_request_timeout", "poll_interval", "queued_poll_interval")
    @classmethod
    def positive(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("timeout and polling intervals must be positive")
        return value
