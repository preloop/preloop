"""Configuration for Preloop."""

import logging
import os
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


def _load_release_version(
    default: str = "0.8.0", version_file: Path | None = None
) -> str:
    """Load the release version.

    Uses package metadata when installed via pip (importlib.metadata).
    Falls back to the VERSION file for Docker and local dev.
    """
    try:
        return version("preloop")
    except PackageNotFoundError:
        pass

    if version_file is None:
        version_file = Path(__file__).resolve().parents[2] / "VERSION"
    try:
        v = version_file.read_text(encoding="utf-8").strip()
        if v:
            return v
    except OSError:
        logger.warning("Could not read %s, using fallback version", version_file)

    return default


# Versioning
SERVER_VERSION = _load_release_version()
MIN_CLIENT_VERSION = SERVER_VERSION
MAX_CLIENT_VERSION = SERVER_VERSION


class DatabaseSettings(BaseModel):
    """Database configuration."""

    url: str = Field(..., description="Database URL")
    pool_size: int = Field(
        20,
        description=(
            "Database connection pool size per worker process. With default "
            "max_overflow this allows up to 60 concurrent connections per worker "
            "(pool_size + max_overflow). Reduce both values on small Postgres "
            "instances or when running many workers."
        ),
    )
    max_overflow: int = Field(
        40,
        description=(
            "Maximum overflow connections beyond pool_size for each worker. "
            "Total peak connections per worker is pool_size + max_overflow."
        ),
    )
    pool_timeout: int = Field(30, description="Pool timeout in seconds")
    pool_recycle: int = Field(1800, description="Pool recycle time in seconds")


class SecuritySettings(BaseModel):
    """Security configuration."""

    secret_key: str = Field(..., description="Secret key for JWT tokens")
    encryption_key: str = Field(
        "",
        description="Fernet encryption key for sensitive data (32 url-safe base64-encoded bytes). "
        "Generate with: python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'",
    )
    token_expire_minutes: int = Field(
        30, description="Token expiration time in minutes"
    )
    algorithm: str = Field("HS256", description="JWT algorithm")


class ServerSettings(BaseModel):
    """Server configuration."""

    host: str = Field("0.0.0.0", description="Server host")
    port: int = Field(8000, description="Server port")
    debug: bool = Field(False, description="Debug mode")
    allowed_origins: list[str] = Field(["*"], description="Allowed CORS origins")


class GitHubAppSettings(BaseModel):
    """GitHub App OAuth configuration (SaaS only).

    These settings are required for GitHub App OAuth integration.
    When configured, enables "Connect with GitHub" flow for tracker creation.
    """

    app_id: str = Field("", description="GitHub App ID")
    client_id: str = Field("", description="GitHub App Client ID")
    client_secret: str = Field("", description="GitHub App Client Secret")
    private_key: str = Field(
        "", description="GitHub App Private Key (PEM format, base64 encoded)"
    )
    webhook_secret: str = Field(
        "", description="GitHub App Webhook Secret for signature verification"
    )
    slug: str = Field(
        "", description="GitHub App slug (e.g., 'preloop' or 'preloop-staging')"
    )

    @property
    def is_configured(self) -> bool:
        """Check if GitHub App is fully configured."""
        return bool(
            self.app_id
            and self.client_id
            and self.client_secret
            and self.private_key
            and self.webhook_secret
            and self.slug
        )


class GoogleOAuthSettings(BaseModel):
    """Google OAuth configuration for sign-in/sign-up."""

    client_id: str = Field("", description="Google OAuth Client ID")
    client_secret: str = Field("", description="Google OAuth Client Secret")


class GitLabOAuthSettings(BaseModel):
    """GitLab OAuth configuration for sign-in/sign-up.

    Works with GitLab.com by default. For self-hosted GitLab, set
    GITLAB_OAUTH_BASE_URL to your instance URL (e.g. https://gitlab.example.com).
    """

    client_id: str = Field("", description="GitLab OAuth Application ID")
    client_secret: str = Field("", description="GitLab OAuth Application Secret")
    base_url: str = Field(
        "https://gitlab.com",
        description="GitLab instance URL (for self-hosted)",
    )


