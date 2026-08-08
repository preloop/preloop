"""CRUD operations for ApiKey model."""

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..models.api_key import ApiKey
from ..models.managed_agent_credential import ManagedAgentCredential
from ..models.user import User
from .base import CRUDBase


class CRUDApiKey(CRUDBase[ApiKey]):
    """CRUD operations for API key."""

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
    ) -> tuple[ApiKey, str]:
        """Create a runtime-scoped API key stored without plaintext value."""
        token_value = key_value or f"flow_{secrets.token_urlsafe(32)}"
        runtime_key_name = name
        if (
            db.query(ApiKey)
            .filter(ApiKey.account_id == account_id, ApiKey.name == runtime_key_name)
            .first()
            is not None
        ):
            runtime_key_name = f"{name} ({secrets.token_hex(4)})"
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
        )

        db.add(db_obj)
        if commit:
            db.commit()
            db.refresh(db_obj)
        else:
            db.flush()
        return db_obj, token_value

    def get_by_key(
        self, db: Session, *, key: str, account_id: Optional[str] = None
    ) -> Optional[ApiKey]:
        """Get API key by key string."""
        key_hash = self.build_key_hash(key)
        key_prefix = self.build_key_prefix(key)

        query = db.query(ApiKey).filter(
            (ApiKey.key == key)
            | ((ApiKey.key_prefix == key_prefix) & (ApiKey.key_hash == key_hash))
        )
        if account_id:
            query = query.filter(ApiKey.account_id == account_id)
        return query.first()

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
        key_objs = (
            db.query(ApiKey)
            .outerjoin(
                ManagedAgentCredential,
                ManagedAgentCredential.api_key_id == ApiKey.id,
            )
            .filter(
                ApiKey.account_id == account_id,
                ApiKey.is_active.is_(False),
                ApiKey.context_data.op("->>")("managed_agent_id")
                == str(managed_agent_id),
                or_(
                    ManagedAgentCredential.id.is_(None),
                    ManagedAgentCredential.status != "revoked",
                ),
            )
            .all()
        )

        reactivated: List[ApiKey] = []
        for key_obj in key_objs:
            if key_obj.is_expired:
                continue
            key_obj.is_active = True
            db.add(key_obj)
            reactivated.append(key_obj)

        if not reactivated:
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
        if account_id:
            query = query.filter(User.account_id == account_id)
        return query.order_by(ApiKey.created_at.desc()).all()

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
        if account_id:
            query = query.filter(User.account_id == account_id)
        return query.first()
