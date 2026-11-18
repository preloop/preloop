"""Encryption utilities for sensitive data.

Uses Fernet symmetric encryption from the cryptography library.

Key Management:
- If SECURITY__ENCRYPTION_KEY is set, uses that key
- Otherwise, derives a key from SECURITY__SECRET_KEY (JWT secret) for seamless upgrades
- This ensures existing Helm deployments get encryption without config changes

For new deployments, it's recommended to set a dedicated SECURITY__ENCRYPTION_KEY.
Generate with: python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
"""

import base64
import hashlib
import logging
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

# Cache for the Fernet instance
_fernet: Fernet | None = None


@lru_cache(maxsize=1)
def _get_encryption_key() -> str:
    """Get or derive the encryption key.

    Priority:
    1. SECURITY__ENCRYPTION_KEY if explicitly set
    2. Derived from SECURITY__SECRET_KEY (JWT secret) for seamless upgrades
    """
    from preloop.config import settings

    # Use dedicated encryption key if set
    if settings.security.encryption_key:
        return settings.security.encryption_key

    # Derive from JWT secret key for seamless upgrades
    # This ensures existing deployments get encryption without config changes
    secret_key = settings.security.secret_key
    if secret_key:
        # Derive a Fernet-compatible key from the secret
        # Use SHA256 to get 32 bytes, then base64 encode for Fernet
        derived = hashlib.sha256(secret_key.encode()).digest()
        fernet_key = base64.urlsafe_b64encode(derived).decode()
        logger.debug("Using encryption key derived from SECRET_KEY.")
        return fernet_key

    raise ValueError(
        "No encryption key available. "
        "Set SECURITY__ENCRYPTION_KEY or SECURITY__SECRET_KEY."
    )


def _get_fernet() -> Fernet:
    """Get or create a Fernet instance with the configured key."""
    global _fernet

    if _fernet is not None:
        return _fernet

    key = _get_encryption_key()
    _fernet = Fernet(key.encode())
    return _fernet


def encrypt_value(value: str) -> str:
    """Encrypt a sensitive value for storage.

    Args:
        value: Plain text value to encrypt

    Returns:
        Encrypted Fernet token
    """
    if not value:
        return ""

    fernet = _get_fernet()
    encrypted = fernet.encrypt(value.encode("utf-8"))
    return encrypted.decode("utf-8")


def decrypt_value(encrypted_value: str) -> str:
    """Decrypt a stored encrypted value.

    Args:
        encrypted_value: Fernet-encrypted value from storage

    Returns:
        Decrypted plain text value

    Raises:
        ValueError: If decryption fails
    """
    if not encrypted_value:
        return ""

    fernet = _get_fernet()
    try:
        decrypted = fernet.decrypt(encrypted_value.encode("utf-8"))
        return decrypted.decode("utf-8")
    except InvalidToken as e:
        logger.error("Failed to decrypt value - invalid token")
        raise ValueError("Unable to decrypt value") from e


def try_decrypt_value(encrypted_value: str) -> str | None:
    """Decrypt a stored value, returning None when this key cannot read it.

    Unlike ``decrypt_value`` this logs nothing, so a caller can write one
    line of its own that names the affected record.

    Args:
        encrypted_value: Fernet-encrypted value from storage

    Returns:
        The plain text value, or None if the token is invalid for this key
    """
    if not encrypted_value:
        return ""
    try:
        return _get_fernet().decrypt(encrypted_value.encode("utf-8")).decode("utf-8")
    except InvalidToken:
        return None


def reset_encryption_cache() -> None:
    """Reset the encryption key cache. Useful for testing."""
    global _fernet
    _fernet = None
    _get_encryption_key.cache_clear()
