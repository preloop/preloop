"""Standalone output masking and private credential handoff for image setup."""

import inspect
import textwrap

from preloop.utils import execution_credentials

_RESTORE = r"""
import json, os, sys
path = sys.argv[1]
with open(path) as source:
    credentials = json.load(source)
os.unlink(path)
os.rmdir(os.path.dirname(path))
os.environ.update(credentials)
os.execvpe("bash", ["bash", "--noprofile", "--norc", *sys.argv[2:]], os.environ)
"""

_MAIN = r"""
import os, shlex, signal, subprocess, sys, tempfile

environment = dict(os.environ)
environment.pop("PRELOOP_OUTPUT_BOOTSTRAP", None)
values = credential_values(environment)
masker = CredentialMasker.from_values(values)
credentials = {
    key: environment.pop(key) for key in list(environment)
    if _SECRET_NAME.search(key) or key == "MCP_CONFIG_JSON"
    or credential_values({key: environment[key]})
}
directory = tempfile.mkdtemp(prefix="preloop-auth-")
path = os.path.join(directory, "environment.json")
with open(path, "w", opener=lambda name, flags: os.open(name, flags, 0o600)) as target:
    json.dump(credentials, target)
restore = "exec python3 -c " + shlex.quote(RESTORE) + " " + shlex.quote(path)
restore += " " + " ".join(shlex.quote(arg) for arg in sys.argv[2:])
process = None
try:
    process = subprocess.Popen(
        [sys.argv[1], "-c", restore], env=environment,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True,
    )
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda sig, frame: os.killpg(process.pid, sig))
    stream = CredentialStream(masker)
    while True:
        chunk = process.stdout.read1(65536)
        if not chunk:
            break
        sys.stdout.buffer.write(stream.feed(chunk))
        sys.stdout.buffer.flush()
    sys.stdout.buffer.write(stream.finish())
    sys.stdout.buffer.flush()
    code = process.wait()
finally:
    if os.path.exists(path):
        os.unlink(path)
    if os.path.isdir(directory):
        os.rmdir(directory)
sys.exit(code if code >= 0 else 128 - code)
"""


def bootstrap_source() -> str:
    """Render a dependency-free wrapper for hosted and private containers."""
    return (
        inspect.getsource(execution_credentials)
        + "\nRESTORE = "
        + repr(textwrap.dedent(_RESTORE))
        + "\n"
        + textwrap.dedent(_MAIN)
    )
