"""Bounded remote installation using the same CLI path as manual onboarding.

SSH secrets are used for this request only. Installation output is deliberately
not returned: upstream installers and runtime diagnostics can contain tokens.
"""

import asyncio
import hashlib
import ipaddress
import json
import os
import re
import shlex
import socket
from dataclasses import dataclass
from uuid import UUID

import asyncssh

from preloop.schemas.agent_deployment import AgentDeploymentSSH


class DeploymentError(Exception):
    """A safe, operator-facing failure without upstream output or secrets."""


@dataclass
class DeploymentResult:
    """Evidence returned by the remote CLI, checked against local CRUD next."""

    agent_id: str
    runtime_version: str
    model_alias: str


def deployment_capabilities() -> dict[str, bool]:
    """Expose only deployment methods which the operator configured."""
    enabled = os.getenv("PRELOOP_AGENT_DEPLOYMENT_ENABLED", "").lower() == "true"
    return {
        "ssh": enabled,
        "gcp": enabled
        and bool(os.getenv("PRELOOP_DEPLOY_GCP_PROJECT"))
        and bool(os.getenv("PRELOOP_DEPLOY_GCP_ZONE")),
    }


async def resolve_ssh_address(host: str, port: int) -> str:
    """Pin a public address, or an operator-allowlisted private network.

    Resolve once and connect to that address, preventing a second DNS lookup
    from turning a validated public name into a metadata or loopback address.
    """
    networks = [
        ipaddress.ip_network(item.strip())
        for item in os.getenv("PRELOOP_DEPLOY_SSH_ALLOWED_CIDRS", "").split(",")
        if item.strip()
    ]
    try:
        answers = await asyncio.get_running_loop().getaddrinfo(
            host, port, type=socket.SOCK_STREAM
        )
    except OSError as exc:
        raise DeploymentError("The SSH host could not be resolved") from exc
    addresses = sorted({answer[4][0] for answer in answers})
    for value in addresses:
        address = ipaddress.ip_address(value)
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        if (
            address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_unspecified
        ):
            raise DeploymentError(
                "Loopback, link-local and metadata SSH targets are forbidden"
            )
        if not address.is_global and not any(
            address in network for network in networks
        ):
            raise DeploymentError(
                "The SSH address is outside the operator's allowed networks"
            )
    if not addresses:
        raise DeploymentError("The SSH host has no usable address")
    return addresses[0]


def parse_host_key(value: str) -> asyncssh.SSHKey:
    """Accept a public key, optionally prefixed by its known_hosts hostname."""
    fields = value.strip().split()
    if fields and not fields[0].startswith(("ssh-", "ecdsa-")):
        fields = fields[1:]
    try:
        return asyncssh.import_public_key(" ".join(fields))
    except (ValueError, asyncssh.KeyImportError) as exc:
        raise DeploymentError(
            "Provide a valid independently verified SSH host public key"
        ) from exc


