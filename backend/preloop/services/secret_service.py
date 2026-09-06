"""Provider-agnostic secret resolution service."""

from __future__ import annotations

from base64 import urlsafe_b64decode
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
from typing import Any, Callable, Dict, Optional, Protocol
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request
from uuid import UUID

from sqlalchemy.orm import Session

from preloop.config import settings
from preloop.models.crud.secret_reference import crud_secret_reference
from preloop.models import models

from preloop.utils.encryption import decrypt_value, encrypt_value

AIModel = models.AIModel
SecretReference = models.SecretReference

logger = logging.getLogger(__name__)


LOCAL_ENCRYPTED_BACKEND = "local_encrypted"
VAULT_KV_V2_BACKEND = "vault_kv_v2"
OPENBAO_KV_V2_BACKEND = "openbao_kv_v2"
AI_MODEL_API_KEY_SECRET_KIND = "ai_model_api_key"
AI_MODEL_CREDENTIALS_SECRET_KIND = "ai_model_credentials"
# Provider ORGANIZATION admin keys (OpenAI Admin key / Anthropic Admin key)
# used to fetch billing/usage actuals — distinct from inference credentials.
PROVIDER_ADMIN_CREDENTIALS_SECRET_KIND = "provider_admin_credentials"
OPENAI_CODEX_OAUTH_CREDENTIAL_TYPE = "oauth_openai_codex"
ANTHROPIC_CLAUDE_CODE_OAUTH_CREDENTIAL_TYPE = "oauth_anthropic_claude_code"
# Credential types bound to a human principal's subscription (Claude Code /
# Codex OAuth). They only authorize that principal's own interactive traffic,
# so Preloop must never auto-select them for server-side generation.
PRINCIPAL_BOUND_OAUTH_CREDENTIAL_TYPES = frozenset(
    {
        OPENAI_CODEX_OAUTH_CREDENTIAL_TYPE,
        ANTHROPIC_CLAUDE_CODE_OAUTH_CREDENTIAL_TYPE,
    }
)
OPENAI_CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
OPENAI_CODEX_TOKEN_URL = "https://auth.openai.com/oauth/token"
OPENAI_CODEX_JWT_CLAIM_PATH = "https://api.openai.com/auth"
OPENAI_CODEX_REFRESH_SKEW_MS = 60_000
ANTHROPIC_CLAUDE_CODE_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
ANTHROPIC_CLAUDE_CODE_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
ANTHROPIC_CLAUDE_CODE_REFRESH_SKEW_MS = 60_000
# platform.claude.com sits behind Cloudflare, which rejects requests whose
# signature does not look like the real Claude Code client with HTTP 403
# "error code: 1010" (browser-signature ban) — BEFORE the OAuth handler runs.
# A bare urllib form-POST is blocked; sending a JSON body plus a Claude
# Code-style User-Agent passes. Keep this UA in sync with a plausible
# Claude Code release string.
ANTHROPIC_CLAUDE_CODE_USER_AGENT = "claude-cli/2.1.168 (external, cli)"


@dataclass
class ResolvedSecret:
    """Resolved secret value plus non-sensitive metadata."""

    value: str
    backend_type: str
    secret_reference_id: Optional[UUID] = None


@dataclass
class ResolvedModelCredentials:
    """Resolved model credentials with logical type information."""

    credential_type: str
    backend_type: str
    value: Optional[str] = None
    payload: Optional[Dict[str, Any]] = None
    secret_reference_id: Optional[UUID] = None


