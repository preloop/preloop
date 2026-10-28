"""CRUD operations for ApiKey model."""

import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Query, Session

from preloop.models import models
from .base import CRUDBase

ApiKey = models.ApiKey
ManagedAgentCredential = models.ManagedAgentCredential
User = models.User

logger = logging.getLogger(__name__)


class CRUDApiKey(CRUDBase[ApiKey]):
    """CRUD operations for API key."""

    @staticmethod
    def _legacy_only(query: Query[ApiKey]) -> Query[ApiKey]:
        """Keep every machine marker out of human key administration."""
        return query.filter(
            ApiKey.credential_type == "legacy",
            ApiKey.credential_version.is_(None),
            ApiKey.ci_principal_id.is_(None),
            ApiKey.ci_actions.is_(None),
        )

    @staticmethod
    def build_key_hash(key_value: str) -> str:
        """Build a deterministic lookup fingerprint for an API key value.

        This is not password hashing: API keys are high-entropy secrets looked
        up on every request, so a fast one-way digest is appropriate.
        """
        # codeql[py/weak-sensitive-data-hashing] Token fingerprint for lookup, not password storage
        return hashlib.sha256(key_value.encode("utf-8")).hexdigest()

    @staticmethod
    def build_key_prefix(key_value: str, prefix_len: int = 12) -> str:
        """Build a non-sensitive prefix used for key lookups."""
        return key_value[:prefix_len]

    def issue(
        self,
        db: Session,
        *,
        obj_in: Dict[str, Any],
        key_value: Optional[str] = None,
        commit: bool = True,
    ) -> tuple[ApiKey, str]:
        """Persist a fingerprint and return the token once, separately from the row."""
        token = key_value or secrets.token_urlsafe(32)
        data = dict(obj_in)
        data.pop("key", None)
        data["key_hash"] = self.build_key_hash(token)
        data["key_prefix"] = self.build_key_prefix(token)
        return super().create(db, obj_in=data, commit=commit), token

    def update(
        self,
        db: Session,
        *,
        db_obj: ApiKey,
        obj_in: Dict[str, Any],
        commit: bool = True,
    ) -> ApiKey:
        """Hash legacy rotation input before updating persisted columns."""
        data = dict(obj_in)
        token = data.pop("key", None)
        # Already-hashed callers used None to clear the retired plaintext field.
        if token is not None:
            if not isinstance(token, str) or not token:
                raise ValueError("API key must not be empty")
            data["key_hash"] = self.build_key_hash(token)
            data["key_prefix"] = self.build_key_prefix(token)
        return super().update(db, db_obj=db_obj, obj_in=data, commit=commit)

    def create_with_owner(
        self,
        db: Session,
        *,
        obj_in: Dict[str, Any],
        owner_username: str,
        expires_days: Optional[int] = 365,
        key_value: Optional[str] = None,
    ) -> ApiKey:
        """Create a new API key with owner."""
        # Get the user by username
        user = db.query(User).filter(User.username == owner_username).first()
        if not user:
            raise ValueError(f"User with username {owner_username} not found")

        obj_in_data = dict(obj_in)
        key_value = key_value or secrets.token_urlsafe(32)

        expires_at = None
        if expires_days is not None:
            expires_at = datetime.now(timezone.utc) + timedelta(days=expires_days)

        db_obj = ApiKey(
            name=obj_in_data.get("name", "API Key"),
            key=key_value,
            key_hash=self.build_key_hash(key_value),
            key_prefix=self.build_key_prefix(key_value),
            account_id=user.account_id,
            user_id=user.id,
            expires_at=expires_at,
            scopes=obj_in_data.get("scopes", []),
            is_active=True,
        )

        db.add(db_obj)
        db.commit()
        db.refresh(db_obj)
        return db_obj

    def get_for_update(
        self, db: Session, *, id: Any, account_id: Any
    ) -> Optional[ApiKey]:
        """Lock and reload one tenant's key until the caller ends its transaction.

        Reloading defeats stale identity-map state after a concurrent rotation.
        Callers must retain the transaction through validation, mutation and audit.
        """
        return (
            db.query(ApiKey)
            .filter(ApiKey.id == id, ApiKey.account_id == account_id)
            .populate_existing()
            .with_for_update()
            .first()
        )

    def list_by_context(
        self,
        db: Session,
        *,
        account_id: Any,
        context_key: str,
        filters: Optional[Dict[str, str]] = None,
        limit: int = 100,
    ) -> tuple[List[ApiKey], int]:
        """List a tenant's context-bound keys with exact count and bounded rows.

        Context field names and values remain bound SQL parameters. Missing
        and JSON-null contexts cannot match a persisted restriction.
        """
        if not 1 <= limit <= 1000:
            raise ValueError("Key list limit must be between 1 and 1000")
        context = ApiKey.context_data[context_key]
        query = db.query(ApiKey).filter(
            ApiKey.account_id == account_id, context.as_string().is_not(None)
        )
        for field, value in (filters or {}).items():
            query = query.filter(context[field].as_string() == value)
        total = query.count()
        rows = query.order_by(ApiKey.created_at.desc(), ApiKey.id).limit(limit).all()
        return rows, total

    def create_runtime_key(
        self,
        db: Session,
        *,
        name: str,
        account_id: Any,
        user_id: Any,
        scopes: Optional[List[str]] = None,
        expires_at: Optional[datetime] = None,
        context_data: Optional[Dict[str, Any]] = None,
        key_value: Optional[str] = None,
        commit: bool = True,
        restricted_runtime: bool = False,
    ) -> tuple[ApiKey, str]:
        """Create a runtime-scoped API key stored without plaintext value."""
        token_value = key_value or f"flow_{secrets.token_urlsafe(32)}"
        runtime_key_name = name[:100]
        if (
            db.query(ApiKey)
            .filter(ApiKey.account_id == account_id, ApiKey.name == runtime_key_name)
            .first()
            is not None
        ):
            suffix = f" ({secrets.token_hex(4)})"
            runtime_key_name = f"{name[: 100 - len(suffix)]}{suffix}"
        db_obj = ApiKey(
            name=runtime_key_name,
            key=None,
            key_hash=self.build_key_hash(token_value),
            key_prefix=self.build_key_prefix(token_value),
            account_id=account_id,
            user_id=user_id,
            expires_at=expires_at,
            scopes=scopes or [],
            is_active=True,
            context_data=context_data,
            credential_type="restricted_runtime" if restricted_runtime else "legacy",
            credential_version=1 if restricted_runtime else None,
        )

        db.add(db_obj)
        if commit:
            db.commit()
            db.refresh(db_obj)
        else:
            db.flush()
        return db_obj, token_value

    def get_by_key(
        self,
        db: Session,
        *,
        key: str,
        account_id: Optional[str] = None,
        include_restricted: bool = False,
        include_restricted_runtime: bool = False,
    ) -> Optional[ApiKey]:
        """Get API key by key string."""
        if not key:
            # A blank bearer must never match a legacy sha256('') fingerprint.
            return None
        key_hash = self.build_key_hash(key)
        key_prefix = self.build_key_prefix(key)

        query = db.query(ApiKey).filter(
            ApiKey.key_prefix == key_prefix, ApiKey.key_hash == key_hash
        )
        if account_id:
            query = query.filter(ApiKey.account_id == account_id)
        key_obj = query.first()
        # Custom MCP/gateway/session auth also uses this lookup. Until a caller
        # explicitly opts into machine authorization, never return a CI key.
        if (
            key_obj
            and key_obj.requires_machine_authorization is True
            and not include_restricted
            and not (
                include_restricted_runtime
                and key_obj.credential_type == "restricted_runtime"
            )
        ):
            return None
        return key_obj

    def get_active_by_user(
        self,
        db: Session,
        *,
        username: str,
        skip: int = 0,
        limit: int = 100,
        account_id: Optional[str] = None,
    ) -> List[ApiKey]:
        """Get active API keys for a user."""
        query = (
            db.query(ApiKey)
            .join(User, ApiKey.user_id == User.id)
            .filter(User.username == username, ApiKey.is_active.is_(True))
        )
        # Human key administration must use the CI lifecycle seam for CI keys.
        query = self._legacy_only(query)
        if account_id:
            query = query.filter(User.account_id == account_id)
        return query.offset(skip).limit(limit).all()

    def update_last_used(self, db: Session, *, key_id: Any) -> Optional[ApiKey]:
        """Update the last_used_at field to current timestamp.

        Args:
            db: Database session
            key_id: ID of the key to update

        Returns:
            Updated API key if found
        """
        key_obj = db.query(ApiKey).filter(ApiKey.id == key_id).first()
        if key_obj:
            key_obj.last_used_at = datetime.now(timezone.utc)
            db.add(key_obj)
            db.commit()
            db.refresh(key_obj)
        return key_obj

    def deactivate(self, db: Session, *, key_id: Any) -> Optional[ApiKey]:
        """Deactivate an API key.

        Args:
            db: Database session
            key_id: ID of the key to deactivate

        Returns:
            Deactivated API key if found
        """
        # Convert string ID to UUID if needed
        key_obj = db.query(ApiKey).filter(ApiKey.id == key_id).first()
        if key_obj:
            key_obj.is_active = False
            db.add(key_obj)
            db.commit()
            db.refresh(key_obj)
        return key_obj

    def deactivate_runtime_keys_for_session(
        self,
        db: Session,
        *,
        account_id: Any,
        runtime_session_id: Any,
        commit: bool = True,
    ) -> List[ApiKey]:
        """Deactivate runtime-scoped API keys bound to one runtime session."""
        key_objs = (
            db.query(ApiKey)
            .filter(
                ApiKey.account_id == account_id,
                ApiKey.is_active.is_(True),
                ApiKey.context_data.op("->>")("runtime_session_id")
                == str(runtime_session_id),
            )
            .all()
        )

        deactivated: List[ApiKey] = []
        for key_obj in key_objs:
            key_obj.is_active = False
            db.add(key_obj)
            deactivated.append(key_obj)

        if not deactivated:
            return deactivated

        if commit:
            db.commit()
            for key_obj in deactivated:
                db.refresh(key_obj)
        else:
            db.flush()
        return deactivated

    def deactivate_runtime_keys_for_principal(
        self,
        db: Session,
        *,
        account_id: Any,
        runtime_principal_type: str,
        runtime_principal_id: str,
        commit: bool = True,
    ) -> List[ApiKey]:
        """Deactivate runtime-scoped API keys bound to one durable principal.

        WARNING: several managed-agent registry entries can share one runtime
        principal (repeated onboards of the same local agent), so this sweeps
        EVERY sibling agent's keys — including durable credentials still in
        use. For agent lifecycle changes use
        ``deactivate_runtime_keys_for_managed_agent`` plus
        ``deactivate_unbound_runtime_keys_for_principal`` instead.
        """
        key_objs = (
            db.query(ApiKey)
            .filter(
                ApiKey.account_id == account_id,
                ApiKey.is_active.is_(True),
                ApiKey.context_data["runtime_principal"].op("->>")("type")
                == str(runtime_principal_type),
                ApiKey.context_data["runtime_principal"].op("->>")("id")
                == str(runtime_principal_id),
            )
            .all()
        )

        deactivated: List[ApiKey] = []
        for key_obj in key_objs:
            key_obj.is_active = False
            db.add(key_obj)
            deactivated.append(key_obj)

        if not deactivated:
            return deactivated

        if commit:
            db.commit()
            for key_obj in deactivated:
                db.refresh(key_obj)
        else:
            db.flush()
        return deactivated

    def deactivate_unbound_runtime_keys_for_principal(
        self,
        db: Session,
        *,
        account_id: Any,
        runtime_principal_type: str,
        runtime_principal_id: str,
        commit: bool = True,
    ) -> List[ApiKey]:
        """Deactivate principal-bound runtime keys with no managed-agent link.

        Several managed-agent registry entries can share one runtime principal
        (repeated onboards of the same local agent). Keys that carry a
        ``managed_agent_id`` must be revoked per agent via
        ``deactivate_runtime_keys_for_managed_agent``; revoking by principal
        alone would also kill sibling agents' durable credentials. This method
        only sweeps legacy keys that never recorded a managed-agent binding.
        """
        key_objs = (
            db.query(ApiKey)
            .filter(
                ApiKey.account_id == account_id,
                ApiKey.is_active.is_(True),
                ApiKey.context_data["runtime_principal"].op("->>")("type")
                == str(runtime_principal_type),
                ApiKey.context_data["runtime_principal"].op("->>")("id")
                == str(runtime_principal_id),
                ApiKey.context_data.op("->>")("managed_agent_id").is_(None),
            )
            .all()
        )

        deactivated: List[ApiKey] = []
        for key_obj in key_objs:
            key_obj.is_active = False
            db.add(key_obj)
            deactivated.append(key_obj)

        if not deactivated:
            return deactivated

        if commit:
            db.commit()
            for key_obj in deactivated:
                db.refresh(key_obj)
        else:
            db.flush()
        return deactivated

    def deactivate_runtime_keys_for_flow_execution(
        self,
        db: Session,
        *,
        account_id: Any,
        execution_id: Any,
        commit: bool = True,
    ) -> List[ApiKey]:
        """Deactivate runtime-scoped API keys bound to one flow execution."""
        key_objs = (
            db.query(ApiKey)
            .filter(
                ApiKey.account_id == account_id,
                ApiKey.is_active.is_(True),
                ApiKey.context_data.op("->>")("flow_execution_id") == str(execution_id),
            )
            .all()
        )

        deactivated: List[ApiKey] = []
        for key_obj in key_objs:
            key_obj.is_active = False
            db.add(key_obj)
            deactivated.append(key_obj)

        if not deactivated:
            return deactivated

        if commit:
            db.commit()
            for key_obj in deactivated:
                db.refresh(key_obj)
        else:
            db.flush()
        return deactivated

    def deactivate_runtime_keys_for_managed_agent(
        self,
        db: Session,
        *,
        account_id: Any,
        managed_agent_id: str,
        commit: bool = True,
    ) -> List[ApiKey]:
        """Deactivate runtime-scoped API keys bound to one managed agent."""
        key_objs = (
            db.query(ApiKey)
            .filter(
                ApiKey.account_id == account_id,
                ApiKey.is_active.is_(True),
                ApiKey.context_data.op("->>")("managed_agent_id")
                == str(managed_agent_id),
            )
            .all()
        )

        deactivated: List[ApiKey] = []
        for key_obj in key_objs:
            key_obj.is_active = False
            db.add(key_obj)
            deactivated.append(key_obj)

        if not deactivated:
            return deactivated

        if commit:
            db.commit()
            for key_obj in deactivated:
                db.refresh(key_obj)
        else:
            db.flush()
        return deactivated

    def reactivate_runtime_keys_for_managed_agent(
        self,
        db: Session,
        *,
        account_id: Any,
        managed_agent_id: str,
        commit: bool = True,
    ) -> List[ApiKey]:
        """Reactivate runtime keys a past suspension deactivated.

        Pausing an agent is a lifecycle flag today (every auth path rejects
        non-active agents on every request), so nothing needs restoring for
        agents paused by current code. This heals agents that were suspended
        by the older revoke-on-suspend behavior, whose durable gateway
        credential stayed ``is_active = False`` forever because resume had no
        inverse.

        Deliberately narrow so it can never resurrect a credential an
        operator killed on purpose:

        - only keys bound to this managed agent (never principal-wide sweeps,
          which would touch sibling agents' credentials),
        - never a key whose ``ManagedAgentCredential`` row is ``revoked``,
        - never an expired key.

        Args:
            db: Database session.
            account_id: Account that owns the keys.
            managed_agent_id: Managed agent whose keys are being restored.
            commit: Commit the transaction when True.

        Returns:
            The API keys that were reactivated.
        """
        # ``expires_at`` is a naive column holding UTC (see ApiKey.is_expired),
        # so the cutoff is computed here rather than with func.now(), which
        # would compare a timestamptz against a naive column.
        now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
        key_objs = (
            db.query(ApiKey)
            .outerjoin(
                ManagedAgentCredential,
                ManagedAgentCredential.api_key_id == ApiKey.id,
            )
            .filter(
                ApiKey.account_id == account_id,
                ApiKey.is_active.is_(False),
                ApiKey.credential_type == "legacy",
                ApiKey.context_data.op("->>")("managed_agent_id")
                == str(managed_agent_id),
                or_(
                    ManagedAgentCredential.id.is_(None),
                    ManagedAgentCredential.status != "revoked",
                ),
                or_(
                    ApiKey.expires_at.is_(None),
                    ApiKey.expires_at > now_naive,
                ),
            )
            .all()
        )

        reactivated: List[ApiKey] = []
        skipped_expired = 0
        for key_obj in key_objs:
            # Belt and braces: the SQL filter above does the real work, this
            # catches a row that expired between the query and this loop.
            if key_obj.is_expired:
                skipped_expired += 1
                continue
            key_obj.is_active = True
            db.add(key_obj)
            reactivated.append(key_obj)

        if not reactivated:
            logger.info(
                "No runtime keys reactivated for managed agent %s "
                "(candidates=%d, skipped_expired=%d)",
                managed_agent_id,
                len(key_objs),
                skipped_expired,
            )
            return reactivated

        if commit:
            db.commit()
            for key_obj in reactivated:
                db.refresh(key_obj)
        else:
            db.flush()
        return reactivated

    def validate_key(
        self, db: Session, *, key: str, required_scopes: Optional[List[str]] = None
    ) -> Optional[ApiKey]:
        """Validate key and check if it has the required scopes.

        Args:
            db: Database session
            key: Key string to validate
            required_scopes: List of scopes that the key must have

        Returns:
            Valid API key if found and has required scopes, None otherwise
        """
        key_obj = self.get_by_key(db, key=key)

        # Key doesn't exist or is inactive
        if not key_obj or not key_obj.is_active:
            return None

        # Key is expired
        if key_obj.is_expired:
            return None

        # Check scopes if required
        if required_scopes:
            key_scopes = set(key_obj.scopes)
            if not all(scope in key_scopes for scope in required_scopes):
                return None

        # Update last used timestamp
        self.update_last_used(db, key_id=key_obj.id)

        return key_obj

    def get_by_user(
        self, db: Session, *, username: str, account_id: Optional[str] = None
    ) -> List[ApiKey]:
        """Get all API keys for a user (including inactive)."""
        query = (
            db.query(ApiKey)
            .join(User, ApiKey.user_id == User.id)
            .filter(User.username == username)
        )
        # Human key administration must use the CI lifecycle seam for CI keys.
        query = self._legacy_only(query)
        if account_id:
            query = query.filter(User.account_id == account_id)
        return query.order_by(ApiKey.created_at.desc()).all()

    #: Columns ``page_by_user`` may sort by, keyed by their public name.
    PAGE_SORTS = ("name", "created", "expires")

    def page_by_user(
        self,
        db: Session,
        *,
        username: str,
        limit: int,
        offset: int = 0,
        account_id: Optional[str] = None,
        search: Optional[str] = None,
        include_retired: bool = True,
        sort: str = "created",
        descending: bool = True,
    ) -> Tuple[List[ApiKey], int, int]:
        """Read one page of a user's keys with the counts the list needs.

        Settings > API keys used to read every key (242 KB on a busy
        account) to show 25 of them (#1694).

        Args:
            db: Database session.
            username: Owner of the keys.
            limit: Page size.
            offset: Rows to skip.
            account_id: Optional account the owner must belong to.
            search: Case-insensitive substring of the key name.
            include_retired: Whether revoked and expired keys are listed.
            sort: One of ``PAGE_SORTS``.
            descending: Sort direction.

        Returns:
            The page, the number of rows matching the filters, and the number
            of retired keys matching the search (listed or not).

        Raises:
            ValueError: If ``sort`` is not one of ``PAGE_SORTS``.
        """
        if sort not in self.PAGE_SORTS:
            raise ValueError(f"Unsupported sort: {sort}")
        now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
        retired = or_(
            ApiKey.is_active.is_(False),
            ApiKey.expires_at <= now_naive,
        )
        # Spelled out rather than ``~retired``: NOT over a NULL expiry is NULL.
        usable = and_(
            ApiKey.is_active.is_(True),
            or_(ApiKey.expires_at.is_(None), ApiKey.expires_at > now_naive),
        )
        base = (
            db.query(ApiKey)
            .join(User, ApiKey.user_id == User.id)
            .filter(User.username == username)
        )
        base = self._legacy_only(base)
        if account_id:
            base = base.filter(User.account_id == account_id)
        if search:
            escaped = (
                search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            )
            base = base.filter(ApiKey.name.ilike(f"%{escaped}%", escape="\\"))
        retired_count = int(
            base.filter(retired).with_entities(func.count(ApiKey.id)).scalar() or 0
        )
        listed = base if include_retired else base.filter(usable)
        total = int(listed.with_entities(func.count(ApiKey.id)).scalar() or 0)
        if sort == "name":
            column: Any = func.lower(ApiKey.name)
        elif sort == "expires":
            column = ApiKey.expires_at
        else:
            column = ApiKey.created_at
        # Postgres sorts NULL as the largest value, which is what "Never
        # expires" means.
        order = column.desc() if descending else column.asc()
        rows = (
            listed.order_by(order, ApiKey.id.desc())
            .offset(max(0, offset))
            .limit(max(1, limit))
            .all()
        )
        return rows, total, retired_count

    def count_usable_by_user(
        self, db: Session, *, username: str, account_id: Optional[str] = None
    ) -> int:
        """Count a user's keys that still work: active and not past expiry.

        The console Overview shows this number; reading the full key list for
        it cost about two seconds on a busy account (#1694).

        Args:
            db: Database session.
            username: Owner of the keys.
            account_id: Optional account the owner must belong to.

        Returns:
            Number of usable keys.
        """
        # ``expires_at`` is naive UTC; compare against a naive UTC cutoff, not
        # func.now(), which depends on the session TimeZone.
        now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
        query = (
            db.query(func.count(ApiKey.id))
            .join(User, ApiKey.user_id == User.id)
            .filter(User.username == username, ApiKey.is_active.is_(True))
            .filter(or_(ApiKey.expires_at.is_(None), ApiKey.expires_at > now_naive))
        )
        query = self._legacy_only(query)
        if account_id:
            query = query.filter(User.account_id == account_id)
        return int(query.scalar() or 0)

    def get_by_id_and_user(
        self,
        db: Session,
        *,
        key_id: Any,
        username: str,
        account_id: Optional[str] = None,
    ) -> Optional[ApiKey]:
        """Get API key by ID and username."""
        query = (
            db.query(ApiKey)
            .join(User, ApiKey.user_id == User.id)
            .filter(ApiKey.id == key_id, User.username == username)
        )
        # Human key administration must use the CI lifecycle seam for CI keys.
        query = self._legacy_only(query)
        if account_id:
            query = query.filter(User.account_id == account_id)
        return query.first()