def installation_script(
    runtime: str, alias: str, url: str, token: str, request_id: UUID
) -> str:
    """Build a fixed script; every variable is shell quoted and secrets use stdin."""
    cli_url = os.getenv("PRELOOP_DEPLOY_CLI_URL", "")
    checksum = os.getenv("PRELOOP_DEPLOY_CLI_SHA256", "")
    if cli_url and (
        not cli_url.startswith("https://")
        or len(checksum) != 64
        or any(c not in "0123456789abcdef" for c in checksum.lower())
    ):
        raise DeploymentError("Operator CLI URL requires HTTPS and a SHA256 checksum")
    values = {
        "runtime": runtime,
        "alias": alias,
        "origin": url,
        "token": token,
        "request_id": str(request_id),
        "cli_url": cli_url,
        "checksum": checksum,
    }
    assignments = "\n".join(
        f"deploy_{key}={shlex.quote(value)}" for key, value in values.items()
    )
    return (
        assignments
        + r"""
set -euo pipefail
deploy_stage=PRELOOP_DEPLOY_PREREQUISITES_FAILED
trap 'printf "%s\n" "$deploy_stage"' ERR
umask 077
export PATH="$HOME/.local/bin:$HOME/.npm-global/bin:$HOME/.hermes/hermes-agent/venv/bin:$PATH"
export PRELOOP_URL="$deploy_origin"
unset PRELOOP_TOKEN
export PRELOOP_DISABLE_TELEMETRY=true
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT HUP INT TERM
# The lock is on the host, so retries across API workers cannot install twice
# concurrently. A disconnected SSH session cannot leave a permanent lock.
mkdir -p "$HOME/.local/state/preloop"
exec 9>"$HOME/.local/state/preloop/deployment.lock"
flock -n 9 || { echo PRELOOP_DEPLOY_BUSY; exit 75; }
if [ -n "$deploy_cli_url" ]; then
  deploy_stage=PRELOOP_DEPLOY_DOWNLOAD_FAILED
  curl -fsSL --connect-timeout 20 "$deploy_cli_url" -o "$work/preloop"
  deploy_stage=PRELOOP_DEPLOY_CHECKSUM_FAILED
  printf '%s  %s\n' "$deploy_checksum" "$work/preloop" | sha256sum -c - >/dev/null
  deploy_stage=PRELOOP_DEPLOY_CLI_FAILED
  mkdir -p "$HOME/.local/bin"
  install -m 700 "$work/preloop" "$HOME/.local/bin/preloop"
else
  deploy_stage=PRELOOP_DEPLOY_DOWNLOAD_FAILED
  curl -fsSL --connect-timeout 20 https://preloop.ai/install/cli -o "$work/install"
  deploy_stage=PRELOOP_DEPLOY_CLI_FAILED
  sh "$work/install" </dev/null >"$work/install.log" 2>&1 || { echo PRELOOP_DEPLOY_CLI_FAILED; exit 1; }
fi
deploy_stage=PRELOOP_DEPLOY_RUNTIME_FAILED
preloop agents install-runtime "$deploy_runtime" --install-only -y </dev/null >"$work/runtime.log" 2>&1 || { echo PRELOOP_DEPLOY_RUNTIME_FAILED; exit 1; }
# Third-party runtime installers never receive the bootstrap bearer. Only the
# trusted CLI enrollment phase gets it; the API revokes it when this ends.
export PRELOOP_TOKEN="$deploy_token"
deploy_stage=PRELOOP_DEPLOY_ONBOARDING_INCOMPLETE
preloop agents install-runtime "$deploy_runtime" --skip-install -y --model "$deploy_alias" </dev/null >"$work/onboard.log" 2>&1 || { echo PRELOOP_DEPLOY_ONBOARDING_INCOMPLETE; exit 1; }
# Validate the real runtime and live managed gateway before announcing success.
deploy_stage=PRELOOP_DEPLOY_VALIDATION_FAILED
preloop agents validate "$deploy_runtime" --live </dev/null >"$work/validate.log" 2>&1 || { echo PRELOOP_DEPLOY_VALIDATION_FAILED; exit 1; }
deploy_stage=PRELOOP_DEPLOY_VERSION_UNAVAILABLE
"$deploy_runtime" --version </dev/null >"$work/version" 2>/dev/null
deploy_stage=PRELOOP_DEPLOY_STATUS_FAILED
preloop agents status "$deploy_runtime" --json </dev/null >"$work/status.json" 2>/dev/null
deploy_stage=PRELOOP_DEPLOY_ONBOARDING_INCOMPLETE
python3 - "$work" "$deploy_alias" <<'PRELOOP_EVIDENCE'
import json,pathlib,sys
p=pathlib.Path(sys.argv[1])
s=json.loads((p/'status.json').read_text())
a=(s.get('remote_state') or {}).get('agent') or {}
validations=[e.get('validation_result') or {} for e in (s.get('remote_state') or {}).get('enrollments', [])]
valid=next((v for v in validations if v.get('validation_passed') is True and v.get('live_validation_status')=='passed' and v.get('live_validation_model_alias')==sys.argv[2]), None)
if not a.get('id') or not a.get('model_gateway_configured') or valid is None:
    print('PRELOOP_DEPLOY_ONBOARDING_INCOMPLETE')
    raise SystemExit(1)
if valid.get('control_plugin_verified') is not True or valid.get('control_channel_configured') is not True:
    print('PRELOOP_DEPLOY_CONTROL_NOT_READY')
    raise SystemExit(1)
version=(p/'version').read_text().strip()
if not version or len(version)>512:
    print('PRELOOP_DEPLOY_VERSION_UNAVAILABLE')
    raise SystemExit(1)
print(json.dumps({'agent_id':a['id'],'runtime_version':version,'model_alias':valid['live_validation_model_alias']}))
PRELOOP_EVIDENCE
"""
    )


