"""
Utility functions for tracker implementations.
"""

import asyncio
import logging
from functools import wraps
from typing import Any, Callable, List, Optional, Type, Union

logger = logging.getLogger(__name__)

# Constants for common API limits and defaults
JIRA_MAX_COMMENTS_PER_ISSUE = 20
JIRA_MAX_CHANGELOG_ITEMS = 40
GITHUB_DEFAULT_PAGE_SIZE = 100
GITLAB_DEFAULT_PAGE_SIZE = 100
JIRA_DEFAULT_PAGE_SIZE = 100
MAX_RETRY_ATTEMPTS = 3
DEFAULT_RETRY_DELAY = 1.0

# HTTP Status Code Constants
HTTP_STATUS_OK = 200
HTTP_STATUS_CREATED = 201
HTTP_STATUS_NO_CONTENT = 204
HTTP_STATUS_UNAUTHORIZED = 401
HTTP_STATUS_NOT_FOUND = 404
HTTP_STATUS_CONFLICT = 409
HTTP_STATUS_UNPROCESSABLE_ENTITY = 422

# HTTP Status Code Ranges
HTTP_SUCCESS_MIN = 200
HTTP_SUCCESS_MAX = 299


def is_http_error(error: Exception, status_code: int) -> bool:
    """
    Standardized HTTP error detection across different tracker clients.

    Args:
        error: The exception to check
        status_code: The HTTP status code to check for

    Returns:
        True if the error indicates the specified HTTP status code
    """
    # Check if error has response_code attribute (python-gitlab style)
    if hasattr(error, "response_code") and error.response_code == status_code:
        return True

    # Check if error has status_code attribute (requests style)
    if hasattr(error, "status_code") and error.status_code == status_code:
        return True

    # Check if error has response with status_code (httpx style)
    if hasattr(error, "response") and hasattr(error.response, "status_code"):
        if error.response.status_code == status_code:
            return True

    # Check if status code appears in string representation (as a fallback)
    # Use word boundaries to avoid false positives from memory addresses
    error_str = str(error)
    status_str = str(status_code)

    # Common patterns in error messages:
    # - "404 Not Found"  -> starts with code followed by space
    # - "Error 404: "    -> code followed by colon
    # - "status 404 "    -> code surrounded by spaces
    if (
        error_str.startswith(f"{status_str} ")
        or error_str.startswith(f"{status_str}:")
        or f" {status_str} " in error_str
        or f" {status_str}:" in error_str
        or f":{status_str} " in error_str
        or f":{status_str}:" in error_str
    ):
        return True

    return False


def is_not_found_error(error: Exception) -> bool:
    """Check if an error indicates a 404 Not Found response."""
    return is_http_error(error, HTTP_STATUS_NOT_FOUND)


def is_authentication_error(error: Exception) -> bool:
    """Check if an error indicates a 401 Unauthorized response."""
    return is_http_error(error, HTTP_STATUS_UNAUTHORIZED)


def is_conflict_error(error: Exception) -> bool:
    """Check if an error indicates a 409 Conflict response."""
    return is_http_error(error, HTTP_STATUS_CONFLICT)


def async_retry(
    max_attempts: int = MAX_RETRY_ATTEMPTS,
    delay: float = DEFAULT_RETRY_DELAY,
    exceptions: Union[Type[Exception], tuple] = Exception,
    backoff_factor: float = 1.0,
):
    """
    Async retry decorator for handling transient failures.

    Args:
        max_attempts: Maximum number of retry attempts
        delay: Initial delay between retries in seconds
        exceptions: Exception types to retry on
        backoff_factor: Multiplier for delay after each retry
    """

    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(*args, **kwargs):
            current_delay = delay
            last_exception = None

            for attempt in range(max_attempts):
                try:
                    return await func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    if attempt == max_attempts - 1:
                        # Last attempt, re-raise the exception
                        raise

                    logger.warning(
                        f"Attempt {attempt + 1}/{max_attempts} failed for {func.__name__}: {e}. "
                        f"Retrying in {current_delay}s..."
                    )
                    await asyncio.sleep(current_delay)
                    current_delay *= backoff_factor

            # Unreachable if max_attempts >= 1; keep mypy/CodeQL happy.
            if last_exception is not None:
                raise last_exception
            raise RuntimeError(
                f"retry_async exhausted without an exception for {func.__name__}"
            )

        return wrapper

    return decorator


def extract_repo_name_from_url(url: str) -> Optional[str]:
    """
    Extract repository name from various Git URL formats.

    Args:
        url: Git repository URL

    Returns:
        Repository name in 'owner/repo' format, or None if parsing fails
    """
    import re

    patterns = [
        r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?/?$",
        r"gitlab\.com[:/]([^/]+/[^/]+?)(?:\.git)?/?$",
    ]

    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)

    return None


def truncate_description(description: str, max_length: int) -> str:
    """
    Truncate a description to fit within database constraints.

    Args:
        description: The original description
        max_length: Maximum allowed length

    Returns:
        Truncated description with indicator if truncation occurred
    """
    if not description or len(description) <= max_length:
        return description

    truncation_indicator = "... [content truncated]"
    available_length = max_length - len(truncation_indicator)

    if available_length <= 0:
        return truncation_indicator[:max_length]

    return description[:available_length] + truncation_indicator


def normalize_datetime_string(dt_str: str) -> str:
    """
    Normalize datetime strings from different APIs to ISO format.

    Args:
        dt_str: Datetime string in various formats

    Returns:
        Normalized ISO format datetime string
    """
    from datetime import datetime

    # Common patterns from different APIs
    patterns = [
        "%Y-%m-%dT%H:%M:%S.%fZ",  # GitHub format
        "%Y-%m-%dT%H:%M:%S.%f%z",  # Jira format with timezone
        "%Y-%m-%dT%H:%M:%SZ",  # Simple ISO format
        "%Y-%m-%dT%H:%M:%S%z",  # ISO with timezone
    ]

    for pattern in patterns:
        try:
            dt = datetime.strptime(dt_str, pattern)
            return dt.isoformat()
        except ValueError:
            continue

    # If no pattern matches, return as-is
    logger.warning(f"Could not normalize datetime string: {dt_str}")
    return dt_str


def safe_dict_get(data: dict, keys: List[str], default: Any = None) -> Any:
    """
    Safely navigate nested dictionary structures.

    Args:
        data: The dictionary to navigate
        keys: List of keys to traverse (e.g., ['user', 'profile', 'name'])
        default: Default value if any key is missing

    Returns:
        The value at the nested path, or default if not found
    """
    current = data
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            return default
        current = current[key]
    return current
