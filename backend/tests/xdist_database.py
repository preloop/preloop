"""Give each pytest-xdist worker its own clone of the CI database."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping, MutableMapping
from urllib.parse import urlsplit, urlunsplit

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
        if url:
            parts = urlsplit(url)
            environ[name] = urlunsplit(parts._replace(path=f"{parts.path}_{worker}"))


def check_job_database(environ: Mapping[str, str]) -> None:
    """Fail loudly when ``DATABASE_URL`` is not this CI job's database.

    ``scripts/ci_postgres.py prepare`` records the job database in
    ``$RUNNER_TEMP/preloop-ci-postgres.json``. A URL that names another
    server or database (for example a fake one a test leaked into
    ``GITHUB_ENV``) would otherwise surface as "connection refused".
    Outside GitHub Actions, or without a marker, nothing is checked.

    Args:
        environ: The process environment.

    Raises:
        RuntimeError: The URL disagrees with the marker.
    """
    runner_temp = environ.get("RUNNER_TEMP")
    url = environ.get("DATABASE_URL")
    if environ.get("GITHUB_ACTIONS") != "true" or not runner_temp or not url:
        return
    marker_path = Path(runner_temp) / "preloop-ci-postgres.json"
    if not marker_path.is_file():
        return
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    expected = str(marker["database"])
    parts = urlsplit(url)
    database = parts.path.lstrip("/")
    worker = environ.get("PYTEST_XDIST_WORKER", "")
    allowed = {expected, f"{expected}_{worker}"} if worker else {expected}
    if (
        database not in allowed
        or parts.hostname != marker["host"]
        or parts.port != int(marker["port"])
    ):
        raise RuntimeError(
            f"DATABASE_URL points at {parts.hostname}:{parts.port}/{database}, "
            f"but ci_postgres.py prepared {marker['host']}:{marker['port']}/"
            f"{expected} ({marker_path}). Something exported a different URL, "
            "for example a test that wrote to GITHUB_ENV."
        )
