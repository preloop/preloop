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
import os, select, shlex, signal, subprocess, sys, tempfile, time

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
    # End on the child's exit, not on pipe EOF: a background process the
    # script left behind keeps the pipe open and must not keep us alive.
    descriptor = process.stdout.fileno()
    os.set_blocking(descriptor, False)
    drain_until = None
    at_eof = False
    while True:
        exited = process.poll() is not None
        if exited and drain_until is None:
            drain_until = time.monotonic() + 2
        if not select.select([descriptor], [], [], 0 if exited else 0.1)[0]:
            if exited:
                break
            continue
        try:
            chunk = os.read(descriptor, 65536)
        except BlockingIOError:
            continue
        if not chunk:
            at_eof = True
            break
        sys.stdout.buffer.write(stream.feed(chunk))
        sys.stdout.buffer.flush()
        if exited and time.monotonic() > drain_until:
            break
    # Short of EOF a leftover process may be part-way through a credential;
    # its beginning must not be released once the rest will never be read.
    sys.stdout.buffer.write(stream.finish() if at_eof else stream.cut())
    sys.stdout.buffer.flush()
    code = process.wait()
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except OSError:
        pass
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
