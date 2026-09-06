"""Operator-owned, versioned execution environments and capability preflight."""

import hashlib
import json
import shlex
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from preloop.config import settings


class DependencyService(BaseModel):
    """An isolated disposable dependency without host mounts or privileges."""

    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[a-z][a-z0-9-]{0,40}$")
    image: str = Field(pattern=r"^[^\s]+@sha256:[a-f0-9]{64}$")
    port: int = Field(ge=1, le=65535)
    command: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)


class EnvironmentProfile(BaseModel):
    """Trusted registry entry; issue payloads can only select its identifier."""

    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    image: str = Field(pattern=r"^[^\s]+@sha256:[a-f0-9]{64}$")
    harness: Literal["codex", "opencode"]
    protocol_version: Literal[1] = 1
    setup_commands: list[str] = Field(default_factory=list)
    setup_timeout_seconds: int = Field(default=600, ge=1, le=3600)
    services: list[DependencyService] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    test_commands: dict[str, list[str]] = Field(default_factory=dict)
    artifact_paths: list[str] = Field(default_factory=list)
    lockfiles: list[str] = Field(default_factory=list)
    cache_paths: list[str] = Field(default_factory=list)
    required_executables: list[str] = Field(
        default_factory=lambda: ["python", "python3", "git", "bash"]
    )

    @field_validator("artifact_paths", "lockfiles", "cache_paths")
    @classmethod
    def relative_paths(cls, paths: list[str]) -> list[str]:
        """Profiles cannot place caches or artifacts outside the checkout."""
        if any(Path(path).is_absolute() or ".." in Path(path).parts for path in paths):
            raise ValueError("environment paths must be relative to checkout")
        return paths

    @field_validator("services")
    @classmethod
    def unique_services(cls, value: list[DependencyService]) -> list[DependencyService]:
        """Prevent ambiguous DNS and localhost port collisions."""
        if len({s.name for s in value}) != len(value) or len(
            {s.port for s in value}
        ) != len(value):
            raise ValueError("duplicate environment service name or port")
        return value

    @property
    def digest(self) -> str:
        """Cache identity changes with every environment dependency/configuration."""
        return hashlib.sha256(
            json.dumps(self.model_dump(), sort_keys=True).encode()
        ).hexdigest()


def resolve_profile(
    agent_config: dict[str, Any], *, agent_type: str, runner: str
) -> EnvironmentProfile | None:
    """Select only registry profiles and reject unsupported runtimes pre-agent."""
    name = agent_config.get("environment_profile")
    if name is None:
        return None
    if not isinstance(name, str) or not settings.flow_environment_profiles_file:
        raise ValueError("environment_profile_not_approved")
    registry = json.loads(Path(settings.flow_environment_profiles_file).read_text())
    if name not in registry:
        raise ValueError("environment_profile_not_approved")
    profile = EnvironmentProfile.model_validate(registry[name])
    if profile.harness != agent_type:
        raise ValueError("environment_harness_mismatch")
    if runner == "private":
        # Existing private image entrypoints do not implement the hosted script
        # contract. Never accept a profile then silently ignore its setup.
        raise ValueError("environment_protocol_unsupported_private_runner")
    return profile


