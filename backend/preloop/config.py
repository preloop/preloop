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
        10,
        description=(
            "Database connection pool size per worker process. With default "
            "max_overflow this allows up to 30 concurrent connections per pool "
            "(pool_size + max_overflow), and a process builds two pools plus a "
            "one-connection health engine. Reduce both values on small Postgres "
            "instances or when running many workers."
        ),
    )
    max_overflow: int = Field(
        20,
        description=(
            "Maximum overflow connections beyond pool_size for each worker. "
            "Total peak connections per worker is pool_size + max_overflow."
        ),
    )
    pool_timeout: int = Field(
        5,
        description=(
            "Seconds a request waits for a pooled connection before failing "
            "with 503. Short on purpose: a saturated pool should shed load "
            "rather than hold requests open."
        ),
    )
    pool_recycle: int = Field(1800, description="Pool recycle time in seconds")


class SecuritySettings(BaseModel):
    """Security configuration."""

    secret_key: str = Field(..., description="Secret key for JWT tokens")
    encryption_key: str = Field(
        "",
        description="Fernet encryption key for sensitive data (32 url-safe base64-encoded bytes). "
        "Generate with: python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'",
    )
    # NOTE: Access-token TTL is actually enforced in preloop/api/auth/jwt.py,
    # which reads the ACCESS_TOKEN_EXPIRE_MINUTES env var directly (default
    # 1440 = 24h). This setting mirrors that default for documentation and
    # future consumers; changing it here alone does NOT change live token
    # lifetimes.
    token_expire_minutes: int = Field(
        1440, description="Access token expiration time in minutes (24h default)"
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


class OtlpSettings(BaseModel):
    """Optional OTLP export for gateway and MCP telemetry.

    Disabled by default. When enabled, Preloop exports GenAI spans (and
    duration metrics) to the configured collector or vendor OTLP endpoint.
    """

    enabled: bool = Field(False, description="Enable OTLP export")
    endpoint: str = Field(
        "",
        description=(
            "OTLP endpoint. HTTP protocols append /v1/traces (and /v1/metrics) "
            "when those suffixes are missing. gRPC uses host:port."
        ),
    )
    protocol: str = Field(
        "http/protobuf",
        description="OTLP protocol: http/protobuf or grpc",
    )
    headers: str = Field(
        "",
        description=(
            "OTLP headers as key=value pairs separated by commas "
            "(vendor ingest keys, for example Langfuse Basic auth or a "
            "Datadog API key header)"
        ),
    )
    service_name: str = Field("preloop", description="Resource service.name")
    service_namespace: str = Field("", description="Resource service.namespace")
    deployment_environment: str = Field(
        "",
        description="Resource deployment.environment (falls back to ENVIRONMENT)",
    )
    sampler_ratio: float = Field(
        1.0,
        description=(
            "Parent-based TraceIdRatioBased sampler ratio in [0, 1]. "
            "Use a lower ratio or collector-side sampling when volume is high."
        ),
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
    bootstrap_token: str = Field(
        "",
        description=(
            "First-user setup token (PRELOOP_BOOTSTRAP_TOKEN). While the "
            "instance has zero users and this is set, /register requires the "
            "token regardless of registration_enabled. Ignored once any user "
            "exists."
        ),
    )
    disable_rbac: bool = Field(
        False,
        description=(
            "Disable proprietary RBAC permission checks and plugin loading. "
            "Set via DISABLE_RBAC=true for OSS / unrestricted access."
        ),
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
    otlp: OtlpSettings = Field(
        default_factory=OtlpSettings,
        description="Optional OTLP export for gateway and MCP telemetry",
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
    model_gateway_upstream_retry_max_attempts: int = Field(
        3,
        description=(
            "Total attempts (1 initial + retries) the gateway makes for ONE "
            "upstream model call when the provider fails transiently: "
            "mid-stream disconnect, provider_unavailable, network error, "
            "overload, or a non-terminal 429. Auth, quota and request errors "
            "are never retried. Set to 1 to disable."
        ),
    )
    model_gateway_upstream_retry_base_seconds: float = Field(
        0.2,
        description=(
            "Base backoff between upstream retry attempts. Doubles per "
            "attempt and carries jitter; a provider Retry-After hint raises "
            "it, capped by MODEL_GATEWAY_UPSTREAM_RETRY_AFTER_CAP_SECONDS."
        ),
    )
    model_gateway_upstream_retry_after_cap_seconds: float = Field(
        8.0,
        description=(
            "Ceiling applied to a provider Retry-After hint, so one hostile "
            "or mistaken header cannot stall a gateway worker."
        ),
    )
    runtime_session_idle_timeout_minutes: int = Field(
        720,
        description=(
            "Idle window after which a gateway runtime session is considered "
            "finished, so the next request opens a NEW session row instead of "
            "appending to a stale one. This is the honest fallback for agents "
            "that put no session id on the wire (Gemini CLI, Hermes, OpenClaw's "
            "Anthropic transport): without it their sessions grow forever. It is "
            "a safety net only — a native session id always wins, so agents that "
            "do identify their conversation (Claude Code, Codex, OpenCode, and "
            "anything sending X-Preloop-Session-Id or prompt_cache_key) are "
            "split by that id and are unaffected by this timeout. Set to 0 to "
            "disable the closer entirely and restore the previous "
            "never-ending-session behavior."
        ),
    )
    model_gateway_claude_family_autoregister_enabled: bool = Field(
        True,
        description=(
            "When an Anthropic-protocol gateway request over a Claude Code "
            "subscription-OAuth credential asks for a claude-* model the "
            "account has not registered (e.g. a new dated snapshot shipped "
            "by a Claude Code update, or a family the onboarding import "
            "missed), auto-register the model against the same OAuth "
            "credential and bind it to the requesting managed agent instead "
            "of answering 404 model_not_authorized. Anthropic itself remains "
            "the authorization boundary for what the subscription may use; "
            "subject-scoped allowed_models checks still apply afterwards."
        ),
    )
    model_gateway_codex_family_autoregister_enabled: bool = Field(
        True,
        description=(
            "When an OpenAI-protocol gateway request over a Codex ChatGPT "
            "subscription-OAuth credential asks for a gpt-*/o-series/"
            "chatgpt-* model the account has not registered (e.g. gpt-6-astra "
            "after a Codex CLI update), auto-register the model against the "
            "same OAuth credential and bind it to the requesting managed "
            "agent instead of answering 404. OpenAI itself remains the "
            "authorization boundary for what the subscription may use; "
            "subject-scoped allowed_models checks still apply afterwards. "
            "preloop models sync cannot cover this path: those credentials "
            "cannot authenticate server-side listing."
        ),
    )
    model_price_live_lookup_enabled: bool = Field(
        True,
        description=(
            "When a gateway request records an unpriced model, fetch its "
            "price from the live upstream price map once in the background "
            "and re-price the row. Unknown models are negative-cached for a "
            "day so repeated traffic never re-triggers lookups."
        ),
    )
    model_catalog_sync_scheduled_enabled: bool = Field(
        False,
        description=(
            "Schedule the automatic model-catalog sync (the scheduled "
            "equivalent of 'preloop models sync'): periodically discover "
            "newly released provider models with stored API-key credentials "
            "and add them to each account catalog. Principal-bound "
            "subscription-OAuth credentials (Claude Code / Codex) are never "
            "used. Default off so self-hosted catalogs never change on "
            "upgrade without an explicit opt-in; set "
            "MODEL_CATALOG_SYNC_SCHEDULED_ENABLED=true (helm: "
            "config.modelCatalogSync.scheduledEnabled) to enable."
        ),
    )
    model_catalog_sync_interval_hours: int = Field(
        24,
        description=(
            "How often the scheduled model-catalog sync runs, in hours. "
            "Only meaningful when model_catalog_sync_scheduled_enabled is "
            "true (helm: config.modelCatalogSync.intervalHours)."
        ),
    )
    provider_billing_sync_enabled: bool = Field(
        True,
        description=(
            "Schedule the daily provider-billing ingestion task (cost "
            "reconciliation). The task no-ops unless the Enterprise billing "
            "plugin and at least one provider connection are configured."
        ),
    )
    provider_billing_drift_alert_pct: float = Field(
        10.0,
        description=(
            "Absolute percentage drift between provider-reported cost and "
            "Preloop's estimated cost (per provider, per day) above which a "
            "reconciliation drift alert is sent to the account owner. "
            "Set to 0 or a negative value to disable drift alerting."
        ),
    )
    provider_billing_drift_alert_min_usd: float = Field(
        1.0,
        description=(
            "Minimum provider-reported daily cost (USD) required before a "
            "reconciliation drift alert may fire; avoids noisy alerts on "
            "penny-sized spend where drift percentages are meaningless."
        ),
    )
    flow_artifact_expanded_max_bytes: int = Field(2 * 1024**3, ge=1)
    flow_artifact_account_quota_bytes: int = Field(4 * 1024**3, ge=1)
    flow_native_session_retention_hours: int = Field(168, ge=0)
    flow_checkpoint_interval_seconds: int = Field(300, ge=30)
    flow_artifact_direct_upload: bool = False
    flow_environment_profiles_file: str = ""

    workspace_snapshot_max_bytes: int = Field(
        512 * 1024 * 1024,
        description=(
            "Cap on the workspace snapshot (tar.gz of /workspace) captured at "
            "the end of every hosted flow run so an execution that failed "
            "before pushing can be restored. Workspaces larger than this are "
            "skipped with a logged reason. On Kubernetes the snapshot travels "
            "through the pod log stream and is additionally capped at 2 MiB "
            "(K8S_WORKSPACE_STREAM_MAX_BYTES)."
        ),
    )
    workspace_snapshot_ttl_hours: int = Field(
        24,
        description=(
            "How long captured workspace snapshots (and Docker "
            "agent-workspace-* volumes) are retained before the janitor "
            "deletes them, in hours. 0 disables retention: snapshots are "
            "deleted on the next janitor pass "
            "(WORKSPACE_SNAPSHOT_TTL_HOURS)."
        ),
    )
    cost_digest_enabled: bool = Field(
        True,
        description=(
            "Schedule the weekly cost optimization & savings digest email. "
            "The task no-ops unless the Enterprise billing plugin is "
            "installed."
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
    model_gateway_activity_max_body_chars: int = Field(
        8192,
        description=(
            "Maximum characters retained per string inside the request and "
            "response bodies embedded in runtime_session_activity metadata "
            "(JSONB). This is deliberately tighter than "
            "MODEL_GATEWAY_MAX_PREVIEW_CHARS because the UI never renders "
            "these bodies in full: the transcript reads conversation_preview, "
            "and the only direct consumers take a 300-character substring for "
            "the activity preview or read request.tools. A gateway request "
            "that returned a binary body once produced a 533KB activity row, "
            "which is a database bloat and query-latency problem independent "
            "of encoding. Tune via MODEL_GATEWAY_ACTIVITY_MAX_BODY_CHARS."
        ),
    )
    flow_execution_max_wait_seconds: int = Field(
        3600,
        description="Maximum wall-clock time to wait for one flow execution before failing it",
    )
    flow_execution_max_attempts: int = Field(
        2,
        description=(
            "Maximum agent attempts per flow execution. Attempts beyond the "
            "first are only made when the failure was a transient upstream "
            "model-provider error (timeout, overload, throttling) and the "
            "failed attempt produced no external side effects. Set to 1 to "
            "disable flow-level retries."
        ),
    )
    flow_execution_retry_backoff_seconds: int = Field(
        15,
        description=(
            "Base backoff before retrying a flow execution attempt. Doubles "
            "per attempt, giving an overloaded provider time to recover."
        ),
    )
    agent_job_create_max_attempts: int = Field(
        3,
        description=(
            "Attempts to create the Kubernetes agent Job before failing the "
            "execution. Covers 409 AlreadyExists (a leftover Job from an "
            "earlier session of the same execution, or a duplicate dispatch) "
            "and 429/5xx from the API server. Set to 1 to disable."
        ),
    )
    agent_job_create_retry_base_seconds: float = Field(
        0.5,
        description=(
            "Base backoff before re-attempting Kubernetes agent Job "
            "creation. Doubles per attempt and carries jitter, so concurrent "
            "dispatchers do not retry in lockstep."
        ),
    )
    flow_confirmation_nudge_max_tokens: int = Field(
        4096,
        description=(
            "Token ceiling for the one-shot confirmation round (layer 2 of "
            "the completion contract). Bounds the prior-context excerpt "
            "embedded in the nudge prompt (~4 chars/token) and is passed to "
            "the nudge session as model_parameters.max_output_tokens for "
            "runtimes that honor it. The nudge only asks the agent to "
            "confirm or deny completion, so it should stay small."
        ),
    )
    flow_confirmation_nudge_timeout_seconds: int = Field(
        300,
        description=(
            "Maximum wall-clock time to wait for the one-shot confirmation "
            "round before failing closed with the standard "
            "missing-confirmation message."
        ),
    )
    flow_completion_nudge_enabled: bool = Field(
        True,
        description=(
            "When true, agent scripts carry the in-place completion nudge: "
            "after a clean harness exit with no completion signal, the same "
            "container re-invokes the same harness session once with a short "
            "reminder to write result.json and print the sentinel. Runs "
            "before the container's post-execution git block, so it can "
            "never re-run a push. Set to false to disable fleet-wide."
        ),
    )
    flow_completion_nudge_timeout_seconds: int = Field(
        300,
        description=(
            "Wall clock for the in-place completion nudge round inside the "
            "agent container. The round is one short reminder, so this "
            "should stay small; when it expires the run falls back to the "
            "standard missing-confirmation handling."
        ),
    )
    flow_execution_worker_enabled: bool = Field(
        False,
        description=(
            "When true, flow orchestration runs on sync workers via JetStream "
            "(execute_flow / resume_flow_execution) instead of asyncio.create_task "
            "in the API or webhook worker process."
        ),
    )
    flow_execution_claim_stale_seconds: int = Field(
        120,
        description=(
            "Seconds after the last orchestrator heartbeat before another worker "
            "may reclaim an active flow execution."
        ),
    )
    flow_execution_reclaim_interval_seconds: int = Field(
        30,
        description=(
            "How often flow-execution workers re-dispatch stale/unclaimed "
            "active executions (deploy handoff safety net)."
        ),
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
    billing_budget_notification_workers: int = Field(
        4,
        description=(
            "Thread-pool size for async budget-limit notification delivery "
            "(BILLING_BUDGET_NOTIFICATION_WORKERS)"
        ),
    )
    billing_budget_notification_queue_size: int = Field(
        32,
        description=(
            "Max in-flight + queued budget notifications before new ones are "
            "dropped (BILLING_BUDGET_NOTIFICATION_QUEUE_SIZE)"
        ),
    )
    billing_free_hosted_model_hard_cap_usd: float = Field(
        1.0,
        description=(
            "Maximum built-in hosted model spend per calendar month for "
            "accounts with no subscription (card-free free tier)"
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
    agent_control_command_ttl_seconds: int = Field(
        3600,
        description=(
            "Seconds an undelivered Agent Control command stays pending "
            "(eligible for redelivery on agent reconnect) before the expiry "
            "pass marks it expired"
        ),
    )
    agent_control_allow_query_token: bool = Field(
        True,
        description=(
            "Allow Agent Control WebSockets to authenticate via ?token= "
            "(leaks into access logs). Set false in production once clients "
            "send Authorization: Bearer."
        ),
    )
    billing_budget_default_estimated_output_tokens: int = Field(
        1024,
        description=(
            "Default estimated completion tokens used for gateway budget "
            "preflight when the request omits max_tokens"
        ),
    )
    billing_budget_chars_per_token: float = Field(
        4.0,
        description=(
            "Chars-per-token heuristic divisor for gateway budget preflight "
            "input estimates"
        ),
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
            # Keep in sync with models/db/session.py, the actual consumer.
            pool_size=int(os.getenv("DATABASE_POOL_SIZE", "10")),
            max_overflow=int(os.getenv("DATABASE_MAX_OVERFLOW", "20")),
            pool_timeout=int(os.getenv("DATABASE_POOL_TIMEOUT", "5")),
            pool_recycle=int(os.getenv("DATABASE_POOL_RECYCLE", "1800")),
        )

        # Create security settings
        security = SecuritySettings(
            secret_key=secret_key,
            encryption_key=os.getenv("SECURITY__ENCRYPTION_KEY", ""),
            # Keep in sync with preloop/api/auth/jwt.py, the actual consumer
            # of ACCESS_TOKEN_EXPIRE_MINUTES (env default 1440 there too).
            token_expire_minutes=int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "1440")),
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
        disable_rbac = os.getenv("DISABLE_RBAC", "false").lower() in (
            "true",
            "1",
            "t",
            "yes",
        )
        bootstrap_token = os.getenv("PRELOOP_BOOTSTRAP_TOKEN", "")

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
        otlp_ratio_raw = os.getenv("OTLP_SAMPLER_RATIO", "1.0")
        try:
            otlp_sampler_ratio = float(otlp_ratio_raw)
        except ValueError:
            otlp_sampler_ratio = 1.0
        otlp = OtlpSettings(
            enabled=os.getenv("OTLP_ENABLED", "false").lower()
            in ("true", "1", "t", "yes"),
            endpoint=os.getenv("OTLP_ENDPOINT")
            or os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", ""),
            protocol=os.getenv("OTLP_PROTOCOL")
            or os.getenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf"),
            headers=os.getenv("OTLP_HEADERS")
            or os.getenv("OTEL_EXPORTER_OTLP_HEADERS", ""),
            service_name=os.getenv("OTLP_SERVICE_NAME")
            or os.getenv("OTEL_SERVICE_NAME", "preloop"),
            service_namespace=os.getenv("OTLP_SERVICE_NAMESPACE", ""),
            deployment_environment=os.getenv("OTLP_DEPLOYMENT_ENVIRONMENT", ""),
            sampler_ratio=otlp_sampler_ratio,
        )

        return cls(
            app_name=os.getenv("APP_NAME", "Preloop"),
            environment=os.getenv("ENVIRONMENT", "development"),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            product_team_email=os.getenv("PRODUCT_TEAM_EMAIL", ""),
            nats_url=os.getenv("NATS_URL", "nats://localhost:4222"),
            PROMPTS_FILE=prompts_file,
            registration_enabled=registration_enabled,
            bootstrap_token=bootstrap_token,
            disable_rbac=disable_rbac,
            database=database,
            security=security,
            server=server,
            github_app=github_app,
            google_oauth=google_oauth,
            gitlab_oauth=gitlab_oauth,
            vault_kv_v2=vault_kv_v2,
            otlp=otlp,
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
            model_gateway_activity_max_body_chars=int(
                os.getenv("MODEL_GATEWAY_ACTIVITY_MAX_BODY_CHARS", "8192")
            ),
            flow_execution_max_wait_seconds=int(
                os.getenv("FLOW_EXECUTION_MAX_WAIT_SECONDS", "3600")
            ),
            flow_execution_max_attempts=int(
                os.getenv("FLOW_EXECUTION_MAX_ATTEMPTS", "2")
            ),
            flow_execution_retry_backoff_seconds=int(
                os.getenv("FLOW_EXECUTION_RETRY_BACKOFF_SECONDS", "15")
            ),
            agent_job_create_max_attempts=int(
                os.getenv("AGENT_JOB_CREATE_MAX_ATTEMPTS", "3")
            ),
            agent_job_create_retry_base_seconds=float(
                os.getenv("AGENT_JOB_CREATE_RETRY_BASE_SECONDS", "0.5")
            ),
            model_gateway_upstream_retry_max_attempts=int(
                os.getenv("MODEL_GATEWAY_UPSTREAM_RETRY_MAX_ATTEMPTS", "3")
            ),
            model_gateway_upstream_retry_base_seconds=float(
                os.getenv("MODEL_GATEWAY_UPSTREAM_RETRY_BASE_SECONDS", "0.2")
            ),
            model_gateway_upstream_retry_after_cap_seconds=float(
                os.getenv("MODEL_GATEWAY_UPSTREAM_RETRY_AFTER_CAP_SECONDS", "8.0")
            ),
            flow_confirmation_nudge_max_tokens=int(
                os.getenv("FLOW_CONFIRMATION_NUDGE_MAX_TOKENS", "4096")
            ),
            flow_confirmation_nudge_timeout_seconds=int(
                os.getenv("FLOW_CONFIRMATION_NUDGE_TIMEOUT_SECONDS", "300")
            ),
            flow_completion_nudge_enabled=os.getenv(
                "FLOW_COMPLETION_NUDGE_ENABLED", "true"
            ).lower()
            in ("true", "1", "t", "yes"),
            flow_completion_nudge_timeout_seconds=int(
                os.getenv("FLOW_COMPLETION_NUDGE_TIMEOUT_SECONDS", "300")
            ),
            flow_execution_worker_enabled=os.getenv(
                "FLOW_EXECUTION_WORKER_ENABLED", "false"
            ).lower()
            in ("true", "1", "t", "yes"),
            flow_execution_claim_stale_seconds=int(
                os.getenv("FLOW_EXECUTION_CLAIM_STALE_SECONDS", "120")
            ),
            flow_execution_reclaim_interval_seconds=int(
                os.getenv("FLOW_EXECUTION_RECLAIM_INTERVAL_SECONDS", "30")
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
            billing_budget_notification_workers=int(
                os.getenv("BILLING_BUDGET_NOTIFICATION_WORKERS", "4")
            ),
            billing_budget_notification_queue_size=int(
                os.getenv("BILLING_BUDGET_NOTIFICATION_QUEUE_SIZE", "32")
            ),
            billing_free_hosted_model_hard_cap_usd=float(
                os.getenv("BILLING_FREE_HOSTED_MODEL_HARD_CAP_USD", "1.0")
            ),
            installer_audit_account_id=os.getenv("INSTALLER_AUDIT_ACCOUNT_ID", ""),
            agent_control_command_ttl_seconds=int(
                os.getenv("AGENT_CONTROL_COMMAND_TTL_SECONDS", "3600")
            ),
            agent_control_allow_query_token=os.getenv(
                "AGENT_CONTROL_ALLOW_QUERY_TOKEN", "true"
            ).lower()
            in ("true", "1", "t", "yes"),
            billing_budget_default_estimated_output_tokens=int(
                os.getenv("BILLING_BUDGET_DEFAULT_ESTIMATED_OUTPUT_TOKENS", "1024")
            ),
            billing_budget_chars_per_token=float(
                os.getenv("BILLING_BUDGET_CHARS_PER_TOKEN", "4.0")
            ),
        )


def get_settings() -> Settings:
    """Get application settings.

    Returns:
        Settings: Application settings.
    """
    return Settings.from_env()


# Create settings instance
settings = get_settings()
