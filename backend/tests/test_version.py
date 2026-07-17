"""Test the package version."""

import preloop
from preloop.config import SERVER_VERSION


def test_version():
    """Test that the version is a string."""
    assert isinstance(preloop.__version__, str)
    assert preloop.__version__ != ""


def test_version_matches_server_version():
    """preloop.__version__ must resolve like config.SERVER_VERSION.

    Guards against the historical bug where __init__.py hardcoded "0.1.0"
    and every self-hosted install reported itself as v0.1.0 to the tracker.
    """
    assert preloop.__version__ == SERVER_VERSION
    assert preloop.__version__ != "0.1.0"
