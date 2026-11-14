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
    # PRD PLT-052 asks for a second platform admin to review suspend / delete. Switched off by decision (2026-11-09):
    # lifecycle changes apply directly. Set TENANT_LIFECYCLE_REVIEW=true to bring the review back.
    tenant_lifecycle_review: bool = False
    # PII / safety detectors (T-055). Both are optional: an unconfigured detector is reported as such and fails open.
    presidio_analyzer_url: str = ""  # e.g. http://presidio-analyzer:3000 (compose profile `pii`)
    presidio_language: str = "en"
    safety_judge_model: str = ""  # a LiteLLM model alias used by LLM-judge rules
    safety_judge_timeout_seconds: float = 1.5
    # PLT-071: adding, switching off or retiring an enforced platform PII / baseline rule needs a second administrator.
    platform_rule_review: bool = True
    # T-071: tool calls (MCP, API plugins, flow connections) only reach public https hosts. Local development may allow
    # http and loopback / private addresses with ALLOW_INSECURE_TOOL_URLS=true; never switch it on in production.
    allow_insecure_tool_urls: bool = False
    # T-165: the platform default policy of a tool call (an entry of the capability registry overrides it). Retrying is off by default.
    tool_timeout_ms: int = 30000
    tool_max_attempts: int = 1
    tool_backoff_ms: int = 0
    tool_retry_on: str = "network,timeout,5xx,429"
    tool_breaker_failures: int = 5  # consecutive failures that open the circuit of a tool
    tool_breaker_window_seconds: int = 120  # failures further apart than this do not add up
    tool_breaker_open_seconds: int = 30  # how long an open circuit refuses calls before one probe is let through
    app_origin: str = "http://localhost:5173"
    bootstrap_tenant_name: str = "Acme 科技"
    bootstrap_tenant_slug: str = "acme"
    bootstrap_admin_email: str = "admin@beework.dev"
    bootstrap_admin_name: str = "Olivia Lin"
    bootstrap_admin_password: str = "Beework-Dev-2026!"
    bootstrap_platform_admin_email: str = "platform@beework.dev"
    bootstrap_platform_admin_name: str = "BeeWork Platform Admin"
    bootstrap_platform_admin_password: str = "BeeWork-Platform-2026!"
    # Second platform admin so dual-review operations (PLT-052) can be exercised locally.
    bootstrap_platform_reviewer_email: str = "reviewer@beework.dev"
    bootstrap_platform_reviewer_name: str = "BeeWork Platform Reviewer"
    bootstrap_platform_reviewer_password: str = "BeeWork-Reviewer-2026!"
    environment: str = "development"
    # Public base URL of this API as seen by browsers (embed code, widget script); empty = derive it from the request.
    public_api_url: str = ""
    # Session lifetime and idle timeout (T-004); the tenant-level setting arrives with T-093. 15 minutes – 7 days.
    session_max_minutes: int = 720
    session_idle_minutes: int = 120
    # Third-party engines are integrated over their HTTP APIs; empty URL = not configured.
    litellm_base_url: str = ""
    litellm_master_key: str = ""
    # Which engine new knowledge bases use: "dify" (the default) or "ragflow" (optional, unverified); each knowledge base remembers its own.
    knowledge_engine: str = "dify"
    ragflow_base_url: str = ""
    ragflow_api_key: str = ""
    dify_base_url: str = ""  # includes /v1, e.g. https://api.dify.ai/v1
    dify_api_key: str = ""
    langfuse_host: str = ""
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    trace_slow_ms: int = 5000  # a run slower than this is marked "slow" in the call-trace list (T-084)
    notification_daily_hour_utc: int = 1  # the hour (UTC) at which daily notification digests go out; 01:00 UTC is 09:00 in China
    # Outbound mail (T-008). Transport "smtp" delivers via SMTP (Mailpit in development); "console"
    # only logs a masked line (handy for tests and bare local runs). Empty SMTP_USER = no auth.
    mail_transport: str = "console"  # "smtp" or "console"
    smtp_host: str = "localhost"
    smtp_port: int = 1025
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = False  # STARTTLS on the submission port (587)
    smtp_use_ssl: bool = False  # implicit TLS (465)
    # RFC 5322 From header, e.g. "BeeWork <no-reply@beework.local>".
    mail_from: str = "BeeWork <no-reply@beework.local>"
    # Where uploaded knowledge files are kept (a volume shared by the API and the Worker); see file_store.py.
    file_storage_dir: str = "/data/uploads"
    integration_timeout_seconds: float = 10.0
    model_call_timeout_seconds: float = 60.0

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @model_validator(mode="after")
    def validate_production_auth(self) -> "Settings":
        for name in ("session_max_minutes", "session_idle_minutes"):
            if not 15 <= getattr(self, name) <= 7 * 24 * 60:
                raise ValueError(f"{name.upper()} must be between 15 minutes and 7 days")
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


    def origin_allowed(self, origin: str | None) -> bool:
        """Whether a request's ``Origin`` is this application's. In development the loopback names are interchangeable
        (``localhost``, ``127.0.0.1``, ``[::1]``): opening the app by one of them must not break every write."""
        if not origin:
            return True
        origin, expected = origin.rstrip("/").lower(), self.app_origin.rstrip("/").lower()
        if origin == expected:
            return True
        if self.environment != "development":
            return False
        loopback = ("localhost", "127.0.0.1", "[::1]")
        scheme, _, rest = expected.partition("://")
        host, _, port = rest.partition(":")
        if host not in loopback:
            return False
        return any(origin == f"{scheme}://{name}{':' + port if port else ''}" for name in loopback)


settings = Settings()
