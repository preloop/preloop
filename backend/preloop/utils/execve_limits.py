"""Keep agent launch payloads inside the kernel's ``execve`` limits.

Why this module exists
----------------------

Starting an agent is an ``execve``. Linux caps *one* string handed to
``execve`` (a single ``argv`` element **or** a single ``NAME=value``
environment entry) at ``MAX_ARG_STRLEN``, which is 32 pages, i.e. 128 KiB
on every architecture Preloop runs on. Exceeding it does not truncate: the
kernel returns ``E2BIG`` and the container runtime reports the opaque
``exec /bin/bash: argument list too long``, with no hint about which string
was too long or by how much.

``preloop.utils.workspace_seed`` learned this the hard way in
preloop/preloop#505 and moved seed files out of the launch command into one
environment variable per file, capped at 96 KiB. The rendered prompt and the
generated launch script were left behind, and they are the two strings that
actually grow without bound: the prompt carries whatever a webhook payload
interpolated into it (a Dependabot pull request body is tens of KiB of
release-note HTML), and the script used to carry the prompt *again*,
base64-encoded, which is a 4/3 expansion on top.

What this module provides
-------------------------

1. :func:`chunked_env` / :func:`build_chunk_materialization_shell`: the
   transport. Text of any size travels as N base64 chunks of at most
   :data:`CHUNK_BYTES`, one environment variable each, and the emitted shell
   block reassembles them into a file inside the container. No caller ever
   puts unbounded text into a single ``execve`` string again.

2. :func:`check_launch_payload`: the guard. Called just before a Job or a
   container is created, it measures every string the launch would pass and
   raises :class:`LaunchPayloadTooLargeError` naming the offending item and
   its size, so the execution fails with something a human can act on
   instead of the runtime's ``argument list too long``.

The two ceilings
----------------

``MAX_ARG_STRLEN`` (128 KiB) bounds one string.  ``ARG_MAX`` bounds the sum
of argv plus environment; Linux derives it from the stack rlimit (one
quarter of it), so the usual 8 MiB stack yields 2 MiB. Kubernetes adds its
own environment entries (service discovery variables, the downward API) to
whatever the Job spec declares, and the whole Job object must also stay well
under etcd's ~1.5 MiB limit. :data:`MAX_LAUNCH_TOTAL_BYTES` is therefore set
to 1 MiB, half the typical ``ARG_MAX``, leaving the other half for the
runtime's own contribution.
"""

from __future__ import annotations

import base64
import shlex
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

# Kernel cap on a single execve string (argv element or NAME=value entry).
# 32 pages of 4 KiB. Not configurable, not per-distro: it is
# ``MAX_ARG_STRLEN`` in ``include/uapi/linux/binfmts.h``.
MAX_ARG_STRLEN = 128 * 1024  # 131072

# Headroom below the kernel cap. A container runtime may prepend to what we
# declare (an entrypoint wrapper, an injected variable name), and a payload
# that lands 40 bytes under a hard kernel limit is a payload that will fail
# on the next unrelated change. The guard refuses at this number, which is
# what makes the failure a Preloop error message rather than an ``E2BIG``.
EXECVE_STRING_RESERVE = 4 * 1024
MAX_LAUNCH_STRING_BYTES = MAX_ARG_STRLEN - EXECVE_STRING_RESERVE  # 126976

# Bytes of base64 per chunk variable. Same number as
# ``workspace_seed.MAX_SINGLE_SEED_ENCODED_BYTES`` deliberately: one
# precedent, one constant to reason about, and it leaves 32 KiB of the
# kernel string budget for the variable name and any runtime prefix.
CHUNK_BYTES = 96 * 1024

# Budget for argv + environment together. Half of the ~2 MiB ``ARG_MAX`` a
# default 8 MiB stack yields, so the runtime's own environment additions
# cannot push the total over, and the Kubernetes Job object stays well
# inside etcd's ~1.5 MiB object limit.
MAX_LAUNCH_TOTAL_BYTES = 1024 * 1024

# Directory the materialization blocks write into. ``/tmp`` is writable in
# every agent image, for root and for the non-root UID alike, and it is not
# ``/workspace`` (which git clone relocates and the snapshotter archives).
LAUNCH_PAYLOAD_DIR = "/tmp/preloop"

# The rendered prompt, once reassembled inside the container. Agents read the
# prompt from here instead of from an environment variable or their own
# command line.
PROMPT_FILE_PATH = f"{LAUNCH_PAYLOAD_DIR}/prompt.txt"

# Prefix of the environment variables carrying the prompt chunks.
PROMPT_ENV_PREFIX = "PRELOOP_AGENT_PROMPT_"

