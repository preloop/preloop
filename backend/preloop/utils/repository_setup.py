"""Deliver generated repository setup as a file, outside process arguments.

Like workspace seeds, the transport carries base64 data and materializes it
before use. Gzip keeps the mounted object small; neither the setup nor its
credentials are copied into the job's arguments or environment.
"""

from __future__ import annotations

import base64
import gzip
import io
import json
import tarfile
from typing import Any

from preloop.utils.execve_limits import LaunchPayloadTooLargeError, chunked_env
from preloop.utils.git_credentials import GIT_CREDENTIALS_FILE

SETUP_CONTEXT_KEY = "_repository_setup"
SETUP_DIRECTORY = "/tmp/preloop-repository-setup"
SETUP_FILENAME = "setup.json.gz"
SETUP_PATH = f"{SETUP_DIRECTORY}/{SETUP_FILENAME}"
# Owner read only: the setup holds the git credential store, and the agent
# harness runs as a different user (setpriv) in root-started containers.
SETUP_FILE_MODE = 0o400
# Kubernetes' hard Secret data limit, not a repository-count policy.
MAX_SETUP_OBJECT_BYTES = 1024 * 1024
SETUP_ENV_PREFIX = "PRELOOP_REPOSITORY_SETUP_"


def setup_seed_env(encoded: str) -> dict[str, str]:
    """Use the existing chunked seed transport for private runner protocol v1."""
    return chunked_env(SETUP_ENV_PREFIX, encoded)


def setup_seed_shell() -> str:
    """Materialize private-runner data without changing its launch protocol.

    The fixed shell loop reads the same chunks used by workspace/prompt seeds.
    Remove them before invoking children; credentials never enter the script.
    """
    return f"""mkdir -p {SETUP_DIRECTORY} && (
set -eu
umask 077
: > {SETUP_PATH}.chunks
_pl_i=0
while [ "$_pl_i" -lt "${{{SETUP_ENV_PREFIX}CHUNKS}}" ]; do
    eval "_pl_chunk=\\${{{SETUP_ENV_PREFIX}$_pl_i:-}}"
    [ -n "$_pl_chunk" ] || exit 1
    printf '%s' "$_pl_chunk" >> {SETUP_PATH}.chunks
    _pl_i=$((_pl_i + 1))
done
base64 -d < {SETUP_PATH}.chunks > {SETUP_PATH}.base64
[ "$(wc -c < {SETUP_PATH}.base64 | tr -d ' ')" = "${{{SETUP_ENV_PREFIX}BYTES}}" ]
base64 -d < {SETUP_PATH}.base64 > {SETUP_PATH}
rm -f {SETUP_PATH}.chunks {SETUP_PATH}.base64
) || exit 1
_pl_i=0
while [ "$_pl_i" -lt "${{{SETUP_ENV_PREFIX}CHUNKS}}" ]; do
    unset "{SETUP_ENV_PREFIX}$_pl_i"
    _pl_i=$((_pl_i + 1))
done
unset {SETUP_ENV_PREFIX}CHUNKS {SETUP_ENV_PREFIX}BYTES
"""


def encode_setup(script: str, credentials: str, repository_count: int) -> str:
    """Encode the setup file and enforce the transport's existing hard limit."""
    data = gzip.compress(
        json.dumps({"script": script, "credentials": credentials}).encode(), mtime=0
    )
    encoded = base64.b64encode(data).decode("ascii")
    # Secret's hard cap applies to decoded data. Base64 plus metadata still
    # fits below the API server/etcd object limit; do not add a lower policy cap.
    if len(data) > MAX_SETUP_OBJECT_BYTES:
        raise LaunchPayloadTooLargeError(
            f"Repository setup for {repository_count} repositories is "
            f"{len(data)} bytes; the mounted setup object limit is "
            f"{MAX_SETUP_OBJECT_BYTES} bytes. The report could not start."
        )
    return encoded


def setup_archive(encoded: str, *, uid: int, gid: int) -> bytes:
    """Docker's pre-start file upload, using the workspace restore mechanism.

    The file belongs to the user that runs the container's init commands and
    only that user can read it; the bootstrap deletes it after extraction.
    """
    output = io.BytesIO()
    data = base64.b64decode(encoded, validate=True)
    with tarfile.open(fileobj=output, mode="w") as archive:
        directory = tarfile.TarInfo(SETUP_DIRECTORY.lstrip("/"))
        directory.type = tarfile.DIRTYPE
        directory.mode, directory.uid, directory.gid = 0o755, uid, gid
        archive.addfile(directory)
        entry = tarfile.TarInfo(SETUP_PATH.lstrip("/"))
        entry.mode, entry.uid, entry.gid = SETUP_FILE_MODE, uid, gid
        entry.size = len(data)
        archive.addfile(entry, io.BytesIO(data))
    return output.getvalue()


def setup_shell() -> str:
    """Constant bootstrap that reads the mounted file and preserves shell state.

    The transport file is removed once extracted where the filesystem allows
    it (Docker upload); a Kubernetes Secret mount is read only and owner-only.
    """
    return f"""(umask 077; python3 - <<'PRELOOP_REPOSITORY_SETUP'
import gzip, json, os
from pathlib import Path
with gzip.open('{SETUP_PATH}', 'rt') as source:
    setup = json.load(source)
root = Path('/tmp/preloop')
root.mkdir(mode=0o700, parents=True, exist_ok=True)
for path, content in ((root / 'repository-setup.sh', setup['script']),
                      (Path('{GIT_CREDENTIALS_FILE}'), setup['credentials'])):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, 'w') as destination:
        destination.write(content)
PRELOOP_REPOSITORY_SETUP
) && {{ rm -f {SETUP_PATH} 2>/dev/null || :; }} && . /tmp/preloop/repository-setup.sh"""


def repository_count(context: dict[str, Any]) -> int:
    """Count the resolved repositories without exposing names or credentials."""
    return len((context.get("git_clone_config") or {}).get("repositories") or [])
