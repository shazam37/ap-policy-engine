"""
Configuration module.
All settings loaded from environment variables with sensible defaults.
"""
from __future__ import annotations
from pydantic_settings import BaseSettings, SettingsConfigDict
from functools import lru_cache


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # App
    APP_NAME: str = "AP Policy Engine"
    APP_VERSION: str = "1.0.0"
    DEBUG: bool = False
    LOG_LEVEL: str = "INFO"

    # Groq
    GROQ_API_KEY: str = ""
    GROQ_MODEL: str = "openai/gpt-oss-120b"

    # SMTP / Mailtrap
    EMAIL_BACKEND: str = "console"   # "console" | "smtp"
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM: str = "ap-engine@company.com"
    SMTP_TLS: bool = True

    # Storage
    RULES_STORE_PATH: str = "./data/rules_store.json"
    DB_URL: str = "sqlite:///./data/ap_engine.db"

    # Extraction
    CONFIDENCE_THRESHOLD: float = 0.6
    LLM_RETRY_COUNT: int = 3


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()