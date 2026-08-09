import asyncio
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from .durability import fsync_directory


class DatabaseSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = "sqlite+aiosqlite:////config/downloadarr.db"


class DownloadSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = "/downloads"
    connections: int = 8
    provider_max_connections: int = 4
    minimum_file_size_mb: int = 0
    transfer_mode: Literal["auto", "sequential", "parallel"] = "auto"
    categories: dict[str, str] = Field(default_factory=dict)

    @field_validator("path", mode="before")
    @classmethod
    def path_string(cls, value: Any) -> str:
        value = str(value)
        if not value:
            raise ValueError("download path is required")
        return value

    @field_validator("connections", "provider_max_connections")
    @classmethod
    def valid_connections(cls, value: int) -> int:
        if not 1 <= value <= 256:
            raise ValueError("connections must be between 1 and 256")
        return value

    @field_validator("minimum_file_size_mb")
    @classmethod
    def valid_minimum_file_size(cls, value: int) -> int:
        if not 0 <= value <= 1_000_000:
            raise ValueError("minimum_file_size_mb must be between 0 and 1000000")
        return value

    @field_validator("categories")
    @classmethod
    def valid_categories(cls, value: dict[str, str]) -> dict[str, str]:
        for name, path in value.items():
            if not name or len(name) > 255 or not str(path):
                raise ValueError("download categories require a valid name and path")
        return value

    @field_validator("categories", mode="before")
    @classmethod
    def category_strings(cls, value: Any) -> dict[str, str]:
        if not isinstance(value, dict):
            raise ValueError("download categories must be an object")
        return {str(name): str(path) for name, path in value.items()}


class QBittorrentSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = "downloadarr"
    password: SecretStr = Field(default=SecretStr("downloadarr"), repr=False)
    api_key: SecretStr | None = Field(default=None, repr=False)
    webapi_version: str = "2.8.1"
    application_version: str = "v4.3.9"


class TorBoxSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    api_token: SecretStr = Field(default=SecretStr(""), repr=False)
    api_base: str = "https://api.torbox.app/v1/api"
    request_timeout: float = 30.0

    @field_validator("request_timeout")
    @classmethod
    def positive_timeout(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("request_timeout must be positive")
        return value


class ArrInstanceSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = ""
    api_key: SecretStr = Field(default=SecretStr(""), repr=False)
    category: str

    @field_validator("url", mode="before")
    @classmethod
    def normalized_url(cls, value: Any) -> str:
        return str(value or "").strip().rstrip("/")

    @field_validator("category")
    @classmethod
    def valid_category(cls, value: str) -> str:
        value = value.strip()
        if not value or len(value) > 255:
            raise ValueError("Arr category must be between 1 and 255 characters")
        return value

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.api_key.get_secret_value())


class IntegrationsSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sonarr: ArrInstanceSettings = Field(
        default_factory=lambda: ArrInstanceSettings(category="tv-sonarr"))
    radarr: ArrInstanceSettings = Field(
        default_factory=lambda: ArrInstanceSettings(category="radarr"))

    @model_validator(mode="before")
    @classmethod
    def category_defaults(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        result = dict(value)
        for name, category in (("sonarr", "tv-sonarr"), ("radarr", "radarr")):
            instance = dict(result.get(name) or {})
            instance.setdefault("category", category)
            result[name] = instance
        return result


class SchedulerSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider_concurrency: int = 4
    poll_interval: float = 5.0
    queued_poll_interval: float = 30.0
    max_poll_backoff: float = 300.0
    max_job_failures: int = 5

    @field_validator("provider_concurrency")
    @classmethod
    def valid_concurrency(cls, value: int) -> int:
        if not 1 <= value <= 64:
            raise ValueError("provider_concurrency must be between 1 and 64")
        return value

    @field_validator("max_job_failures")
    @classmethod
    def valid_failures(cls, value: int) -> int:
        if not 1 <= value <= 100:
            raise ValueError("max_job_failures must be between 1 and 100")
        return value

    @field_validator("poll_interval", "queued_poll_interval", "max_poll_backoff")
    @classmethod
    def positive_interval(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("polling intervals and backoff must be positive")
        return value


class TelemetrySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    retention_days: int = 0
    export_max_rows: int = 5000

    @field_validator("retention_days")
    @classmethod
    def valid_retention(cls, value: int) -> int:
        if value != 0 and not 30 <= value <= 3650:
            raise ValueError("retention_days must be 0 or between 30 and 3650")
        return value

    @field_validator("export_max_rows")
    @classmethod
    def valid_export_limit(cls, value: int) -> int:
        if not 100 <= value <= 50000:
            raise ValueError("export_max_rows must be between 100 and 50000")
        return value

class Settings(BaseModel):
    """Versioned, backup-friendly application configuration."""

    model_config = ConfigDict(extra="forbid")
    schema_version: int = 1
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    download: DownloadSettings = Field(default_factory=DownloadSettings)
    qbittorrent: QBittorrentSettings = Field(default_factory=QBittorrentSettings)
    torbox: TorBoxSettings = Field(default_factory=TorBoxSettings)
    integrations: IntegrationsSettings = Field(default_factory=IntegrationsSettings)
    scheduler: SchedulerSettings = Field(default_factory=SchedulerSettings)
    telemetry: TelemetrySettings = Field(default_factory=TelemetrySettings)

    @field_validator("schema_version")
    @classmethod
    def supported_version(cls, value: int) -> int:
        if value != 1:
            raise ValueError(f"unsupported settings schema version: {value}")
        return value

    # Compatibility accessors keep application code independent from storage layout.
    @property
    def database_url(self) -> str:
        return self.database.url

    @property
    def download_path(self) -> Path:
        return Path(self.download.path)

    @property
    def username(self) -> str:
        return self.qbittorrent.username

    @property
    def password(self) -> SecretStr:
        return self.qbittorrent.password

    @property
    def api_key(self) -> SecretStr | None:
        return self.qbittorrent.api_key

    @property
    def torbox_api_token(self) -> SecretStr:
        return self.torbox.api_token

    @property
    def torbox_api_base(self) -> str:
        return self.torbox.api_base

    @property
    def torbox_request_timeout(self) -> float:
        return self.torbox.request_timeout

    @property
    def provider_concurrency(self) -> int:
        return self.scheduler.provider_concurrency

    @property
    def poll_interval(self) -> float:
        return self.scheduler.poll_interval

    @property
    def queued_poll_interval(self) -> float:
        return self.scheduler.queued_poll_interval

    @property
    def max_poll_backoff(self) -> float:
        return self.scheduler.max_poll_backoff

    @property
    def max_job_failures(self) -> int:
        return self.scheduler.max_job_failures

    def masked(self) -> dict[str, Any]:
        data = self.model_dump(mode="json")
        data["qbittorrent"]["password"] = "********"
        data["qbittorrent"]["api_key"] = "********" if self.api_key else None
        data["torbox"]["api_token"] = "********" if self.torbox_api_token.get_secret_value() else ""
        for name in ("sonarr", "radarr"):
            instance = getattr(self.integrations, name)
            data["integrations"][name]["api_key"] = (
                "********" if instance.api_key.get_secret_value() else "")
        return data

    def storage_dict(self) -> dict[str, Any]:
        """Serialize validated settings with secrets for protected local storage."""
        data = self.model_dump(mode="json")
        data["qbittorrent"]["password"] = self.password.get_secret_value()
        data["qbittorrent"]["api_key"] = (self.api_key.get_secret_value()
                                                if self.api_key else None)
        data["torbox"]["api_token"] = self.torbox_api_token.get_secret_value()
        for name in ("sonarr", "radarr"):
            instance = getattr(self.integrations, name)
            data["integrations"][name]["api_key"] = instance.api_key.get_secret_value()
        return data


ENVIRONMENT_OVERRIDES: dict[str, tuple[str, ...]] = {
    "DOWNLOADARR_DATABASE_URL": ("database", "url"),
    "DOWNLOADARR_DOWNLOAD_PATH": ("download", "path"),
    "DOWNLOADARR_CONNECTIONS": ("download", "connections"),
    "DOWNLOADARR_PROVIDER_MAX_CONNECTIONS": ("download", "provider_max_connections"),
    "DOWNLOADARR_MINIMUM_FILE_SIZE_MB": ("download", "minimum_file_size_mb"),
    "DOWNLOADARR_TRANSFER_MODE": ("download", "transfer_mode"),
    "DOWNLOADARR_USERNAME": ("qbittorrent", "username"),
    "DOWNLOADARR_PASSWORD": ("qbittorrent", "password"),
    "DOWNLOADARR_API_KEY": ("qbittorrent", "api_key"),
    "TORBOX_API_TOKEN": ("torbox", "api_token"),
    "TORBOX_API_BASE": ("torbox", "api_base"),
    "TORBOX_REQUEST_TIMEOUT": ("torbox", "request_timeout"),
    "DOWNLOADARR_SONARR_URL": ("integrations", "sonarr", "url"),
    "DOWNLOADARR_SONARR_API_KEY": ("integrations", "sonarr", "api_key"),
    "DOWNLOADARR_SONARR_CATEGORY": ("integrations", "sonarr", "category"),
    "DOWNLOADARR_RADARR_URL": ("integrations", "radarr", "url"),
    "DOWNLOADARR_RADARR_API_KEY": ("integrations", "radarr", "api_key"),
    "DOWNLOADARR_RADARR_CATEGORY": ("integrations", "radarr", "category"),
    "DOWNLOADARR_PROVIDER_CONCURRENCY": ("scheduler", "provider_concurrency"),
    "DOWNLOADARR_POLL_INTERVAL": ("scheduler", "poll_interval"),
    "DOWNLOADARR_QUEUED_POLL_INTERVAL": ("scheduler", "queued_poll_interval"),
    "DOWNLOADARR_MAX_POLL_BACKOFF": ("scheduler", "max_poll_backoff"),
    "DOWNLOADARR_MAX_JOB_FAILURES": ("scheduler", "max_job_failures"),
    "DOWNLOADARR_TELEMETRY_RETENTION_DAYS": ("telemetry", "retention_days"),
    "DOWNLOADARR_EXPORT_MAX_ROWS": ("telemetry", "export_max_rows"),
}

SECRET_FILE_OVERRIDES = {
    f"{name}_FILE": path for name, path in ENVIRONMENT_OVERRIDES.items()
    if name in {"DOWNLOADARR_PASSWORD", "DOWNLOADARR_API_KEY", "TORBOX_API_TOKEN",
                "DOWNLOADARR_SONARR_API_KEY", "DOWNLOADARR_RADARR_API_KEY"}
}


def settings_path(path: str | os.PathLike[str] | None = None) -> Path:
    return Path(path or os.environ.get("DOWNLOADARR_CONFIG", "config/settings.json"))


def load_settings(path: str | os.PathLike[str] | None = None) -> Settings:
    configured_path = settings_path(path)
    values: dict[str, Any] = {}
    if configured_path.is_file():
        try:
            raw = json.loads(configured_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise ValueError(f"invalid Downloadarr settings file: {configured_path}") from error
        if not isinstance(raw, dict):
            raise ValueError(f"Downloadarr settings file must contain a JSON object: {configured_path}")
        values = _migrate_flat_settings(raw)
    _apply_environment(values)
    _apply_secret_files(values)
    return Settings.model_validate(values)


class SettingsService:
    """Coordinates validated, atomic, backup-preserving settings updates."""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = settings_path(path)
        self._lock = asyncio.Lock()

    async def load(self) -> Settings:
        async with self._lock:
            return await asyncio.to_thread(load_settings, self.path)

    async def save(self, settings: Settings) -> None:
        async with self._lock:
            await asyncio.to_thread(self._save_sync, settings)

    def managed_fields(self) -> list[str]:
        direct = [".".join(path) for name, path in ENVIRONMENT_OVERRIDES.items()
                  if name in os.environ]
        files = [".".join(path) for name, path in SECRET_FILE_OVERRIDES.items()
                 if name in os.environ]
        return sorted(set(direct + files))

    def _save_sync(self, settings: Settings) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            backup = self.path.with_name(f"{self.path.stem}.{stamp}.bak{self.path.suffix}")
            shutil.copy2(self.path, backup)
            backups = sorted(self.path.parent.glob(
                f"{self.path.stem}.*.bak{self.path.suffix}"), reverse=True)
            for stale in backups[10:]:
                stale.unlink(missing_ok=True)
        fd, name = tempfile.mkstemp(prefix=self.path.name + ".", suffix=".tmp",
                                    dir=self.path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(settings.storage_dict(), handle, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            fsync_directory(self.path.parent)
        finally:
            temporary.unlink(missing_ok=True)


def _apply_environment(values: dict[str, Any]) -> None:
    for variable, path in ENVIRONMENT_OVERRIDES.items():
        if variable not in os.environ:
            continue
        target = values
        for part in path[:-1]:
            target = target.setdefault(part, {})
        target[path[-1]] = os.environ[variable]


def _apply_secret_files(values: dict[str, Any]) -> None:
    for variable, path in SECRET_FILE_OVERRIDES.items():
        file_name = os.environ.get(variable)
        if not file_name:
            continue
        secret_path = Path(file_name)
        try:
            value = secret_path.read_text(encoding="utf-8").rstrip("\r\n")
        except OSError as error:
            raise ValueError(f"cannot read secret file for {variable}") from error
        if not value:
            raise ValueError(f"secret file for {variable} is empty")
        target = values
        for part in path[:-1]:
            target = target.setdefault(part, {})
        target[path[-1]] = value


def _migrate_flat_settings(raw: dict[str, Any]) -> dict[str, Any]:
    if any(key in raw for key in ("database", "download", "qbittorrent", "torbox",
                                  "integrations", "scheduler")):
        return raw
    mapping = {
        "database_url": ("database", "url"),
        "download_path": ("download", "path"),
        "provider_max_connections": ("download", "provider_max_connections"),
        "minimum_file_size_mb": ("download", "minimum_file_size_mb"),
        "username": ("qbittorrent", "username"),
        "password": ("qbittorrent", "password"),
        "api_key": ("qbittorrent", "api_key"),
        "torbox_api_token": ("torbox", "api_token"),
        "torbox_api_base": ("torbox", "api_base"),
        "torbox_request_timeout": ("torbox", "request_timeout"),
        "provider_concurrency": ("scheduler", "provider_concurrency"),
        "poll_interval": ("scheduler", "poll_interval"),
        "queued_poll_interval": ("scheduler", "queued_poll_interval"),
        "max_poll_backoff": ("scheduler", "max_poll_backoff"),
        "max_job_failures": ("scheduler", "max_job_failures"),
    }
    migrated: dict[str, Any] = {"schema_version": 1}
    for key, value in raw.items():
        path = mapping.get(key)
        if path is None:
            raise ValueError(f"unknown legacy setting: {key}")
        migrated.setdefault(path[0], {})[path[1]] = value
    return migrated