def profile_setup_shell(
    profile: EnvironmentProfile, *, kubernetes: bool, working_dir: str
) -> str:
    """Check services and timebox setup independently, without logging env values."""
    targets = [
        ("127.0.0.1" if kubernetes else service.name, service.port)
        for service in profile.services
    ]
    readiness = f"""import socket,time
end=time.monotonic()+{profile.setup_timeout_seconds}
for host,port in {targets!r}:
 while True:
  try:
   socket.create_connection((host,port),timeout=1).close(); break
  except OSError:
   if time.monotonic()>=end: raise SystemExit("environment_service_unhealthy")
   time.sleep(0.2)
"""
    # A single deadline covers readiness plus install. The subprocess timeout
    # terminates its process group so background setup services cannot leak.
    commands = "\n".join(profile.setup_commands)
    wrapper = f"""import hashlib,json,os,pathlib,shutil,signal,subprocess,sys,time
root=pathlib.Path({working_dir!r})
missing=[name for name in {profile.required_executables!r} if shutil.which(name) is None]
if missing: print('PRELOOP_SETUP_FAILED missing_executables:'+','.join(missing)); sys.exit(78)
profile_path=pathlib.Path('/opt/preloop-environment.json')
try:
 protocol=json.loads(profile_path.read_text())
 if protocol.get('version') != 1 or protocol.get('harness') != {profile.harness!r}: raise ValueError()
except (OSError,ValueError): print('PRELOOP_SETUP_FAILED environment_protocol_unsupported'); sys.exit(78)
identity=hashlib.sha256({profile.digest!r}.encode())
for name in {profile.lockfiles!r}:
 path=root/name
 identity.update(name.encode()+b'\\0'+(path.read_bytes() if path.is_file() else b'missing'))
key=identity.hexdigest()
cache=root/'.preloop-setup-cache.json'
cache_paths={profile.cache_paths!r}
cached=False
try: cached=bool(cache_paths) and json.loads(cache.read_text()).get('key')==key and all((root/name).exists() for name in cache_paths)
except (OSError,ValueError): pass
script={("python3 -c " + shlex.quote(readiness))!r}
if not cached: script+='\\n'+{commands!r}
with open('/workspace/evidence/setup.log','wb') as log:
 p=subprocess.Popen(['bash','-e','-c',script],cwd=str(root),stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
 try: code=p.wait(timeout={profile.setup_timeout_seconds})
 except subprocess.TimeoutExpired:
  os.killpg(p.pid,signal.SIGKILL); p.wait(); print('PRELOOP_SETUP_FAILED setup_timeout'); sys.exit(124)
 if code: print('PRELOOP_SETUP_FAILED setup_failed'); sys.exit(code)
cache.write_text(json.dumps({{'key':key}}))
print('PRELOOP_ENVIRONMENT cache_hit' if cached else 'PRELOOP_ENVIRONMENT setup_complete')
print('PRELOOP_ENVIRONMENT ready {profile.digest}')
"""
    return "mkdir -p /workspace/evidence && python3 -c " + shlex.quote(wrapper)


def profile_env(profile: EnvironmentProfile, *, kubernetes: bool) -> dict[str, str]:
    """Resolve service host placeholders from trusted profile configuration."""
    env = dict(profile.env)
    for key, value in env.items():
        for service in profile.services:
            value = value.replace(
                "${service." + service.name + "}",
                "127.0.0.1" if kubernetes else service.name,
            )
        env[key] = value
    env["PRELOOP_DISABLE_TELEMETRY"] = "true"
    env["PRELOOP_ENVIRONMENT_DIGEST"] = profile.digest
    return env


def profile_readiness(
    agent_config: dict[str, Any],
    *,
    agent_type: str,
    runner: str,
    required_command_ids: list[str],
    required_commands: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Resolve an auditable capability contract without starting an agent."""
    try:
        profile = resolve_profile(agent_config, agent_type=agent_type, runner=runner)
    except (ValueError, OSError) as exc:
        return {"ready": False, "blockers": [str(exc)], "command_ids": []}
    if profile is None:
        return {
            "ready": False,
            "blockers": ["environment_profile_required"],
            "command_ids": [],
        }
    required = set(required_command_ids) | set(required_commands or {})
    missing = sorted(required - profile.test_commands.keys())
    mismatched = sorted(
        key
        for key, command in (required_commands or {}).items()
        if key in profile.test_commands
        and command != "\n".join(profile.test_commands[key])
    )
    blockers = ["environment_command_missing:" + key for key in missing]
    blockers.extend("environment_command_mismatch:" + key for key in mismatched)
    return {
        "ready": not blockers,
        "blockers": blockers,
        "command_ids": sorted(profile.test_commands),
        "profile_digest": profile.digest,
        "protocol_version": profile.protocol_version,
    }


def verification_profile_readiness(
    agent_config: dict[str, Any],
    git_clone_config: dict[str, Any],
    *,
    agent_type: str,
    runner: str,
    required_command_ids: list[str],
) -> dict[str, Any]:
    """Check readiness against the configured verifier, never its log output.

    Before an implementation diff exists, every potentially selected check must
    be supported. Issue acceptance command IDs must also exist in that policy.
    This describes capabilities; only the isolated publisher can attest results.
    """
    from preloop.services.verification import (
        configured_verification_commands,
        resolve_verification_policy,
    )

    try:
        policy = resolve_verification_policy(git_clone_config)
        if policy.mode != "gate" or policy.profile is None:
            return {"ready": False, "blockers": ["verification_gate_required"]}
        configured = configured_verification_commands(policy.profile)
    except (ValueError, TypeError):
        return {"ready": False, "blockers": ["verification_policy_invalid"]}
    if not configured:
        return {"ready": False, "blockers": ["verification_profile_empty"]}
    commands = {command.id: command.command for command in configured}
    readiness = profile_readiness(
        agent_config,
        agent_type=agent_type,
        runner=runner,
        required_command_ids=required_command_ids,
        required_commands=commands,
    )
    readiness["blockers"].extend(
        "verification_command_missing:" + key
        for key in sorted(set(required_command_ids) - commands.keys())
    )
    readiness["ready"] = not readiness["blockers"]
    return readiness