# Name of the variable pointing agents at the materialized prompt file.
PROMPT_FILE_ENV = "AGENT_PROMPT_FILE"

# Legacy whole-prompt variable. Still set when the prompt fits comfortably
# inside one execve string, because agent images not built from this repo may
# read it; dropped (in favour of the chunks plus ``AGENT_PROMPT_FILE``) when
# it would be the string that breaks the launch.
LEGACY_PROMPT_ENV = "AGENT_PROMPT"

# A prompt above this size stops travelling in ``AGENT_PROMPT``. Well under
# ``MAX_LAUNCH_STRING_BYTES`` so the legacy variable is never the largest
# string in the payload when it is present at all.
MAX_LEGACY_PROMPT_BYTES = 64 * 1024

# Printed by the container-side guard when a harness is about to start a CLI
# whose prompt file is missing or empty. A CLI reading its prompt from stdin
# cannot tell "no prompt" from "an empty prompt", and an agent that runs on an
# empty prompt is worse than one that refuses to start: it burns a model call,
# it may push something, and nothing in the log says why. The marker makes the
# run fail with a cause a human can read, and
# ``preloop.agents.failure_analysis`` classifies it.
PROMPT_NOT_DELIVERED_MARKER = "PRELOOP_PROMPT_NOT_DELIVERED"


class LaunchPayloadTooLargeError(ValueError):
    """A launch would hand the kernel a string it must reject.

    Raised by :func:`check_launch_payload` *before* the Job or container is
    created, so the execution fails with the offending item named rather than
    with ``exec /bin/bash: argument list too long`` from inside a pod that
    never started.
    """


def _encoded(text: str) -> bytes:
    return text.encode("utf-8")


def chunk_text(text: str, *, chunk_bytes: int = CHUNK_BYTES) -> List[str]:
    """Split ``text`` into base64 chunks of at most ``chunk_bytes`` each.

    Base64 rather than raw slicing for two reasons: a UTF-8 code point must
    not be split across two variables, and an environment value cannot carry
    a NUL byte. Encoding first makes both moot, and the decoder is a single
    ``base64 -d`` in the container.

    An empty ``text`` yields an empty list, which
    :func:`build_chunk_materialization_shell` renders as "create an empty
    file" rather than as "nothing to do".
    """
    if chunk_bytes <= 0:
        raise ValueError("chunk_bytes must be positive")
    encoded = base64.b64encode(_encoded(text)).decode("ascii")
    return [
        encoded[offset : offset + chunk_bytes]
        for offset in range(0, len(encoded), chunk_bytes)
    ]


def chunk_count_env(prefix: str) -> str:
    """Name of the variable holding the chunk count for ``prefix``."""
    return f"{prefix}CHUNKS"


def chunk_bytes_env(prefix: str) -> str:
    """Name of the variable holding the DECODED byte length for ``prefix``."""
    return f"{prefix}BYTES"


def chunk_env_var(prefix: str, index: int) -> str:
    """Name of the variable carrying chunk ``index`` for ``prefix``."""
    return f"{prefix}{index}"


def chunked_env(prefix: str, text: str) -> Dict[str, str]:
    """Environment carrying ``text`` as base64 chunks under ``prefix``.

    Always includes the count and the decoded byte length, so the container
    side can tell "the text was empty" from "the transport dropped a
    variable", which is the difference between an agent that runs with no prompt and
    an agent that must not start at all.
    """
    chunks = chunk_text(text)
    env = {
        chunk_count_env(prefix): str(len(chunks)),
        chunk_bytes_env(prefix): str(len(_encoded(text))),
    }
    for index, chunk in enumerate(chunks):
        env[chunk_env_var(prefix, index)] = chunk
    return env


