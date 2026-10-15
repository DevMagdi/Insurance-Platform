"""Runtime configuration. No application credentials are embedded in source."""

from __future__ import annotations

import hmac
from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.security.crypto import decode_key


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file="../.env", extra="ignore")

    database_url: str = Field(validation_alias="DATABASE_URL")
    redis_url: str = "redis://redis:6379/0"
    app_encryption_key: str = Field(validation_alias="APP_ENCRYPTION_KEY")
    app_blind_index_key: str = Field(validation_alias="APP_BLIND_INDEX_KEY")
    jwt_signing_key: str = Field(validation_alias="JWT_SIGNING_KEY")
    s3_bucket: str = "complaint-attachments"
    s3_endpoint_url: str | None = None
    s3_access_key_id: str | None = None
    s3_secret_access_key: str | None = None
    s3_region: str = "us-east-1"
    smtp_host: str | None = None
    smtp_port: int = 1025
    smtp_from: str = "no-reply@insurance.local"
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_start_tls: bool = False
    dev_sms_mailbox: bool = False
    environment: str = "production"
    public_app_url: str = "http://localhost:8080"
    cookie_secure: bool = True
    access_token_minutes: int = 15
    refresh_token_days: int = 7
    otp_seconds: int = 300
    upload_max_bytes: int = 10_485_760

    @model_validator(mode="after")
    def validate_secrets(self) -> Settings:
        cipher = decode_key(self.app_encryption_key)
        blind = decode_key(self.app_blind_index_key)
        if hmac.compare_digest(cipher, blind):
            raise ValueError("Encryption and blind-index keys must be independent")
        if len(self.jwt_signing_key) < 32:
            raise ValueError("JWT_SIGNING_KEY must contain at least 32 characters")
        if self.environment not in {"production", "development", "test"}:
            raise ValueError("Invalid environment")
        if self.dev_sms_mailbox and (self.environment != "development" or not self.smtp_host):
            raise ValueError("Dev SMS mailbox requires development mode and SMTP")
        if self.environment == "production" and not self.cookie_secure:
            raise ValueError("Authentication cookies must be Secure in production")
        if (self.s3_access_key_id is None) != (self.s3_secret_access_key is None):
            raise ValueError("Both S3 credentials must be set together")
        if not 1 <= self.access_token_minutes <= 30 or not 1 <= self.refresh_token_days <= 30:
            raise ValueError("Invalid session duration")
        if not 60 <= self.otp_seconds <= 600 or not 1 <= self.upload_max_bytes <= 10_485_760:
            raise ValueError("Invalid OTP or upload limit")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # aliases are populated from the environment
