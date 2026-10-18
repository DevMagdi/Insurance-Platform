"""Runtime configuration. No application credentials are embedded in source."""

from __future__ import annotations

import ipaddress
import re
from functools import lru_cache
from urllib.parse import urlsplit

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

from app.security.crypto import build_keyring, decode_key, idempotency_root
from app.security.jwt_keys import JWTKeyRing
from app.security.jwt_keys import build_keyring as build_jwt_keyring
from app.security.keyring import Keyring
from app.security.proxy import DEFAULT_TRUSTED_PROXIES, ClientIPResolver


def _is_real_mail_host(value: str) -> bool:
    host = value.strip().lower().rstrip(".")
    if not host or host in {"localhost", "mailpit", "mailhog", "smtp", "testserver"}:
        return False
    if host.endswith((".local", ".localhost", ".test", ".invalid")) or any(
        marker in host for marker in ("mailtrap", "mailpit", "mailhog", "sandbox")
    ):
        return False
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        return "." in host


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file="../.env", extra="ignore")

    database_url: str = Field(validation_alias="DATABASE_URL")
    # Optional streaming replica (via its own PgBouncer) for read-only analytics.
    database_read_url: str | None = Field(default=None, validation_alias="DATABASE_READ_URL")
    redis_url: str = "redis://redis:6379/0"
    app_encryption_key: str = Field(validation_alias="APP_ENCRYPTION_KEY")
    app_encryption_key_id: int = Field(default=1, validation_alias="APP_ENCRYPTION_KEY_ID")
    app_encryption_previous_keys: str = Field(
        default="", validation_alias="APP_ENCRYPTION_PREVIOUS_KEYS"
    )
    # Stable HMAC root for integration de-duplication digests stored in the DB.
    # Defaults to the lowest-id encryption key; set it explicitly before the
    # original key is retired so dedupe keys survive encryption-key rotation.
    app_idempotency_key: str | None = Field(default=None, validation_alias="APP_IDEMPOTENCY_KEY")
    app_blind_index_key: str = Field(validation_alias="APP_BLIND_INDEX_KEY")
    # Legacy-named HMAC root used by OTP, lockout and rate-limit digests. JWTs
    # are signed separately with the asymmetric Ed25519 key pair below.
    jwt_signing_key: str = Field(validation_alias="JWT_SIGNING_KEY")
    jwt_key_id: str = Field(default="active", validation_alias="JWT_KEY_ID")
    jwt_private_key_b64: str | None = Field(default=None, validation_alias="JWT_PRIVATE_KEY_B64")
    jwt_public_key_b64: str | None = Field(default=None, validation_alias="JWT_PUBLIC_KEY_B64")
    jwt_previous_public_keys: str = Field(default="", validation_alias="JWT_PREVIOUS_PUBLIC_KEYS")
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_from: str = ""
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_start_tls: bool = True
    smtp_use_tls: bool = False
    environment: str = "production"
    public_app_url: str = "http://localhost:8080"
    cookie_secure: bool = True
    access_token_minutes: int = 15
    refresh_token_days: int = 7
    otp_seconds: int = 300
    # Attachments, export artifacts and call recordings are stored in PostgreSQL
    # (encrypted with APP_ENCRYPTION_KEY); no object-storage endpoint is configured.
    upload_max_bytes: int = 10_485_760

    # --- Phase 8 hardening -------------------------------------------------
    trusted_proxy_cidrs: str = Field(
        default=DEFAULT_TRUSTED_PROXIES, validation_alias="TRUSTED_PROXY_CIDRS"
    )
    cors_allowed_origins: str = Field(default="", validation_alias="CORS_ALLOWED_ORIGINS")
    hsts_seconds: int = Field(default=31_536_000, validation_alias="HSTS_SECONDS")
    max_json_body_bytes: int = Field(default=1_048_576, validation_alias="MAX_JSON_BODY_BYTES")
    turnstile_site_key: str | None = Field(default=None, validation_alias="TURNSTILE_SITE_KEY")
    turnstile_secret_key: str | None = Field(default=None, validation_alias="TURNSTILE_SECRET_KEY")
    rate_limit_multiplier: float = Field(default=1.0, validation_alias="RATE_LIMIT_MULTIPLIER")
    abuse_denied_threshold: int = Field(default=40, validation_alias="ABUSE_DENIED_THRESHOLD")
    queue_backpressure_threshold: int = Field(
        default=5_000, validation_alias="QUEUE_BACKPRESSURE_THRESHOLD"
    )
    metrics_token: str | None = Field(default=None, validation_alias="METRICS_TOKEN")
    sentry_dsn: str | None = Field(default=None, validation_alias="SENTRY_DSN")
    sentry_traces_sample_rate: float = Field(
        default=0.05, validation_alias="SENTRY_TRACES_SAMPLE_RATE"
    )
    app_release: str | None = Field(default=None, validation_alias="APP_RELEASE")
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")
    log_json: bool = Field(default=True, validation_alias="LOG_JSON")
    privacy_policy_version: str = Field(
        default="2026-10", validation_alias="PRIVACY_POLICY_VERSION"
    )

    @model_validator(mode="after")
    def validate_secrets(self) -> Settings:
        keyring = self.encryption_keyring()
        blind = decode_key(self.app_blind_index_key)
        if keyring.contains_key(blind):
            raise ValueError("Encryption and blind-index keys must be independent")
        if self.app_idempotency_key and decode_key(self.app_idempotency_key) == blind:
            raise ValueError("Idempotency and blind-index keys must be independent")
        if len(self.jwt_signing_key) < 32:
            raise ValueError("JWT_SIGNING_KEY must contain at least 32 characters")
        self.jwt_keyring()  # validate the configured Ed25519 pair and rotation keys
        if self.environment not in {"production", "development", "test"}:
            raise ValueError("Invalid environment")
        if self.smtp_start_tls and self.smtp_use_tls:
            raise ValueError("Choose SMTP_START_TLS or SMTP_USE_TLS, not both")
        if not 1 <= self.smtp_port <= 65535:
            raise ValueError("SMTP_PORT must be between 1 and 65535")
        if self.smtp_host:
            sender_domain = self.smtp_from.rpartition("@")[2].lower()
            if self.environment == "production":
                valid_sender = re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", self.smtp_from)
                if not valid_sender or not _is_real_mail_host(sender_domain):
                    raise ValueError("SMTP_FROM must be a real sender address on a public domain")
            if not (self.smtp_start_tls or self.smtp_use_tls):
                raise ValueError("SMTP delivery requires STARTTLS or implicit TLS")
        if self.environment == "production" and not self.smtp_host:
            raise ValueError("Production requires a real SMTP provider for account and OTP email")
        if self.environment == "production" and not self.cookie_secure:
            raise ValueError("Authentication cookies must be Secure in production")
        if not 1 <= self.access_token_minutes <= 30 or not 1 <= self.refresh_token_days <= 30:
            raise ValueError("Invalid session duration")
        if not 60 <= self.otp_seconds <= 600 or not 1 <= self.upload_max_bytes <= 10_485_760:
            raise ValueError("Invalid OTP or upload limit")
        if self.database_read_url and (
            not self.database_read_url.startswith("postgresql+asyncpg://")
            or make_url(self.database_read_url).username != "app_runtime"
        ):
            raise ValueError("DATABASE_READ_URL must use asyncpg and the app_runtime role")
        ClientIPResolver.from_csv(self.trusted_proxy_cidrs)  # validates CIDR syntax
        for origin in filter(None, (o.strip() for o in self.cors_allowed_origins.split(","))):
            parsed = urlsplit(origin.rstrip("/"))
            if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.path:
                raise ValueError("CORS_ALLOWED_ORIGINS must list bare http(s) origins")
            if self.environment == "production" and parsed.scheme != "https":
                raise ValueError("Production origins must use HTTPS")
        if not 1024 <= self.max_json_body_bytes <= 10_485_760:
            raise ValueError("MAX_JSON_BODY_BYTES must be between 1 KiB and 10 MiB")
        if (self.turnstile_site_key is None) != (self.turnstile_secret_key is None):
            raise ValueError("Set both TURNSTILE_SITE_KEY and TURNSTILE_SECRET_KEY")
        if self.environment == "production":
            if not self.turnstile_secret_key:
                raise ValueError("Production requires Turnstile CAPTCHA keys on public forms")
            if self.rate_limit_multiplier != 1.0:
                raise ValueError("RATE_LIMIT_MULTIPLIER may only be changed outside production")
        if not 0.01 <= self.rate_limit_multiplier <= 1000:
            raise ValueError("Invalid RATE_LIMIT_MULTIPLIER")
        if self.metrics_token is not None and len(self.metrics_token) < 32:
            raise ValueError("METRICS_TOKEN must contain at least 32 characters")
        if self.environment == "production" and not self.metrics_token:
            raise ValueError("Production requires METRICS_TOKEN")
        if not 0 <= self.sentry_traces_sample_rate <= 1:
            raise ValueError("SENTRY_TRACES_SAMPLE_RATE must be within 0..1")
        return self

    def jwt_keyring(self) -> JWTKeyRing:
        return build_jwt_keyring(
            active_key_id=self.jwt_key_id,
            private_key_b64=self.jwt_private_key_b64,
            public_key_b64=self.jwt_public_key_b64,
            previous_public_keys=self.jwt_previous_public_keys,
            development_secret=self.jwt_signing_key,
            allow_derived_key=not self.is_production,
        )

    def encryption_keyring(self) -> Keyring:
        return build_keyring(
            self.app_encryption_key, self.app_encryption_key_id, self.app_encryption_previous_keys
        )

    def idempotency_master_key(self) -> bytes:
        return idempotency_root(self.encryption_keyring(), self.app_idempotency_key)

    def allowed_origins(self) -> frozenset[str]:
        """The app's own origin plus any explicitly allow-listed origins."""
        own = urlsplit(self.public_app_url)
        origins = {f"{own.scheme}://{own.netloc}".lower()} if own.netloc else set()
        origins |= {
            item.strip().rstrip("/").lower()
            for item in self.cors_allowed_origins.split(",")
            if item.strip()
        }
        return frozenset(origins)

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # aliases are populated from the environment
