"""Give each pytest-xdist worker its own clone of the CI database."""

from __future__ import annotations

from typing import MutableMapping

DATABASE_URL_VARIABLES = (
    "DATABASE_URL",
    "FLOW_FEEDBACK_TEST_DATABASE_URL",
    "CHAT_TEST_DATABASE_URL",
)


def use_worker_database(environ: MutableMapping[str, str]) -> None:
    """Point an xdist worker at ``<db>_<worker>`` when CI created the clones.

    ``scripts/run_backend_shard.py`` clones the job database once per worker
    and sets ``PRELOOP_CI_WORKER_DATABASES``. Outside that (local runs, the
    xdist controller, no xdist) the URLs are left unchanged.

    Args:
        environ: The process environment, updated in place.
    """
    worker = environ.get("PYTEST_XDIST_WORKER", "")
    if not worker or environ.get("PRELOOP_CI_WORKER_DATABASES") != "1":
        return
    for name in DATABASE_URL_VARIABLES:
        url = environ.get(name)
        if url and "?" not in url:
            environ[name] = f"{url}_{worker}"
