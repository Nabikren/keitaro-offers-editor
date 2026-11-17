import re
from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# Собственные тестовые кампании кандидата: любой рабочий префикс обязан начинаться с этого корня.
REQUIRED_PREFIX_ROOT = "test_simonov_"


class Settings(BaseSettings):
    """Настройки backend. Значения читаются из окружения и файла .env в корне проекта."""

    model_config = SettingsConfigDict(env_file=("../.env", ".env"), extra="ignore")

    keitaro_url: str = ""
    keitaro_api_key: str = Field(default="", repr=False)
    keitaro_timeout_seconds: float = 15.0

    database_url: str = "sqlite:///./data/app.sqlite3"

    campaign_name_prefix: str = "test_simonov_"
    redirect_url: str = "https://www.google.com/"

    default_domain_id: int | None = None
    default_group_id: int | None = None
    default_traffic_source_id: int | None = None

    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    # Через сколько секунд незавершённый Push показывается как «возможно зависший». Только предупреждение:
    # права перехватить запись это не даёт; восстановление — офлайн `python -m app.recover`.
    push_stale_seconds: int = 120
    # Аренда задания создания кампании; по истечении задание можно продолжить (с предварительной сверкой).
    job_lease_seconds: int = 120

    @field_validator("redirect_url")
    @classmethod
    def _http_url(cls, value: str) -> str:
        from urllib.parse import urlsplit

        value = value.strip()
        if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in value):
            raise ValueError("REDIRECT_URL не должен содержать пробелы и управляющие символы")
        try:
            parts = urlsplit(value)
            parts.port  # некорректный порт («host:bad», «host:99999») вызывает ValueError уже здесь
        except ValueError as e:
            raise ValueError(f"REDIRECT_URL некорректен: {e}") from e
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("REDIRECT_URL должен быть абсолютным http(s)-URL")
        return value.strip()

    @field_validator("default_domain_id", "default_group_id", "default_traffic_source_id", mode="before")
    @classmethod
    def _empty_to_none(cls, value):
        return None if value in ("", None) else value

    @property
    def effective_prefix(self) -> str | None:
        """Префикс собственных кампаний. Пустой, пробельный, с недопустимыми символами или не начинающийся
        с test_simonov_ — None: тогда Push и создание кампаний запрещены (а не разрешены для всех)."""
        p = self.campaign_name_prefix
        if not p or p != p.strip() or not re.fullmatch(r"[A-Za-z0-9_.\-]{3,60}", p):
            return None
        if not p.startswith(REQUIRED_PREFIX_ROOT):
            return None
        return p

    @property
    def keitaro_configured(self) -> bool:
        return bool(self.keitaro_url and self.keitaro_api_key)

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
