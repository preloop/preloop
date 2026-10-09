import os
import logging

from preloop.utils.sentry_filters import (
    sentry_before_send,
    sentry_before_send_transaction,
)

logger = logging.getLogger(__name__)


def init_sentry():
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
                # Set profiles_sample_rate to 1.0 to profile 100%
                # of sampled transactions.
                profiles_sample_rate=1.0,
                enable_tracing=True,
                environment=sentry_env,
                before_send=sentry_before_send,
                before_send_transaction=sentry_before_send_transaction,
            )
            logger.info("Sentry SDK initialized.")
