import logging
import math
import os

from preloop.utils.sentry_filters import (
    sentry_before_send,
    sentry_before_send_transaction,
)

logger = logging.getLogger(__name__)


def _profiles_sample_rate() -> float:
    """Enable cross-thread stack sampling only with explicit configuration."""
    raw = os.getenv("SENTRY_PROFILES_SAMPLE_RATE", "0")
    try:
        rate = float(raw)
    except ValueError:
        rate = float("nan")
    if not math.isfinite(rate) or not 0 <= rate <= 1:
        logger.warning("Invalid SENTRY_PROFILES_SAMPLE_RATE; profiling disabled")
        return 0.0
    return rate


def init_sentry() -> None:
    # Initialize Sentry if DSN is configured
    sentry_dsn = os.getenv("SENTRY_DSN")
    if sentry_dsn:
        import sentry_sdk  # noqa: F401

        sentry_env = ""
        try:
            from preloop.config import settings

            preloop_url = settings.preloop_url
        except ImportError:
            preloop_url = os.getenv("PRELOOP_URL")
        if preloop_url == "https://staging.preloop.ai":
            sentry_env = "staging"
        elif preloop_url == "https://preloop.ai":
            sentry_env = "production"
        elif "localhost" in preloop_url:
            sentry_env = "development"
        elif preloop_url:
            sentry_env = preloop_url.split(".")[0].split("//")[-1]

        if sentry_env:
            sentry_sdk.init(
                dsn=sentry_dsn,
                # Set traces_sample_rate to 1.0 to capture 100%
                # of transactions for performance monitoring.
                traces_sample_rate=1.0,
                # Error reporting and tracing do not require the background
                # stack sampler. Keep native frame introspection opt-in.
                profiles_sample_rate=_profiles_sample_rate(),
                enable_tracing=True,
                environment=sentry_env,
                before_send=sentry_before_send,
                before_send_transaction=sentry_before_send_transaction,
            )
            logger.info("Sentry SDK initialized.")
