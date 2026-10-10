"""Push notification proxy for open-source instances.

Open-source Preloop users can enable push notifications by:
1. Creating a free account at https://preloop.ai
2. Generating an API key with push_proxy scope
3. Setting these environment variables:
   - PUSH_PROXY_URL=https://preloop.ai/api/v1/push/proxy
   - PUSH_PROXY_API_KEY=<your-api-key>

This allows OSS instances to send push notifications through the
production Preloop servers without needing their own APNs/FCM credentials.
"""

import logging
import os
from typing import Optional, Dict, Any, List

import httpx

logger = logging.getLogger(__name__)

# Configuration
PUSH_PROXY_URL = os.getenv("PUSH_PROXY_URL")
PUSH_PROXY_API_KEY = os.getenv("PUSH_PROXY_API_KEY")


def is_push_proxy_configured() -> bool:
    """Check if push notification proxy is configured."""
    return bool(PUSH_PROXY_URL and PUSH_PROXY_API_KEY)


async def send_push_via_proxy(
    platform: str,
    device_token: str,
    title: str,
    body: str,
    data: Optional[Dict[str, Any]] = None,
    **kwargs,
) -> Dict[str, Any]:
    """Send a push notification via the proxy service.

    Args:
        platform: 'ios' or 'android'
        device_token: Device token
        title: Notification title
        body: Notification body
        data: Custom data payload
        **kwargs: Additional options (badge, sound, priority)

    Returns:
        Dict with success status and details
    """
    if not is_push_proxy_configured():
        return {
            "success": False,
            "error": "Push proxy not configured. Set PUSH_PROXY_URL and PUSH_PROXY_API_KEY.",
        }

    try:
        payload = {
            "platform": platform,
            "device_token": device_token,
            "title": title,
            "body": body,
            "data": data or {},
            **{k: v for k, v in kwargs.items() if v is not None},
        }

        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                PUSH_PROXY_URL,
                json=payload,
                headers={
                    "X-API-Key": PUSH_PROXY_API_KEY,
                    "Content-Type": "application/json",
                },
            )

            if response.status_code == 200:
                result = response.json()
                if result.get("success"):
                    # The title is caller-supplied and may carry sensitive text
                    # (a flow name or alert summary); log only the outcome.
                    logger.debug("Push sent via proxy")
                    return {"success": True, "result": result.get("details")}
                else:
                    return {"success": False, "error": result.get("error")}
            elif response.status_code == 401:
                return {"success": False, "error": "Invalid API key for push proxy"}
            elif response.status_code == 403:
                return {
                    "success": False,
                    "error": "API key does not have push_proxy permission",
                }
            else:
                return {
                    "success": False,
                    "error": f"Push proxy returned HTTP {response.status_code}",
                }

    except httpx.TimeoutException:
        logger.error("Push proxy request timed out")
        return {"success": False, "error": "Push proxy request timed out"}
    except Exception as e:
        logger.error(f"Push proxy error: {e}")
        return {"success": False, "error": str(e)}


async def send_bulk_push_via_proxy(
    notifications: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Send multiple push notifications via the proxy service.

    Args:
        notifications: List of notification dicts with platform, device_token, etc.

    Returns:
        List of results for each notification
    """
    if not is_push_proxy_configured():
        return [
            {
                "success": False,
                "error": "Push proxy not configured",
            }
            for _ in notifications
        ]

    bulk_url = PUSH_PROXY_URL.rstrip("/") + "/bulk"

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                bulk_url,
                json={"notifications": notifications},
                headers={
                    "X-API-Key": PUSH_PROXY_API_KEY,
                    "Content-Type": "application/json",
                },
            )

            if response.status_code == 200:
                return response.json()
            else:
                error = f"Push proxy returned HTTP {response.status_code}"
                return [{"success": False, "error": error} for _ in notifications]

    except Exception as e:
        logger.error(f"Bulk push proxy error: {e}")
        return [{"success": False, "error": str(e)} for _ in notifications]


__all__ = [
    "is_push_proxy_configured",
    "send_push_via_proxy",
    "send_bulk_push_via_proxy",
]