class CredentialRefreshError(ValueError):
    """Raised when provider-managed OAuth credentials cannot be refreshed."""

    def __init__(
        self,
        message: str,
        *,
        provider: str,
        status_code: Optional[int] = None,
        code: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.status_code = status_code
        self.code = code

    def safe_summary(self) -> str:
        """Return a DB-safe summary that never embeds provider response bodies."""
        status = self.status_code if self.status_code is not None else "?"
        code = self.code or "unknown"
        return f"{self.provider} refresh failed (status={status}, code={code})"


class SecretBackend(Protocol):
    """Interface for secret backend adapters."""

    backend_type: str

    def validate_reference(
        self,
        *,
        external_ref: Optional[str],
        meta_data: Optional[dict],
    ) -> None:
        """Validate a backend-specific reference before storing it."""

    def resolve(self, secret_ref: SecretReference) -> str:
        """Resolve the secret value from the backend."""


class LocalEncryptedSecretBackend:
    """Local encrypted secret backend stored in the database."""

    backend_type = LOCAL_ENCRYPTED_BACKEND

    def validate_reference(
        self,
        *,
        external_ref: Optional[str],
        meta_data: Optional[dict],
    ) -> None:
        if external_ref:
            raise ValueError("local_encrypted backend does not accept external_ref")

    def resolve(self, secret_ref: SecretReference) -> str:
        if not secret_ref.encrypted_value:
            return ""
        return decrypt_value(secret_ref.encrypted_value)


class VaultKVV2SecretBackend:
    """Vault/OpenBao-compatible KV v2 secret backend."""

    def __init__(self, backend_type: str = VAULT_KV_V2_BACKEND) -> None:
        self.backend_type = backend_type

    def validate_reference(
        self,
        *,
        external_ref: Optional[str],
        meta_data: Optional[dict],
    ) -> None:
        if not settings.vault_kv_v2.is_configured:
            raise ValueError(
                f"{self.backend_type} backend is not configured on this Preloop instance"
            )
        if not external_ref:
            raise ValueError(f"{self.backend_type} requires credentials_external_ref")
        field = (meta_data or {}).get("field")
        if field is not None and not isinstance(field, str):
            raise ValueError("credentials_meta_data.field must be a string")
        self._normalize_external_ref(external_ref)

    def resolve(self, secret_ref: SecretReference) -> str:
        self.validate_reference(
            external_ref=secret_ref.external_ref,
            meta_data=secret_ref.meta_data,
        )
        path = self._build_secret_path(secret_ref.external_ref or "")
        payload = self._read_secret(path, secret_ref.meta_data or {})
        data = payload.get("data", {}).get("data", {})
        field = (secret_ref.meta_data or {}).get("field", "value")
        value = data.get(field)
        if value is None:
            raise ValueError(
                f"Secret field '{field}' not found for backend {self.backend_type}"
            )
        return str(value)

    def _build_secret_path(self, external_ref: str) -> str:
        raw_ref = self._normalize_external_ref(external_ref)
        prefix = settings.vault_kv_v2.path_prefix.strip().strip("/")
        path = f"{prefix}/{raw_ref}" if prefix else raw_ref
        return f"{settings.vault_kv_v2.mount.strip('/')}/data/{path}"

    @staticmethod
    def _normalize_external_ref(external_ref: str) -> str:
        raw_ref = external_ref.strip()
        if not raw_ref:
            raise ValueError("credentials_external_ref must not be empty")
        if raw_ref != urllib_parse.unquote(raw_ref):
            raise ValueError(
                "credentials_external_ref must not contain percent-encoded characters"
            )

        normalized_parts: list[str] = []
        for part in raw_ref.split("/"):
            normalized_part = part.strip()
            if normalized_part in {"", ".", ".."}:
                raise ValueError(
                    "credentials_external_ref must be a relative secret path "
                    "without empty or traversal segments"
                )
            if "\\" in normalized_part:
                raise ValueError(
                    "credentials_external_ref must use forward slashes only"
                )
            if any(ord(char) < 32 for char in normalized_part):
                raise ValueError(
                    "credentials_external_ref must not contain control characters"
                )
            normalized_parts.append(normalized_part)

        return "/".join(normalized_parts)

    def _read_secret(self, path: str, meta_data: dict) -> dict:
        base_url = settings.vault_kv_v2.url.rstrip("/")
        params = {}
        if meta_data.get("version") is not None:
            params["version"] = str(meta_data["version"])
        query = f"?{urllib_parse.urlencode(params)}" if params else ""
        url = f"{base_url}/v1/{path}{query}"
        req = urllib_request.Request(
            url,
            headers=self._build_headers(),
            method="GET",
        )
        ssl_context = None
        if not settings.vault_kv_v2.verify_tls:
            import ssl

            ssl_context = ssl._create_unverified_context()
        elif settings.vault_kv_v2.ca_cert_path:
            import ssl

            ssl_context = ssl.create_default_context(
                cafile=settings.vault_kv_v2.ca_cert_path
            )

        try:
            with urllib_request.urlopen(
                req,
                timeout=settings.vault_kv_v2.timeout_seconds,
                context=ssl_context,
            ) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib_error.HTTPError as exc:
            raise ValueError(
                f"{self.backend_type} secret lookup failed with status {exc.code}"
            ) from exc
        except urllib_error.URLError as exc:
            raise ValueError(
                f"{self.backend_type} secret lookup failed: {exc.reason}"
            ) from exc

    def _build_headers(self) -> dict[str, str]:
        headers = {"X-Vault-Token": settings.vault_kv_v2.token}
        if settings.vault_kv_v2.namespace:
            headers["X-Vault-Namespace"] = settings.vault_kv_v2.namespace
        return headers


class SecretService:
    """Secret storage and resolution service."""

    def __init__(self) -> None:
        self._backends: dict[str, SecretBackend] = {
            LOCAL_ENCRYPTED_BACKEND: LocalEncryptedSecretBackend(),
        }
        if settings.vault_kv_v2.is_configured:
            self._backends[VAULT_KV_V2_BACKEND] = VaultKVV2SecretBackend(
                VAULT_KV_V2_BACKEND
            )
            self._backends[OPENBAO_KV_V2_BACKEND] = VaultKVV2SecretBackend(
                OPENBAO_KV_V2_BACKEND
            )

    def create_local_secret_reference(
        self,
        db: Session,
        *,
        account_id: Optional[UUID],
        name: str,
        secret_kind: str,
        secret_value: str,
        existing_secret_id: Optional[UUID] = None,
        meta_data: Optional[dict] = None,
    ) -> SecretReference:
        """Create or update a locally encrypted secret reference."""
        encrypted_value = encrypt_value(secret_value)
        now = datetime.now(timezone.utc)

        if existing_secret_id:
            secret_ref = crud_secret_reference.get(db, id=existing_secret_id)
            if secret_ref:
                secret_ref.name = name
                secret_ref.backend_type = LOCAL_ENCRYPTED_BACKEND
                secret_ref.secret_kind = secret_kind
                secret_ref.encrypted_value = encrypted_value
                secret_ref.status = "active"
                secret_ref.last_verified_at = now
                secret_ref.meta_data = meta_data or {}
                db.add(secret_ref)
                db.commit()
                db.refresh(secret_ref)
                return secret_ref

        secret_ref = crud_secret_reference.create(
            db,
            obj_in={
                "account_id": account_id,
                "name": name,
                "backend_type": LOCAL_ENCRYPTED_BACKEND,
                "secret_kind": secret_kind,
                "encrypted_value": encrypted_value,
                "status": "active",
                "last_verified_at": now,
                "meta_data": meta_data or {},
                "created_at": now,
                "updated_at": now,
            },
        )
        return secret_ref

    def create_external_secret_reference(
        self,
        db: Session,
        *,
        account_id: Optional[UUID],
        name: str,
        secret_kind: str,
        backend_type: str,
        external_ref: str,
        meta_data: Optional[dict] = None,
        existing_secret_id: Optional[UUID] = None,
    ) -> SecretReference:
        """Create or update a secret reference backed by an external secret store."""
        backend = self._backends.get(backend_type)
        if not backend:
            raise ValueError(f"Unsupported secret backend: {backend_type}")
        backend.validate_reference(external_ref=external_ref, meta_data=meta_data)
        now = datetime.now(timezone.utc)

        if existing_secret_id:
            secret_ref = crud_secret_reference.get(db, id=existing_secret_id)
            if secret_ref:
                secret_ref.name = name
                secret_ref.backend_type = backend_type
                secret_ref.secret_kind = secret_kind
                secret_ref.encrypted_value = None
                secret_ref.external_ref = external_ref
                secret_ref.status = "active"
                secret_ref.last_verified_at = now
                secret_ref.meta_data = meta_data or {}
                db.add(secret_ref)
                db.commit()
                db.refresh(secret_ref)
                return secret_ref

        return crud_secret_reference.create(
            db,
            obj_in={
                "account_id": account_id,
                "name": name,
                "backend_type": backend_type,
                "secret_kind": secret_kind,
                "encrypted_value": None,
                "external_ref": external_ref,
                "status": "active",
                "last_verified_at": now,
                "meta_data": meta_data or {},
                "created_at": now,
                "updated_at": now,
            },
        )

    def resolve_secret_reference(self, secret_ref: SecretReference) -> ResolvedSecret:
        """Resolve a secret from its backend."""
        backend = self._backends.get(secret_ref.backend_type)
        if not backend:
            raise ValueError(f"Unsupported secret backend: {secret_ref.backend_type}")

        value = backend.resolve(secret_ref)
        return ResolvedSecret(
            value=value,
            backend_type=secret_ref.backend_type,
            secret_reference_id=secret_ref.id,
        )

    def resolve_ai_model_credentials(
        self,
        ai_model: AIModel,
        *,
        db: Optional[Session] = None,
        allow_refresh: bool = False,
    ) -> Optional[ResolvedModelCredentials]:
        """Resolve typed credentials for an AI model."""
        if ai_model.credentials_secret:
            resolved = self.resolve_secret_reference(ai_model.credentials_secret)
            secret_kind = (
                (ai_model.credentials_secret.secret_kind or "").strip().lower()
            )
            if secret_kind == AI_MODEL_CREDENTIALS_SECRET_KIND:
                return self._resolve_structured_ai_model_credentials(
                    ai_model,
                    resolved,
                    db=db,
                    allow_refresh=allow_refresh,
                )
            return ResolvedModelCredentials(
                credential_type="api_key",
                backend_type=resolved.backend_type,
                value=resolved.value,
                secret_reference_id=resolved.secret_reference_id,
            )

        if ai_model.api_key:
            return ResolvedModelCredentials(
                credential_type="api_key",
                backend_type="legacy_plaintext",
                value=ai_model.api_key,
            )

        return None

    def resolve_ai_model_api_key(self, ai_model: AIModel) -> Optional[ResolvedSecret]:
        """Resolve the API key for an AI model."""
        resolved_credentials = self.resolve_ai_model_credentials(ai_model)
        if (
            not resolved_credentials
            or resolved_credentials.credential_type != "api_key"
        ):
            return None
        return ResolvedSecret(
            value=resolved_credentials.value or "",
            backend_type=resolved_credentials.backend_type,
            secret_reference_id=resolved_credentials.secret_reference_id,
        )

    def _resolve_structured_ai_model_credentials(
        self,
        ai_model: AIModel,
        resolved_secret: ResolvedSecret,
        *,
        db: Optional[Session] = None,
        allow_refresh: bool = False,
    ) -> Optional[ResolvedModelCredentials]:
        try:
            payload = json.loads(resolved_secret.value or "")
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("AI model credential payload is not valid JSON") from exc

        if not isinstance(payload, dict):
            raise ValueError("AI model credential payload must be a JSON object")

        credential_type = str(payload.get("type") or "").strip()
        if not credential_type:
            raise ValueError("AI model credential payload is missing type")

        if (
            credential_type == OPENAI_CODEX_OAUTH_CREDENTIAL_TYPE
            and allow_refresh
            and self._openai_codex_refresh_required(payload)
        ):
            payload = self._refresh_openai_codex_ai_model_credentials(
                ai_model,
                payload,
                db=db,
            )

        if (
            credential_type == ANTHROPIC_CLAUDE_CODE_OAUTH_CREDENTIAL_TYPE
            and allow_refresh
            and self._anthropic_claude_code_refresh_required(payload)
        ):
            payload = self._refresh_anthropic_claude_code_ai_model_credentials(
                ai_model,
                payload,
                db=db,
            )

        if credential_type == "api_key":
            api_key = str(payload.get("api_key") or payload.get("value") or "").strip()
            return ResolvedModelCredentials(
                credential_type=credential_type,
                backend_type=resolved_secret.backend_type,
                value=api_key or None,
                payload=payload,
                secret_reference_id=resolved_secret.secret_reference_id,
            )

        if credential_type == OPENAI_CODEX_OAUTH_CREDENTIAL_TYPE:
            access_token = str(payload.get("access") or "").strip()
            return ResolvedModelCredentials(
                credential_type=credential_type,
                backend_type=resolved_secret.backend_type,
                value=access_token or None,
                payload=payload,
                secret_reference_id=resolved_secret.secret_reference_id,
            )

        if credential_type == ANTHROPIC_CLAUDE_CODE_OAUTH_CREDENTIAL_TYPE:
            access_token = str(payload.get("access") or "").strip()
            return ResolvedModelCredentials(
                credential_type=credential_type,
                backend_type=resolved_secret.backend_type,
                value=access_token or None,
                payload=payload,
                secret_reference_id=resolved_secret.secret_reference_id,
            )

        return ResolvedModelCredentials(
            credential_type=credential_type,
            backend_type=resolved_secret.backend_type,
            payload=payload,
            secret_reference_id=resolved_secret.secret_reference_id,
        )

    def _openai_codex_refresh_required(self, payload: Dict[str, Any]) -> bool:
        expires_raw = payload.get("expires")
        expires_ms = self._coerce_epoch_millis(expires_raw)
        if expires_ms is None:
            return False
        return (
            expires_ms
            <= int(datetime.now(timezone.utc).timestamp() * 1000)
            + OPENAI_CODEX_REFRESH_SKEW_MS
        )

    def _anthropic_claude_code_refresh_required(self, payload: Dict[str, Any]) -> bool:
        expires_raw = payload.get("expires")
        expires_ms = self._coerce_epoch_millis(expires_raw)
        if expires_ms is None:
            return False
        return (
            expires_ms
            <= int(datetime.now(timezone.utc).timestamp() * 1000)
            + ANTHROPIC_CLAUDE_CODE_REFRESH_SKEW_MS
        )

    def _lock_and_reload_oauth_payload(
        self,
        db: Session,
        secret_ref: SecretReference,
        fallback: Dict[str, Any],
        *,
        refresh_required: Callable[[Dict[str, Any]], bool],
    ) -> Dict[str, Any]:
        """Serialize OAuth refreshes on the secret row across workers.

        Provider refresh tokens are single-use: two gateway workers refreshing
        concurrently burn the same token, and the provider's reuse detection
        can invalidate the whole grant. Lock the row (released on the commit
        that persists the rotated bundle), then re-read — if another worker
        already rotated the bundle while we waited, callers must use its
        result instead of refreshing again.
        """

        def inspect(locked: Optional[SecretReference]) -> tuple[Dict[str, Any], bool]:
            payload = fallback
            if locked is not None and locked.encrypted_value:
                try:
                    decoded = json.loads(decrypt_value(locked.encrypted_value))
                    if isinstance(decoded, dict):
                        payload = decoded
                except (TypeError, ValueError, json.JSONDecodeError):
                    pass
            needs_refresh = refresh_required(payload) and bool(
                str(payload.get("refresh") or "").strip()
            )
            return payload, needs_refresh

        return crud_secret_reference.inspect_for_refresh(
            db, secret_id=secret_ref.id, inspect=inspect
        )

    def _refresh_openai_codex_ai_model_credentials(
        self,
        ai_model: AIModel,
        payload: Dict[str, Any],
        *,
        db: Optional[Session] = None,
    ) -> Dict[str, Any]:
        if (
            db is not None
            and ai_model.credentials_secret is not None
            and ai_model.credentials_secret.backend_type == LOCAL_ENCRYPTED_BACKEND
        ):
            payload = self._lock_and_reload_oauth_payload(
                db,
                ai_model.credentials_secret,
                payload,
                refresh_required=self._openai_codex_refresh_required,
            )
            if not self._openai_codex_refresh_required(payload):
                return payload

        refresh_token = str(payload.get("refresh") or "").strip()
        if not refresh_token:
            return payload

        try:
            refreshed = self._refresh_openai_codex_token(refresh_token)
        except CredentialRefreshError as exc:
            if (
                db is not None
                and ai_model.credentials_secret is not None
                and ai_model.credentials_secret.backend_type == LOCAL_ENCRYPTED_BACKEND
            ):
                ai_model.credentials_secret.status = "error"
                ai_model.credentials_secret.meta_data = {
                    **(
                        ai_model.credentials_secret.meta_data
                        if isinstance(ai_model.credentials_secret.meta_data, dict)
                        else {}
                    ),
                    "credential_type": OPENAI_CODEX_OAUTH_CREDENTIAL_TYPE,
                    "last_refresh_error": exc.safe_summary(),
                    "last_refresh_status_code": exc.status_code,
                    "last_refresh_code": exc.code,
                    "last_refresh_failed_at": datetime.now(timezone.utc).isoformat(),
                }
                db.add(ai_model.credentials_secret)
                db.commit()
            raise

        account_id = refreshed.get("account_id") or payload.get("account_id")
        next_payload = {
            "type": OPENAI_CODEX_OAUTH_CREDENTIAL_TYPE,
            "access": refreshed["access"],
            "refresh": refreshed["refresh"],
            "expires": refreshed["expires"],
        }
        if account_id:
            next_payload["account_id"] = account_id

        if (
            db is not None
            and ai_model.credentials_secret is not None
            and ai_model.credentials_secret.backend_type == LOCAL_ENCRYPTED_BACKEND
        ):
            ai_model.credentials_secret.encrypted_value = encrypt_value(
                json.dumps(next_payload)
            )
            ai_model.credentials_secret.last_verified_at = datetime.now(timezone.utc)
            ai_model.credentials_secret.meta_data = {
                **(
                    ai_model.credentials_secret.meta_data
                    if isinstance(ai_model.credentials_secret.meta_data, dict)
                    else {}
                ),
                "credential_type": OPENAI_CODEX_OAUTH_CREDENTIAL_TYPE,
            }
            db.add(ai_model.credentials_secret)
            db.commit()
            db.refresh(ai_model.credentials_secret)

        return next_payload

    def _refresh_anthropic_claude_code_ai_model_credentials(
        self,
        ai_model: AIModel,
        payload: Dict[str, Any],
        *,
        db: Optional[Session] = None,
    ) -> Dict[str, Any]:
        if (
            db is not None
            and ai_model.credentials_secret is not None
            and ai_model.credentials_secret.backend_type == LOCAL_ENCRYPTED_BACKEND
        ):
            payload = self._lock_and_reload_oauth_payload(
                db,
                ai_model.credentials_secret,
                payload,
                refresh_required=self._anthropic_claude_code_refresh_required,
            )
            if not self._anthropic_claude_code_refresh_required(payload):
                return payload

        refresh_token = str(payload.get("refresh") or "").strip()
        if not refresh_token:
            return payload

        try:
            refreshed = self._refresh_anthropic_claude_code_token(refresh_token)
        except CredentialRefreshError as exc:
            if (
                db is not None
                and ai_model.credentials_secret is not None
                and ai_model.credentials_secret.backend_type == LOCAL_ENCRYPTED_BACKEND
            ):
                ai_model.credentials_secret.status = "error"
                ai_model.credentials_secret.meta_data = {
                    **(
                        ai_model.credentials_secret.meta_data
                        if isinstance(ai_model.credentials_secret.meta_data, dict)
                        else {}
                    ),
                    "credential_type": ANTHROPIC_CLAUDE_CODE_OAUTH_CREDENTIAL_TYPE,
                    "last_refresh_error": exc.safe_summary(),
                    "last_refresh_status_code": exc.status_code,
                    "last_refresh_code": exc.code,
                    "last_refresh_failed_at": datetime.now(timezone.utc).isoformat(),
                }
                db.add(ai_model.credentials_secret)
                db.commit()
            raise

        next_payload = {
            "type": ANTHROPIC_CLAUDE_CODE_OAUTH_CREDENTIAL_TYPE,
            "access": refreshed["access"],
            "refresh": refreshed["refresh"],
            "expires": refreshed["expires"],
        }

        if (
            db is not None
            and ai_model.credentials_secret is not None
            and ai_model.credentials_secret.backend_type == LOCAL_ENCRYPTED_BACKEND
        ):
            ai_model.credentials_secret.encrypted_value = encrypt_value(
                json.dumps(next_payload)
            )
            ai_model.credentials_secret.last_verified_at = datetime.now(timezone.utc)
            ai_model.credentials_secret.meta_data = {
                **(
                    ai_model.credentials_secret.meta_data
                    if isinstance(ai_model.credentials_secret.meta_data, dict)
                    else {}
                ),
                "credential_type": ANTHROPIC_CLAUDE_CODE_OAUTH_CREDENTIAL_TYPE,
            }
            ai_model.credentials_secret.status = "active"
            db.add(ai_model.credentials_secret)
            db.commit()
            db.refresh(ai_model.credentials_secret)

        return next_payload

    def _refresh_anthropic_claude_code_token(
        self, refresh_token: str
    ) -> Dict[str, Any]:
        # Match the real Claude Code client: JSON body + a Claude Code
        # User-Agent. A form-encoded POST without a User-Agent is blocked by
        # Cloudflare with HTTP 403 "error code: 1010" before reaching the
        # OAuth handler (see ANTHROPIC_CLAUDE_CODE_USER_AGENT).
        body = json.dumps(
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": ANTHROPIC_CLAUDE_CODE_CLIENT_ID,
            }
        ).encode("utf-8")
        req = urllib_request.Request(
            ANTHROPIC_CLAUDE_CODE_TOKEN_URL,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "anthropic-beta": "oauth-2025-04-20",
                "User-Agent": ANTHROPIC_CLAUDE_CODE_USER_AGENT,
            },
            method="POST",
        )
        try:
            with urllib_request.urlopen(req, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib_error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "ignore")
            code = self._extract_oauth_error_code(detail)
            logger.warning(
                "Anthropic Claude Code OAuth refresh HTTP %s (code=%s)",
                exc.code,
                code,
            )
            raise CredentialRefreshError(
                f"Anthropic Claude Code OAuth token refresh failed with status "
                f"{exc.code}",
                provider="anthropic",
                status_code=exc.code,
                code=code,
            ) from exc
        except urllib_error.URLError as exc:
            raise CredentialRefreshError(
                f"Anthropic Claude Code OAuth token refresh failed: {exc.reason}",
                provider="anthropic",
            ) from exc

        access = str(payload.get("access_token") or "").strip()
        refresh = str(payload.get("refresh_token") or refresh_token).strip()
        expires_in = payload.get("expires_in")
        if not access or not refresh or not isinstance(expires_in, (int, float)):
            raise CredentialRefreshError(
                "Anthropic Claude Code OAuth token refresh response missing fields",
                provider="anthropic",
            )
        return {
            "access": access,
            "refresh": refresh,
            "expires": int(datetime.now(timezone.utc).timestamp() * 1000)
            + int(expires_in * 1000),
        }

    @staticmethod
    def _extract_oauth_error_code(detail: str) -> Optional[str]:
        try:
            payload = json.loads(detail)
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = None
        if isinstance(payload, dict):
            # OpenAI nests the machine-readable code inside an ``error``
            # object (e.g. {"error": {"code": "refresh_token_reused", ...}}).
            nested = payload.get("error")
            if isinstance(nested, dict):
                payload = nested
            for key in ("code", "error_code", "error"):
                value = payload.get(key)
                if value is not None:
                    return str(value)
        return None

    def _refresh_openai_codex_token(self, refresh_token: str) -> Dict[str, Any]:
        body = urllib_parse.urlencode(
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": OPENAI_CODEX_CLIENT_ID,
            }
        ).encode("utf-8")
        req = urllib_request.Request(
            OPENAI_CODEX_TOKEN_URL,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            with urllib_request.urlopen(req, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib_error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "ignore")
            code = self._extract_oauth_error_code(detail)
            logger.warning(
                "OpenAI Codex OAuth refresh HTTP %s (code=%s)",
                exc.code,
                code,
            )
            raise CredentialRefreshError(
                f"OpenAI Codex token refresh failed with status {exc.code}",
                provider="openai",
                status_code=exc.code,
                code=code,
            ) from exc
        except urllib_error.URLError as exc:
            raise CredentialRefreshError(
                f"OpenAI Codex token refresh failed: {exc.reason}",
                provider="openai",
            ) from exc

        access = str(payload.get("access_token") or "").strip()
        refresh = str(payload.get("refresh_token") or "").strip()
        expires_in = payload.get("expires_in")
        if not access or not refresh or not isinstance(expires_in, (int, float)):
            raise CredentialRefreshError(
                "OpenAI Codex token refresh response missing fields",
                provider="openai",
            )
        return {
            "access": access,
            "refresh": refresh,
            "expires": int(datetime.now(timezone.utc).timestamp() * 1000)
            + int(expires_in * 1000),
            "account_id": self._extract_openai_codex_account_id(access),
        }

    def _extract_openai_codex_account_id(self, access_token: str) -> Optional[str]:
        parts = access_token.split(".")
        if len(parts) != 3:
            return None
        payload = parts[1]
        padding = "=" * (-len(payload) % 4)
        try:
            decoded = json.loads(
                urlsafe_b64decode((payload + padding).encode("utf-8")).decode("utf-8")
            )
        except (ValueError, json.JSONDecodeError):
            return None
        auth = decoded.get(OPENAI_CODEX_JWT_CLAIM_PATH)
        if not isinstance(auth, dict):
            return None
        account_id = auth.get("chatgpt_account_id")
        return str(account_id).strip() if account_id else None

    @staticmethod
    def _coerce_epoch_millis(value: Any) -> Optional[int]:
        if isinstance(value, (int, float)):
            return int(value)
        if isinstance(value, str):
            try:
                return int(float(value))
            except ValueError:
                return None

        return None


_secret_service: Optional[SecretService] = None


def get_secret_service() -> SecretService:
    """Get or create the singleton secret service."""
    global _secret_service
    if _secret_service is None:
        _secret_service = SecretService()
    return _secret_service
