"""Apple Push Notification Service (APNS) for iOS push notifications."""

import base64
import logging
import time
from typing import Any, Dict, Optional, Tuple

import httpx
import jwt

logger = logging.getLogger(__name__)


class APNsService:
    """Service for sending push notifications via Apple Push Notification service.

    Uses JWT authentication with ES256 algorithm for APNs HTTP/2 API.
    Implements token caching to minimize JWT generation overhead.
    """

    def __init__(
        self,
        team_id: str,
        key_id: str,
        bundle_id: str,
        use_sandbox: bool = False,
        auth_key: Optional[str] = None,
        auth_key_path: Optional[str] = None,
    ):
        """Initialize APNs service.

        Args:
            team_id: Apple Developer Team ID (10 characters).
            key_id: APNs Auth Key ID (10 characters).
            bundle_id: App bundle identifier.
            use_sandbox: If True, use sandbox environment.
            auth_key: The .p8 auth key content directly (takes precedence).
            auth_key_path: Path to .p8 auth key file (fallback).

        Raises:
            ValueError: If neither auth_key nor auth_key_path is provided.
        """
        self.team_id = team_id
        self.key_id = key_id
        self.bundle_id = bundle_id
        self.apns_server = (
            "https://api.sandbox.push.apple.com"
            if use_sandbox
            else "https://api.push.apple.com"
        )

        # Load the private key from content or file
        if auth_key:
            # Key provided directly - check if it's base64 encoded
            try:
                # Try to decode as base64 first
                decoded = base64.b64decode(auth_key).decode("utf-8")
                if "-----BEGIN PRIVATE KEY-----" in decoded:
                    self.auth_key = decoded
                else:
                    # Not base64 encoded, use as-is
                    self.auth_key = auth_key
            except Exception:
                # Not base64 encoded, use as-is
                self.auth_key = auth_key
        elif auth_key_path:
            # Load from file
            with open(auth_key_path, "r") as f:
                self.auth_key = f.read()
        else:
            raise ValueError("Either auth_key or auth_key_path must be provided")

        # JWT token cache (valid for 1 hour)
        self._jwt_token: Optional[str] = None
        self._jwt_token_expires_at: float = 0

        logger.info(
            f"APNs service initialized (server={self.apns_server}, bundle_id={bundle_id})"
        )

    def _generate_jwt_token(self) -> str:
        """Generate JWT token for APNs authentication.

        APNs JWT tokens:
        - Algorithm: ES256 (ECDSA with P-256 curve)
        - Headers: alg, kid (key ID)
        - Claims: iss (team ID), iat (issued at)
        - Valid for: 1 hour (we cache for 58 minutes)

        Returns:
            JWT token string.
        """
        # Check cache
        if self._jwt_token and time.time() < self._jwt_token_expires_at:
            return self._jwt_token

        # Generate new token
        headers = {"alg": "ES256", "kid": self.key_id}

        payload = {"iss": self.team_id, "iat": int(time.time())}

        self._jwt_token = jwt.encode(
            payload, self.auth_key, algorithm="ES256", headers=headers
        )

        # Cache for 58 minutes (tokens valid for 60)
        self._jwt_token_expires_at = time.time() + 3480

        logger.debug("Generated new APNs JWT token")

        return self._jwt_token

    async def send_notification(
        self,
        device_token: str,
        payload: Dict[str, Any],
        priority: int = 10,
        collapse_id: Optional[str] = None,
    ) -> Tuple[bool, int, Optional[str]]:
        """Send push notification to a single device.

        Args:
            device_token: APNs device token (64 hex chars).
            payload: Notification payload dict.
            priority: 5 (conserve power) or 10 (immediate).
            collapse_id: Optional collapse identifier.

        Returns:
            Tuple of (success, status_code, error_reason):
            - success: True if 200 response
            - status_code: HTTP status code
            - error_reason: APNs error reason (if failed)

        APNs Status Codes:
            200: Success
            400: Bad request (malformed JSON/headers)
            403: Invalid auth token or topic
            405: Invalid method
            410: Device token invalid/expired → REMOVE TOKEN
            413: Payload too large (>4KB)
            429: Too many requests
            500/503: Server error → RETRY
        """
        url = f"{self.apns_server}/3/device/{device_token}"

        headers = {
            "authorization": f"bearer {self._generate_jwt_token()}",
            "apns-topic": self.bundle_id,
            "apns-priority": str(priority),
            "apns-push-type": "alert",
        }

        if collapse_id:
            headers["apns-collapse-id"] = collapse_id

        async with httpx.AsyncClient(http2=True) as client:
            try:
                response = await client.post(
                    url, json=payload, headers=headers, timeout=30.0
                )

                status_code = response.status_code

                if status_code == 200:
                    logger.info(f"APNs notification sent to {device_token[:10]}...")
                    return True, status_code, None

                # Parse error response
                error_reason = None
                try:
                    error_data = response.json()
                    error_reason = error_data.get("reason")
                except Exception:
                    # APNs error body may be non-JSON; status code is still logged.
                    pass

                logger.warning(
                    f"APNs failed: {status_code} {error_reason} "
                    f"for token {device_token[:10]}..."
                )

                return False, status_code, error_reason

            except Exception as e:
                logger.error(f"APNs exception: {e}")
                return False, 0, str(e)
