"""
Utility functions for preloop.sync.
"""

import sys
import time
from functools import wraps
from typing import Any, Callable, TypeVar, cast

from .config import logger

T = TypeVar("T")


def retry(
    max_attempts: int = 3, backoff_factor: float = 1.0, exceptions: tuple = (Exception,)
) -> Callable:
    """
    Retry decorator with exponential backoff.

    Args:
        max_attempts: Maximum number of retry attempts.
        backoff_factor: Backoff factor for retry intervals.
        exceptions: Tuple of exceptions to catch and retry.

    Returns:
        Decorated function with retry logic.
    """

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            attempt = 0
            while attempt < max_attempts:
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    attempt += 1
                    if attempt >= max_attempts:
                        logger.error(
                            f"Failed after {max_attempts} attempts: {type(e).__name__}"
                        )
                        raise

                    wait_time = backoff_factor * (2 ** (attempt - 1))
                    logger.warning(
                        f"Attempt {attempt} failed with {type(e).__name__}. Retrying in {wait_time:.2f} seconds."
                    )
                    time.sleep(wait_time)

            # This should never be reached due to the raise in the except block
            return cast(T, None)

        return wrapper

    return decorator


def safe_exit(exit_code: int = 1, message: str = None) -> None:
    """
    Exit the application safely with an optional message.

    Args:
        exit_code: Exit code to use (default: 1).
        message: Optional message to log before exiting.
    """
    if message:
        logger.error(message)
    sys.exit(exit_code)