def build_chunk_materialization_shell(
    prefix: str,
    text: str,
    dest_path: str,
    *,
    label: Optional[str] = None,
) -> str:
    """Emit the shell block that rebuilds ``text`` at ``dest_path``.

    Paired with :func:`chunked_env` on the same ``text``, so the chunk count
    the block unrolls and the variables the environment carries cannot
    disagree.

    The block is a ``set -e`` subshell, like the workspace-seed prelude it is
    modelled on, and it verifies the reassembled size against the byte count
    the environment declares. A transport that silently drops one chunk
    (a truncated Job spec, a runtime with its own environment limit) then
    aborts the run loudly instead of handing the agent half a prompt, which
    is the failure mode nobody would have noticed until the output was wrong.

    ``printf`` is a builtin in bash and in every POSIX shell in the agent
    images, so appending a 96 KiB chunk never becomes an ``execve`` of its
    own; even if it did, a chunk is under the per-string limit by
    construction.
    """
    chunks = chunk_text(text)
    what = label or dest_path
    quoted_dest = shlex.quote(dest_path)
    quoted_tmp = shlex.quote(f"{dest_path}.b64")
    quoted_dir = shlex.quote(LAUNCH_PAYLOAD_DIR)
    quoted_what = shlex.quote(what)
    expected = len(_encoded(text))

    lines = [
        "( set -e",
        f"  mkdir -p {quoted_dir}",
        f"  : > {quoted_tmp}",
    ]
    for index in range(len(chunks)):
        name = chunk_env_var(prefix, index)
        lines.append(
            f'  [ -n "${{{name}:-}}" ] || {{ '
            f'echo "PRELOOP_LAUNCH_PAYLOAD_MISSING {name} for {what}" >&2; '
            "exit 1; }"
        )
        lines.append(f"  printf '%s' \"${{{name}}}\" >> {quoted_tmp}")
    lines.extend(
        [
            f"  base64 -d < {quoted_tmp} > {quoted_dest}",
            f"  rm -f {quoted_tmp}",
            f"  _pl_have=$(wc -c < {quoted_dest} | tr -d ' ')",
            f'  if [ "$_pl_have" != "{expected}" ]; then',
            f'    echo "PRELOOP_LAUNCH_PAYLOAD_TRUNCATED {quoted_what}: '
            f'got $_pl_have bytes, expected {expected}" >&2',
            "    exit 1",
            "  fi",
            ")",
        ]
    )
    return "\n".join(lines)


def prompt_transport_env(prompt: str) -> Dict[str, str]:
    """Environment that delivers ``prompt`` to a container.

    Returns the chunk variables, the path the container will materialize them
    at, and, only while the prompt is small enough that it cannot be the
    string that breaks the launch, the historic ``AGENT_PROMPT``. Images
    outside this repository may still read ``AGENT_PROMPT``; keeping it for
    the common case preserves them, and dropping it for the large case is
    strictly better than a launch that fails with ``E2BIG``.
    """
    env = chunked_env(PROMPT_ENV_PREFIX, prompt)
    env[PROMPT_FILE_ENV] = PROMPT_FILE_PATH
    if len(_encoded(prompt)) <= MAX_LEGACY_PROMPT_BYTES:
        env[LEGACY_PROMPT_ENV] = prompt
    return env


def build_prompt_materialization_shell(prompt: str) -> str:
    """Shell block writing the prompt to :data:`PROMPT_FILE_PATH`."""
    return build_chunk_materialization_shell(
        PROMPT_ENV_PREFIX,
        prompt,
        PROMPT_FILE_PATH,
        label="agent prompt",
    )


def prompt_stdin_redirect(path: str = PROMPT_FILE_PATH) -> str:
    """The redirection that feeds a prompt file to a CLI on stdin.

    Harnesses use this instead of interpolating ``"$(cat <file>)"`` into the
    CLI's command line. The command substitution put the whole prompt into one
    ``argv`` element, which the kernel caps at :data:`MAX_ARG_STRLEN`; the
    redirection puts nothing there, so the size of the longest argument stops
    depending on the size of the prompt.
    """
    return f"< {shlex.quote(path)}"


def build_prompt_delivery_guard(
    path: str = PROMPT_FILE_PATH,
    *,
    label: str = "agent prompt",
) -> str:
    """Shell block refusing to start a CLI whose prompt never arrived.

    Placed immediately before the invocation that redirects ``path`` onto the
    CLI's stdin. A CLI handed an empty stdin either errors with wording of its
    own or, worse, starts a session with no instructions; either way the log
    would not say that the prompt was the problem. The guard says it, with a
    marker :func:`preloop.agents.failure_analysis.analyze_agent_failure`
    recognises, and exits non-zero before a model is ever called.

    Args:
        path: The prompt file the CLI will read from stdin.
        label: What the file holds, for the operator-facing message.

    Returns:
        A bash block, safe to embed verbatim in an agent script.
    """
    quoted = shlex.quote(path)
    return (
        f"if [ ! -s {quoted} ]; then\n"
        f'    echo "{PROMPT_NOT_DELIVERED_MARKER} {label} at {path} '
        'is missing or empty" >&2\n'
        "    exit 1\n"
        "fi"
    )


@dataclass(frozen=True)
class LaunchString:
    """One string the launch would hand to ``execve``."""

    label: str
    size: int


