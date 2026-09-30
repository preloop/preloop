"""Standalone runner client. Secrets travel through headers, stdin and stdout only.

Stdout is consumed by the publication wrapper's command substitution, never logs.
"""

import json
import os
import subprocess
import tempfile
import urllib.request
from urllib.parse import urlsplit


def refresh() -> str:
    """Replace stale git storage and return the same fresh token for PR REST calls."""
    repository_url = os.environ["PRELOOP_PUBLICATION_REPOSITORY"]
    origin = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if origin != repository_url:
        raise ValueError("publication origin changed")
    request = urllib.request.Request(
        os.environ["PRELOOP_PUBLICATION_REFRESH_URL"],
        method="POST",
        headers={
            "Authorization": "Bearer "
            + os.environ["PRELOOP_PUBLICATION_REFRESH_CAPABILITY"]
        },
    )
    with urllib.request.urlopen(request, timeout=45) as response:
        data = json.loads(response.read(65536))
    token = data["token"]
    if not isinstance(token, str) or not token or any(c in token for c in "\r\n"):
        raise ValueError("invalid credential")
    repository = urlsplit(os.environ["PRELOOP_PUBLICATION_REPOSITORY"])
    # A fresh private store plus a local helper reset prevents an expired
    # global clone store from winning. Other repositories retain their helper.
    descriptor, store = tempfile.mkstemp(prefix=".preloop-publication-", dir="/tmp")
    os.close(descriptor)
    for args in (
        ["git", "config", "--local", "--replace-all", "credential.helper", ""],
        [
            "git",
            "config",
            "--local",
            "--add",
            "credential.helper",
            "store --file=" + store,
        ],
        ["git", "config", "--local", "credential.useHttpPath", "true"],
    ):
        subprocess.run(
            args, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
    scope = f"protocol=https\nhost={repository.netloc}\npath={repository.path.lstrip('/')}\nusername=x-access-token\n"
    subprocess.run(
        ["git", "credential", "approve"],
        input=scope + "password=" + token + "\n\n",
        text=True,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    os.chmod(store, 0o600)
    os.environ.pop("PRELOOP_GIT_CREDENTIALS", None)
    return token


if __name__ == "__main__":
    try:
        print(refresh())
    except Exception:
        # Provider responses and subprocess errors may contain secrets.
        raise SystemExit("Publication credential refresh failed") from None