async def install_over_ssh(
    ssh: AgentDeploymentSSH,
    *,
    runtime: str,
    alias: str,
    url: str,
    token: str,
    request_id: UUID,
) -> DeploymentResult:
    """Install and validate without falling back to agent or known_hosts trust."""
    address = await resolve_ssh_address(ssh.host, ssh.port)
    host_key = parse_host_key(ssh.host_key)
    client_keys = []
    if ssh.private_key:
        try:
            client_keys = [
                asyncssh.import_private_key(ssh.private_key.get_secret_value())
            ]
        except (ValueError, asyncssh.KeyImportError) as exc:
            raise DeploymentError(
                "The supplied SSH private key could not be read"
            ) from exc
    script = installation_script(runtime, alias, url, token, request_id)
    try:
        async with asyncssh.connect(
            address,
            port=ssh.port,
            username=ssh.username,
            known_hosts=([host_key], [], []),
            client_keys=client_keys,
            password=ssh.password.get_secret_value() if ssh.password else None,
            agent_path=None,
            config=None,
            connect_timeout=20,
            login_timeout=20,
            keepalive_interval=15,
            keepalive_count_max=3,
        ) as connection:
            async with connection.create_process(
                "bash -s", encoding="utf-8", stderr=asyncssh.DEVNULL
            ) as process:
                process.stdin.write(script)
                process.stdin.write_eof()
                # The fixed script suppresses installer output. Bound remote output
                # anyway: a compromised target must not fill API memory.
                output = await process.stdout.read(65537)
                if len(output) > 65536:
                    process.terminate()
                    raise DeploymentError(
                        "SSH target returned excessive deployment output"
                    )
                await process.wait_closed()
                if process.exit_status != 0:
                    stage = next(
                        (
                            line
                            for line in output.splitlines()
                            if line
                            in {
                                "PRELOOP_DEPLOY_BUSY",
                                "PRELOOP_DEPLOY_PREREQUISITES_FAILED",
                                "PRELOOP_DEPLOY_DOWNLOAD_FAILED",
                                "PRELOOP_DEPLOY_CHECKSUM_FAILED",
                                "PRELOOP_DEPLOY_STATUS_FAILED",
                                "PRELOOP_DEPLOY_CONTROL_NOT_READY",
                                "PRELOOP_DEPLOY_CLI_FAILED",
                                "PRELOOP_DEPLOY_RUNTIME_FAILED",
                                "PRELOOP_DEPLOY_VALIDATION_FAILED",
                                "PRELOOP_DEPLOY_ONBOARDING_INCOMPLETE",
                                "PRELOOP_DEPLOY_VERSION_UNAVAILABLE",
                            }
                        ),
                        "PRELOOP_DEPLOY_FAILED",
                    )
                    raise DeploymentError(
                        f"Remote installation failed ({stage}). Check the host's runtime prerequisites and selected model."
                    )
    except (asyncssh.Error, OSError) as exc:
        raise DeploymentError(
            "SSH authentication, host-key verification, or connection failed"
        ) from exc
    try:
        evidence = json.loads(output.strip().splitlines()[-1])
        agent_id = str(UUID(evidence["agent_id"]))
        version_match = re.search(
            r"\b(v?\d+\.\d+\.\d+)\b",
            str(evidence["runtime_version"]),
        )
        if not version_match:
            raise ValueError("No runtime version in verified evidence")
        return DeploymentResult(
            agent_id,
            f"{runtime} {version_match.group(1)}",
            str(evidence["model_alias"]),
        )
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise DeploymentError(
            "The remote CLI did not return verified onboarding evidence"
        ) from exc


def deployment_vm_name(account_id: str, request_id: UUID) -> str:
    """Stable account-scoped GCE name; a retry never provisions another VM."""
    digest = hashlib.sha256(f"{account_id}:{request_id}".encode()).hexdigest()[:24]
    return f"preloop-agent-{digest}"
