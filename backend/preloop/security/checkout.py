"""Clone a caller-supplied repository onto the Preloop API server.

MCP tools run here, not as a stdio process in the agent sandbox. The
agent passes the flow git-clone URL (or another repository URL). The
checkout is a temporary work tree and is deleted when the scan returns.
"""

from __future__ import annotations

import logging
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

ALLOWED_SCHEMES = frozenset({"http", "https", "file"})
CLONE_TIMEOUT_SEC = 180
CHECKOUT_TIMEOUT_SEC = 60


class CheckoutError(ValueError):
    """Raised when a repository cannot be cloned for a scanner tool."""


def validate_repository_url(repository_url: str) -> str:
    """Return a stripped URL if the scheme is allowed.

    Args:
        repository_url: Caller-supplied git URL.

    Returns:
        The stripped URL.

    Raises:
        CheckoutError: If the URL is empty or uses a disallowed scheme.
    """
    url = (repository_url or "").strip()
    if not url:
        raise CheckoutError("repository_url is required")
    parsed = urlparse(url)
    if parsed.scheme not in ALLOWED_SCHEMES:
        raise CheckoutError(
            f"repository_url scheme {parsed.scheme!r} is not allowed "
            f"(use http, https, or file)"
        )
    return url


@contextmanager
def checkout_repository(
    repository_url: str,
    ref: Optional[str] = None,
) -> Iterator[Path]:
    """Clone ``repository_url`` into a temp directory and yield the path.

    History is kept (no ``--depth 1``) so gitleaks can scan commits.
    ``ref`` is checked out after the clone when provided.

    Args:
        repository_url: http(s) or file git URL.
        ref: Optional branch, tag, or commit to check out.

    Yields:
        Path to the cloned work tree.

    Raises:
        CheckoutError: If the URL is invalid or git clone/checkout fails.
    """
    url = validate_repository_url(repository_url)
    with tempfile.TemporaryDirectory(prefix="preloop-scanner-") as tmp:
        dest = Path(tmp) / "repo"
        clone_cmd = ["git", "clone", "--quiet", url, str(dest)]
        try:
            clone = subprocess.run(
                clone_cmd,
                check=False,
                capture_output=True,
                text=True,
                timeout=CLONE_TIMEOUT_SEC,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CheckoutError(f"git clone failed: {exc}") from exc
        if clone.returncode != 0:
            err = (clone.stderr or clone.stdout or "git clone failed").strip()
            raise CheckoutError(f"git clone failed: {err}")
        if ref:
            checkout = subprocess.run(
                ["git", "-C", str(dest), "checkout", "--quiet", "--detach", ref],
                check=False,
                capture_output=True,
                text=True,
                timeout=CHECKOUT_TIMEOUT_SEC,
            )
            if checkout.returncode != 0:
                err = (checkout.stderr or checkout.stdout or "checkout failed").strip()
                raise CheckoutError(f"git checkout {ref!r} failed: {err}")
        logger.info("Cloned repository for scanner into %s", dest)
        yield dest