def launch_strings(
    command: Optional[Sequence[str]] = None,
    args: Optional[Sequence[str]] = None,
    env: Optional[Mapping[str, Any]] = None,
) -> List[LaunchString]:
    """Every individual ``execve`` string a launch would pass, with sizes.

    An environment entry is measured as the kernel sees it, ``NAME=value``
    plus the terminating NUL, not as the value alone: a 126 KiB value under a
    30 character name is over the limit even though the value is not.
    """
    strings: List[LaunchString] = []
    for index, item in enumerate(command or []):
        text = "" if item is None else str(item)
        strings.append(
            LaunchString(f"command[{index}]", len(_encoded(text)) + 1),
        )
    for index, item in enumerate(args or []):
        text = "" if item is None else str(item)
        strings.append(LaunchString(f"args[{index}]", len(_encoded(text)) + 1))
    for name, value in (env or {}).items():
        text = "" if value is None else str(value)
        strings.append(
            LaunchString(
                f"env[{name}]",
                len(_encoded(f"{name}={text}")) + 1,
            )
        )
    return strings


def largest_launch_string(
    command: Optional[Sequence[str]] = None,
    args: Optional[Sequence[str]] = None,
    env: Optional[Mapping[str, Any]] = None,
) -> Optional[LaunchString]:
    """The single biggest ``execve`` string, or None for an empty launch."""
    strings = launch_strings(command, args, env)
    if not strings:
        return None
    return max(strings, key=lambda item: item.size)


def total_launch_bytes(
    command: Optional[Sequence[str]] = None,
    args: Optional[Sequence[str]] = None,
    env: Optional[Mapping[str, Any]] = None,
) -> int:
    """Sum of all ``execve`` strings, the quantity ``ARG_MAX`` bounds."""
    return sum(item.size for item in launch_strings(command, args, env))


def _describe(strings: Iterable[LaunchString], limit: int = 5) -> str:
    ranked = sorted(strings, key=lambda item: item.size, reverse=True)[:limit]
    return ", ".join(f"{item.label}={item.size} bytes" for item in ranked)


def _budget_clause(size: int, budget: int, kind: str) -> str:
    """Describe a size that is at or above ``budget``.

    The guard refuses at ``>=``, so a payload that lands exactly on the
    budget must not claim it exceeds that budget by 0 bytes.
    """
    overage = size - budget
    clause = f"reaches or exceeds the {budget} byte {kind} budget"
    if overage:
        clause += f" by {overage} bytes"
    return clause


def check_launch_payload(
    command: Optional[Sequence[str]] = None,
    args: Optional[Sequence[str]] = None,
    env: Optional[Mapping[str, Any]] = None,
    *,
    what: str = "agent container",
) -> List[LaunchString]:
    """Refuse a launch the kernel would reject, naming what is too big.

    Args:
        command: ``argv[0..]`` override, if the launch sets one.
        args: Arguments appended after the image entrypoint.
        env: Environment the launch declares.
        what: Human label for the thing being started, used in the message.

    Returns:
        The measured strings, so a caller can log them.

    Raises:
        LaunchPayloadTooLargeError: When one string is at or above
            :data:`MAX_LAUNCH_STRING_BYTES`, or the total is at or above
            :data:`MAX_LAUNCH_TOTAL_BYTES`. The message names the offending
            item and its size, because the whole point of the guard is that
            ``argument list too long`` names none of the three.
    """
    strings = launch_strings(command, args, env)
    if not strings:
        return strings

    biggest = max(strings, key=lambda item: item.size)
    if biggest.size >= MAX_LAUNCH_STRING_BYTES:
        raise LaunchPayloadTooLargeError(
            f"Cannot start {what}: launch payload exceeds the execve string "
            f"limit. {biggest.label} is {biggest.size} bytes, which "
            f"{_budget_clause(biggest.size, MAX_LAUNCH_STRING_BYTES, 'per-string launch')} "
            f"(Linux caps one execve string at MAX_ARG_STRLEN, "
            f"{MAX_ARG_STRLEN} bytes). Largest strings: {_describe(strings)}."
        )

    total = sum(item.size for item in strings)
    if total >= MAX_LAUNCH_TOTAL_BYTES:
        raise LaunchPayloadTooLargeError(
            f"Cannot start {what}: launch payload exceeds the total argument "
            f"budget. The launch would pass {total} bytes of "
            f"arguments and environment, which "
            f"{_budget_clause(total, MAX_LAUNCH_TOTAL_BYTES, 'total')} "
            f"(ARG_MAX bounds argv plus environment together). Largest "
            f"strings: {_describe(strings)}."
        )
    return strings
