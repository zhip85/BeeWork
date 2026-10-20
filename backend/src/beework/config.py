from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = (
        "postgresql+psycopg://beework:beework_local_only@localhost:5432/beework"
    )
    valkey_url: str = "redis://localhost:6379/0"
    auth_secret: str = "local-only-change-this-secret-before-deploying"
    model_credentials_secret: str = "local-only-model-credentials-secret"
    auth_cookie_secure: bool = False
    app_origin: str = "http://localhost:5173"
    bootstrap_tenant_name: str = "Acme 科技"
    bootstrap_tenant_slug: str = "acme"
    bootstrap_admin_email: str = "admin@beework.dev"
    bootstrap_admin_name: str = "Olivia Lin"
    bootstrap_admin_password: str = "Beework-Dev-2026!"
    bootstrap_platform_admin_email: str = "platform@beework.dev"
    bootstrap_platform_admin_name: str = "BeeWork Platform Admin"
    bootstrap_platform_admin_password: str = "BeeWork-Platform-2026!"
    environment: str = "development"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @model_validator(mode="after")
    def validate_production_auth(self) -> "Settings":
        if (
            self.bootstrap_admin_email.strip().lower()
            == self.bootstrap_platform_admin_email.strip().lower()
        ):
            raise ValueError("Tenant and platform bootstrap administrators must be different accounts")
        if self.environment != "development":
            if (
                len(self.auth_secret) < 32
                or self.auth_secret == "local-only-change-this-secret-before-deploying"
            ):
                raise ValueError("AUTH_SECRET must be at least 32 characters outside development")
            if not self.auth_cookie_secure:
                raise ValueError("AUTH_COOKIE_SECURE must be true outside development")
            if not self.app_origin.startswith("https://"):
                raise ValueError("APP_ORIGIN must use HTTPS outside development")
            if (
                len(self.model_credentials_secret) < 32
                or self.model_credentials_secret == "local-only-model-credentials-secret"
            ):
                raise ValueError(
                    "MODEL_CREDENTIALS_SECRET must be at least 32 characters outside development"
                )
        return self


settings = Settings()