class VaultKVV2Settings(BaseModel):
    """Vault/OpenBao-compatible KV v2 secret backend settings."""

    enabled: bool = Field(
        False, description="Enable the vault-compatible secret backend"
    )
    url: str = Field("", description="Base URL for Vault/OpenBao")
    token: str = Field("", description="Access token for the secret backend")
    namespace: str = Field("", description="Optional Vault/OpenBao namespace")
    mount: str = Field("secret", description="KV v2 mount name")
    path_prefix: str = Field("", description="Optional path prefix under the mount")
    verify_tls: bool = Field(True, description="Verify TLS certificates")
    ca_cert_path: str = Field("", description="Optional CA certificate path")
    timeout_seconds: int = Field(5, description="HTTP timeout when resolving secrets")

    @property
    def is_configured(self) -> bool:
        """Check if the vault-compatible backend is usable."""
        return bool(self.enabled and self.url and self.token and self.mount)


class Settings(BaseSettings):
    """Application settings."""

    app_name: str = Field("Preloop", description="Application name")
    version: str = Field(SERVER_VERSION, description="Application version")
    environment: str = Field(
        "development", description="Environment (development, production)"
    )
    log_level: str = Field("INFO", description="Log level")
    product_team_email: str = Field("", description="Product team email address")
    nats_url: str = Field("nats://localhost:4222", description="NATS server URL")
    preloop_url: str = Field("http://localhost:8000", description="Preloop URL")
    PROMPTS_FILE: str = Field(
        "backend/preloop/prompts.yaml",
        description="Path to the prompts YAML file",
    )

    # Feature flags for self-hosted deployments
    registration_enabled: bool = Field(
        True,
        description="Enable self-registration. Set to False to require admin invitation.",
    )

    database: DatabaseSettings
    security: SecuritySettings
    server: ServerSettings
    github_app: GitHubAppSettings = Field(
        default_factory=GitHubAppSettings,
        description="GitHub App OAuth settings (SaaS only)",
    )
    google_oauth: GoogleOAuthSettings = Field(
        default_factory=GoogleOAuthSettings,
        description="Google OAuth settings for sign-in/sign-up",
    )
    gitlab_oauth: GitLabOAuthSettings = Field(
        default_factory=GitLabOAuthSettings,
        description="GitLab OAuth settings for sign-in/sign-up",
    )
    vault_kv_v2: VaultKVV2Settings = Field(
        default_factory=VaultKVV2Settings,
        description="Optional Vault/OpenBao-compatible secret backend settings",
    )
    model_gateway_capture_content: bool = Field(
        True,
        description="Whether model gateway events may include redacted content previews",
    )
    model_gateway_auto_index_interactions: bool = Field(
        True,
        description=(
            "Whether completed model gateway interactions may be automatically indexed "
            "into the gateway semantic-search corpus"
        ),
    )
    model_gateway_auto_index_failed_interactions: bool = Field(
        False,
        description=(
            "Whether failed model gateway interactions may be automatically indexed "
            "when automatic gateway indexing is enabled"
        ),
    )
    model_gateway_upstream_backend: str = Field(
        "litellm",
        description=(
            "Upstream transport implementation used by the model gateway. "
            "Current supported value: litellm"
        ),
    )
    model_gateway_max_preview_chars: int = Field(
        32768,
        description=(
            "Maximum characters retained per message in model gateway content "
            "previews (the transcript/chat reads these). 4096 truncated large "
            "tool results (e.g. retrieved-context blobs) so the session log "
            "showed cut-off content; 32768 captures full content for typical "
            "messages. Tune via MODEL_GATEWAY_MAX_PREVIEW_CHARS; the tradeoff is "
            "stored-preview size. (Full request payloads are stored separately "
            "and untruncated; a cleaner follow-up is to read those directly.)"
        ),
    )
    flow_execution_max_wait_seconds: int = Field(
        3600,
        description="Maximum wall-clock time to wait for one flow execution before failing it",
    )

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    stripe_secret_key: str = Field(
        "",
        description="Stripe secret key",
    )
    stripe_webhook_secret: str = Field(
        "",
        description="Stripe webhook secret",
    )
    billing_trial_days: int = Field(
        14,
        description="Default Stripe trial length in days for paid SaaS plans",
    )
    billing_trial_requires_payment_method: bool = Field(
        True,
        description="Whether Stripe Checkout must collect a payment method before starting a trial",
    )
    billing_trial_hosted_model_hard_cap_usd: float = Field(
        2.0,
        description="Maximum built-in hosted model spend allowed during trialing subscriptions",
    )
    billing_session_optimization_daily_cap_usd: float = Field(
        0.5,
        description="Maximum daily per-account spend on session optimization model calls",
    )
    billing_session_title_daily_cap_usd: float = Field(
        0.25,
        description="Maximum daily per-account spend on session title generation model calls",
    )
    billing_default_extra_credit_price_per_usd: float = Field(
        1.0,
        description="Customer-facing fallback price for each additional USD of hosted-model usage",
    )
    billing_enforce_entitlements: bool = Field(
        True,
        description=(
            "Gate premium (LLM-spend) features behind an entitled subscription. "
            "Disable on self-hosted EE deployments that run the billing plugin "
            "without a SaaS paywall."
        ),
    )

    # Notification webhooks for admin alerts
    slack_webhook_url: str = Field(
        "",
        description="Slack webhook URL for admin notifications",
    )
    mattermost_webhook_url: str = Field(
        "",
        description="Mattermost webhook URL for admin notifications",
    )
    installer_audit_account_id: str = Field(
        "",
        description="Account ID used to store public installer download audit events",
    )

    @classmethod
    def from_env(cls) -> "Settings":
        """Create settings from environment variables.

        Returns:
            Settings: Application settings.
        """
        # Load required settings
        database_url = os.getenv("DATABASE_URL")
        if not database_url:
            database_url = "postgresql+psycopg://postgres:postgres@localhost/preloop"
            logger.warning(f"DATABASE_URL not set, using default: {database_url}")

        secret_key = os.getenv("SECRET_KEY")
        if not secret_key:
            env = os.getenv("ENVIRONMENT", "development")
            if env == "production":
                raise ValueError(
                    "SECRET_KEY environment variable is required in production"
                )
            secret_key = "development_secret_key_do_not_use_in_production"
            logger.warning("SECRET_KEY not set, using default development key")

        # Create database settings
        database = DatabaseSettings(
            url=database_url,
            pool_size=int(os.getenv("DATABASE_POOL_SIZE", "20")),
            max_overflow=int(os.getenv("DATABASE_MAX_OVERFLOW", "40")),
            pool_timeout=int(os.getenv("DATABASE_POOL_TIMEOUT", "30")),
            pool_recycle=int(os.getenv("DATABASE_POOL_RECYCLE", "1800")),
        )

        # Create security settings
        security = SecuritySettings(
            secret_key=secret_key,
            encryption_key=os.getenv("SECURITY__ENCRYPTION_KEY", ""),
            token_expire_minutes=int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "30")),
            algorithm=os.getenv("JWT_ALGORITHM", "HS256"),
        )

        # Create server settings
        server = ServerSettings(
            host=os.getenv("SERVER_HOST", "0.0.0.0"),
            port=int(os.getenv("SERVER_PORT", "8000")),
            debug=os.getenv("DEBUG", "False").lower() in ("true", "1", "t"),
            allowed_origins=os.getenv("ALLOWED_ORIGINS", "*").split(","),
        )

        prompts_file = os.getenv("PROMPTS_PATH", "backend/preloop/prompts.yaml")

        # Stripe configuration - no default keys for security
        # Self-hosted deployments must supply their own keys if billing is enabled
        stripe_secret_key = os.getenv("STRIPE_SECRET_KEY", "")
        stripe_webhook_secret = os.getenv("STRIPE_WEBHOOK_SECRET", "")

        # Feature flags
        registration_enabled = os.getenv("REGISTRATION_ENABLED", "true").lower() in (
            "true",
            "1",
            "t",
            "yes",
        )

        # GitHub App OAuth settings (SaaS only)
        github_app = GitHubAppSettings(
            app_id=os.getenv("GITHUB_APP_ID", ""),
            client_id=os.getenv("GITHUB_APP_CLIENT_ID", ""),
            client_secret=os.getenv("GITHUB_APP_CLIENT_SECRET", ""),
            private_key=os.getenv("GITHUB_APP_PRIVATE_KEY", ""),
            webhook_secret=os.getenv("GITHUB_APP_WEBHOOK_SECRET", ""),
            slug=os.getenv("GITHUB_APP_SLUG", ""),
        )

        # Google OAuth settings
        google_oauth = GoogleOAuthSettings(
            client_id=os.getenv("GOOGLE_OAUTH_CLIENT_ID", ""),
            client_secret=os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", ""),
        )

        # GitLab OAuth settings
        gitlab_oauth = GitLabOAuthSettings(
            client_id=os.getenv("GITLAB_OAUTH_CLIENT_ID", ""),
            client_secret=os.getenv("GITLAB_OAUTH_CLIENT_SECRET", ""),
            base_url=os.getenv("GITLAB_OAUTH_BASE_URL", "https://gitlab.com"),
        )
        vault_kv_v2 = VaultKVV2Settings(
            enabled=os.getenv("VAULT_KV_V2_ENABLED", "false").lower()
            in ("true", "1", "t", "yes"),
            url=os.getenv("VAULT_KV_V2_URL", ""),
            token=os.getenv("VAULT_KV_V2_TOKEN", ""),
            namespace=os.getenv("VAULT_KV_V2_NAMESPACE", ""),
            mount=os.getenv("VAULT_KV_V2_MOUNT", "secret"),
            path_prefix=os.getenv("VAULT_KV_V2_PATH_PREFIX", ""),
            verify_tls=os.getenv("VAULT_KV_V2_VERIFY_TLS", "true").lower()
            in ("true", "1", "t", "yes"),
            ca_cert_path=os.getenv("VAULT_KV_V2_CA_CERT_PATH", ""),
            timeout_seconds=int(os.getenv("VAULT_KV_V2_TIMEOUT_SECONDS", "5")),
        )

        return cls(
            app_name=os.getenv("APP_NAME", "Preloop"),
            environment=os.getenv("ENVIRONMENT", "development"),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            product_team_email=os.getenv("PRODUCT_TEAM_EMAIL", ""),
            nats_url=os.getenv("NATS_URL", "nats://localhost:4222"),
            PROMPTS_FILE=prompts_file,
            registration_enabled=registration_enabled,
            database=database,
            security=security,
            server=server,
            github_app=github_app,
            google_oauth=google_oauth,
            gitlab_oauth=gitlab_oauth,
            vault_kv_v2=vault_kv_v2,
            model_gateway_capture_content=os.getenv(
                "MODEL_GATEWAY_CAPTURE_CONTENT", "true"
            ).lower()
            in ("true", "1", "t", "yes"),
            model_gateway_auto_index_interactions=os.getenv(
                "MODEL_GATEWAY_AUTO_INDEX_INTERACTIONS", "true"
            ).lower()
            in ("true", "1", "t", "yes"),
            model_gateway_auto_index_failed_interactions=os.getenv(
                "MODEL_GATEWAY_AUTO_INDEX_FAILED_INTERACTIONS", "false"
            ).lower()
            in ("true", "1", "t", "yes"),
            model_gateway_max_preview_chars=int(
                os.getenv("MODEL_GATEWAY_MAX_PREVIEW_CHARS", "32768")
            ),
            flow_execution_max_wait_seconds=int(
                os.getenv("FLOW_EXECUTION_MAX_WAIT_SECONDS", "3600")
            ),
            stripe_secret_key=stripe_secret_key,
            stripe_webhook_secret=stripe_webhook_secret,
            billing_trial_days=int(os.getenv("BILLING_TRIAL_DAYS", "14")),
            billing_trial_requires_payment_method=os.getenv(
                "BILLING_TRIAL_REQUIRES_PAYMENT_METHOD", "true"
            ).lower()
            in ("true", "1", "t", "yes"),
            billing_trial_hosted_model_hard_cap_usd=float(
                os.getenv("BILLING_TRIAL_HOSTED_MODEL_HARD_CAP_USD", "2.0")
            ),
            billing_session_optimization_daily_cap_usd=float(
                os.getenv("BILLING_SESSION_OPTIMIZATION_DAILY_CAP_USD", "0.5")
            ),
            billing_session_title_daily_cap_usd=float(
                os.getenv("BILLING_SESSION_TITLE_DAILY_CAP_USD", "0.25")
            ),
            billing_default_extra_credit_price_per_usd=float(
                os.getenv("BILLING_DEFAULT_EXTRA_CREDIT_PRICE_PER_USD", "1.0")
            ),
            billing_enforce_entitlements=os.getenv(
                "BILLING_ENFORCE_ENTITLEMENTS", "true"
            ).lower()
            in ("true", "1", "t", "yes"),
            installer_audit_account_id=os.getenv("INSTALLER_AUDIT_ACCOUNT_ID", ""),
        )


def get_settings() -> Settings:
    """Get application settings.

    Returns:
        Settings: Application settings.
    """
    return Settings.from_env()


# Create settings instance
settings = get_settings()
