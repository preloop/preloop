"""The lifespan reads ``settings`` at startup; a missing import only shows in production."""

from preloop.api import app as app_module
from preloop.config import settings


def test_app_module_binds_settings_for_lifespan() -> None:
    # Tests run with is_testing set, so the webhook delivery branch that reads
    # settings.webhook_delivery_enabled is skipped and a NameError there would
    # never surface here. Bind the name explicitly so the regression is caught.
    assert app_module.settings is settings
    assert isinstance(settings.webhook_delivery_enabled, bool)
