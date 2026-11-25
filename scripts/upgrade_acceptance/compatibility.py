"""Exercise real CLI binaries against the running local server, without host state."""

from __future__ import annotations

import json
import re
import signal
import subprocess
import sys
import tempfile
import time
import traceback
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from fixture import MODEL_KEY, PASSWORD, identifier
from mock_provider import MARKER

if TYPE_CHECKING:
    import httpx

BASE_URL = "http://mock:9001"


def unchanged_object(before: httpx.Response, after: httpx.Response) -> bool:
    """Require successful object reads before claiming a refused write was safe."""
    return (
        before.status_code == 200
        and after.status_code == 200
        and before.json() == after.json()
    )


def host_profile(home: Path, env: dict[str, str], token: str) -> None:
    """Install a deterministic test harness that calls the real gateway.

    The runner, dispatch, model credential resolution and result persistence are
    real. This small process replaces the vendor CLI and remote model, making
    the acceptance independent of subscriptions and vendor tool installation.
    """
    (home / "workspace").mkdir()
    executable = home / "cursor-agent"
    executable.write_text("""#!/usr/local/bin/python
import json, os, urllib.request
request = urllib.request.Request("http://mock:9001/openai/v1/chat/completions",
    data=json.dumps({"model":"openai/synthetic-model","messages":[{"role":"user","content":"Synthetic flow"}],"stream":False}).encode(),
    headers={"Authorization":"Bearer " + os.environ["PRELOOP_ACCEPTANCE_KEY"],"Content-Type":"application/json"})
with urllib.request.urlopen(request, timeout=30) as response:
    body = json.load(response)
content = body["choices"][0]["message"]["content"]
assert content == "synthetic-upgrade-completion"
print(json.dumps({"type":"system","subtype":"init","session_id":"synthetic-acceptance"}), flush=True)
print(json.dumps({"type":"result","subtype":"success","result":content,"is_error":False}), flush=True)
""")
    executable.chmod(0o700)
    path = home / "host-profiles.json"
    path.write_text(
        json.dumps(
            {
                "profiles": [
                    {
                        "name": "acceptance",
                        "executable": str(executable),
                        "argv": [
                            "--print",
                            "--output-format",
                            "stream-json",
                            "--mode=ask",
                        ],
                        "workspace_root": str(home / "workspace"),
                        "timeout_seconds": 60,
                        "model_map": {"synthetic-model": "synthetic-model"},
                        "pass_env": [
                            "PRELOOP_ACCEPTANCE_KEY",
                            "PRELOOP_DISABLE_TELEMETRY",
                        ],
                    }
                ]
            }
        )
    )
    env["PRELOOP_RUNNER_HOST_PROFILES"] = str(path)
    env["PRELOOP_ACCEPTANCE_KEY"] = token


def exercise_flow(client: httpx.Client, runner_id: str, name: str) -> dict[str, Any]:
    """Trigger and read a real execution through API, orchestrator and CLI runner."""
    created = client.post(
        "/api/v1/flows",
        json={
            "name": "Synthetic acceptance execution",
            "prompt_template": "Return the acceptance marker.",
            "agent_type": "cursor",
            "agent_config": {
                "host_exec_profile": "acceptance",
                "cursor_model": "synthetic-model",
                "runner_id": runner_id,
            },
            "runner_pool": name,
            "is_enabled": True,
        },
    )
    result: dict[str, Any] = {
        "create_status": created.status_code,
        "content_verified": False,
    }
    if created.status_code not in (200, 201):
        return result
    started = client.post(f"/api/v1/flows/{created.json()['id']}/trigger", json={})
    result["trigger_status"] = started.status_code
    if started.status_code not in (200, 201, 202):
        return result
    execution_id = started.json().get("execution_id") or started.json().get("id")
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/flows/executions/{execution_id}")
        if response.status_code == 200:
            execution = response.json()
            result["execution_status"] = execution.get("status")
            result["waiting_for_owner"] = (
                execution.get("error_message") == "Waiting for runner owner"
            )
            if execution.get("status") in {
                "SUCCEEDED",
                "FAILED",
                "CANCELLED",
                "STOPPED",
            }:
                from evidence import scrub

                diagnostic = scrub(
                    execution.get("error_message") or "",
                    [
                        client.headers["Authorization"].removeprefix("Bearer "),
                        MODEL_KEY,
                        PASSWORD,
                    ],
                )
                result["diagnostic"] = re.sub(
                    r"[A-Za-z0-9_-]{20,}", "[redacted]", diagnostic
                )[:500]
                logs = client.get(f"/api/v1/flows/executions/{execution_id}/logs")
                result["content_verified"] = execution["status"] == "SUCCEEDED" and (
                    MARKER in json.dumps(execution) or MARKER in logs.text
                )
                return result
        time.sleep(2)
    return result


def provision(name: str) -> str:
    """Create a separate synthetic tenant so matrix writes cannot change the corpus."""
    from preloop.api.auth.jwt import get_password_hash
    from preloop.models import crud, models
    from preloop.models.crud.base import CRUDBase
    from preloop.models.db.session import get_db_session

    with contextmanager(get_db_session)() as db:
        account = crud.crud_account.create(
            db,
            obj_in={
                "id": identifier(name + "/account"),
                "organization_name": "Example compatibility",
                "is_active": True,
            },
        )
        user = crud.crud_user.create(
            db,
            obj_in={
                "id": identifier(name + "/user"),
                "account_id": account.id,
                "username": name,
                "email": name + "@example.com",
                "full_name": "Jane Doe",
                "hashed_password": get_password_hash(PASSWORD),
                "is_active": True,
                "email_verified": True,
                "user_source": "local",
            },
        )
        role = crud.crud_role.get_by_name(db, name="owner")
        crud.crud_user_role.create(db, obj_in={"user_id": user.id, "role_id": role.id})
        crud.crud_account.update(
            db, db_obj=account, obj_in={"primary_user_id": user.id}
        )
        _, token = crud.crud_api_key.issue(
            db,
            obj_in={
                "account_id": account.id,
                "user_id": user.id,
                "name": "Synthetic matrix key",
                "is_active": True,
                "scopes": [],
            },
        )
        crud.crud_ai_model.create_with_account(
            db,
            account_id=account.id,
            obj_in={
                "name": "Synthetic matrix model",
                "provider_name": "openai",
                "model_identifier": "synthetic-model",
                "api_endpoint": "http://mock:9000/v1",
                "api_key": MODEL_KEY,
                "is_default": True,
                "meta_data": {
                    "gateway": {
                        "enabled": True,
                        "model_alias": "openai/synthetic-model",
                        "provider_adapter": "preloop",
                    }
                },
            },
        )
        crud.crud_ai_model.create_with_account(
            db,
            account_id=account.id,
            obj_in={
                "id": identifier(name + "/routing-model"),
                "name": "Synthetic routing model",
                "provider_name": "openrouter",
                "model_identifier": "synthetic-model",
                "api_endpoint": "http://mock:9000/v1",
                "api_key": MODEL_KEY,
                "is_default": False,
                "meta_data": {"gateway": {"enabled": False}},
            },
        )
        CRUDBase(models.Flow).create(
            db,
            obj_in={
                "id": identifier(name + "/guard-flow"),
                "account_id": account.id,
                "name": "Synthetic compatibility guard",
                "prompt_template": "Synthetic",
                "agent_type": "codex",
                "agent_config": {},
                "is_enabled": False,
                "git_clone_config": {"enabled": False, "repositories": []},
            },
        )
        return token


def exercise(
    binary: str, phase: str, cli: str, result: dict[str, Any]
) -> dict[str, Any]:
    """Verify login, discovered content, enrollment, policy defaults and runner sessions."""
    import httpx

    name = f"matrix-{phase}-{cli}"
    token = provision(name)
    with tempfile.TemporaryDirectory(prefix=name) as directory:
        home = Path(directory)
        config = home / ".config/opencode/config.json"
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({"model": "openai/synthetic-model"}))
        env = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": directory,
            "XDG_CONFIG_HOME": str(home / ".config"),
            "PRELOOP_DISABLE_TELEMETRY": "true",
            "PRELOOP_URL": BASE_URL,
            "PRELOOP_RUNNER_DISABLE_SLEEP_DETECTION": "true",
        }
        host_profile(home, env, token)

        def run(operation: str, args: list[str]) -> subprocess.CompletedProcess[str]:
            completed = subprocess.run(
                [binary, *args],
                env=env,
                cwd=home,
                capture_output=True,
                text=True,
                input="",
                timeout=120,
                check=False,
            )
            result["operations"][operation] = {"exit_code": completed.returncode}
            return completed

        with httpx.Client(
            base_url=BASE_URL,
            headers={"Authorization": f"Bearer {token}"},
            timeout=60,
            trust_env=False,
        ) as client:
            version = client.get("/api/v1/version")
            result["advertised_version"] = (
                version.json()
                if version.status_code == 200
                else {"status": version.status_code}
            )
            login = run("login", ["login", "--token", token, "--no-runner"])
            saved = home / ".preloop/config.yaml"
            result["operations"]["login"]["content_verified"] = (
                login.returncode == 0
                and saved.exists()
                and run("auth_status", ["auth", "status"]).returncode == 0
            )
            discovery = run(
                "discover", ["agents", "discover", "--json", "--no-onboard-prompt"]
            )
            result["operations"]["discover"]["content_verified"] = (
                discovery.returncode == 0 and "OpenCode" in discovery.stdout
            )
            onboard = run(
                "onboard",
                [
                    "agents",
                    "onboard",
                    "opencode",
                    "--yes",
                    "--model",
                    "openai/synthetic-model",
                    "--skip-live-validate",
                ],
            )
            managed = json.loads(config.read_text())
            enrolled = client.get("/api/v1/agents")
            result["operations"]["onboard"].update(
                content_verified=onboard.returncode == 0
                and managed.get("model") == "preloop/openai/synthetic-model"
                and enrolled.status_code == 200
                and "opencode" in enrolled.text.lower(),
                agent_api_status=enrolled.status_code,
            )
            # Read the installed agent's gateway token, never expose it in evidence.
            options = managed.get("provider", {}).get("preloop", {}).get("options", {})
            gateway_token = options.get("apiKey", token)
            response = client.post(
                "/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {gateway_token}"},
                json={
                    "model": "openai/synthetic-model",
                    "messages": [{"role": "user", "content": "Synthetic acceptance"}],
                    "stream": False,
                },
            )
            body = response.json()
            result["operations"]["gateway"] = {
                "status": response.status_code,
                "content_verified": response.status_code == 200
                and body.get("choices", [{}])[0].get("message", {}).get("content")
                == MARKER
                and body.get("usage", {}).get("total_tokens") == 10,
            }
            policy = home / "policy.yaml"
            policy.write_text(
                'version: "1.0"\nmetadata:\n  name: Synthetic compatibility\ndefaults:\n  unknown_tools: allow\ntools:\n  - name: get_issue\n    source: builtin\n    enabled: false\n'
            )
            applied = run("policy_apply", ["policy", "apply", str(policy)])
            if applied.returncode:
                from evidence import scrub

                result["operations"]["policy_apply"]["diagnostic"] = scrub(
                    applied.stderr, [token, MODEL_KEY]
                )[:2000]
            exported = client.get("/api/v1/policies/export")
            document = (
                yaml.safe_load(exported.text) if exported.status_code == 200 else {}
            )
            result["operations"]["policy_apply"]["content_verified"] = (
                applied.returncode == 0
                and document.get("defaults", {}).get("unknown_tools") == "allow"
                and any(
                    tool.get("name") == "get_issue" and tool.get("enabled") is False
                    for tool in document.get("tools", [])
                )
            )
            strict_policy = home / "strict-policy.yaml"
            strict_policy.write_text(
                policy.read_text().replace(
                    "unknown_tools: allow", "unknown_tools: deny"
                )
            )
            strict = run(
                "restrictive_defaults", ["policy", "apply", str(strict_policy)]
            )
            result["operations"]["restrictive_defaults"]["explicit_unsupported"] = (
                "not yet supported" in strict.stderr
                or "not yet supported" in strict.stdout
            )
            strict_export = client.get("/api/v1/policies/export")
            strict_document = (
                yaml.safe_load(strict_export.text)
                if strict_export.status_code == 200
                else {}
            )
            result["operations"]["restrictive_defaults"]["content_verified"] = (
                strict.returncode == (1 if phase == "baseline" else 0)
                and strict_document.get("defaults", {}).get("unknown_tools")
                == ("allow" if phase == "baseline" else "deny")
                and (
                    phase != "baseline"
                    or result["operations"]["restrictive_defaults"][
                        "explicit_unsupported"
                    ]
                )
            )
            report = run(
                "discovery_report",
                ["agents", "discover", "--report", "--no-onboard-prompt"],
            )
            result["operations"]["discovery_report"].update(
                unavailable="503" in report.stderr
                or "unavailable" in report.stderr.lower(),
                explicit_unsaved="not saved" in report.stderr.lower(),
            )
            candidates = client.get("/api/v1/agents/discovery-candidates")
            result["operations"]["discovery_report"]["content_verified"] = (
                report.returncode == 0
                and candidates.status_code == 200
                and any(
                    row.get("agent_kind") == "opencode"
                    for row in candidates.json().get("items", [])
                )
            )
            if phase == "baseline" and cli == "candidate":
                for operation, path, args in (
                    (
                        "review_posting_guard",
                        f"/api/v1/flows/{identifier(name + '/guard-flow')}",
                        [
                            "flow",
                            "review-posting",
                            "enable",
                            str(identifier(name + "/guard-flow")),
                        ],
                    ),
                    (
                        "routing_guard",
                        f"/api/v1/ai-models/{identifier(name + '/routing-model')}",
                        [
                            "models",
                            "routing",
                            str(identifier(name + "/routing-model")),
                            "--only",
                            "example-provider",
                        ],
                    ),
                ):
                    before = client.get(path)
                    refused = run(operation, args)
                    after = client.get(path)
                    result["operations"][operation].update(
                        content_verified=refused.returncode == 1
                        and "upgrade the server" in refused.stderr.lower()
                        and unchanged_object(before, after),
                    )
                for operation, args in (
                    (
                        "extension_policy_unsupported",
                        ["agents", "extensions", "check", "--online"],
                    ),
                    (
                        "account_health_unsupported",
                        ["accounts", "health", str(identifier(name + "/account"))],
                    ),
                    (
                        "shared_flow_unsupported",
                        [
                            "--url",
                            "http://127.0.0.1:8000",
                            "ci",
                            "shared-flow",
                            str(identifier(name + "/guard-flow")),
                        ],
                    ),
                    (
                        "webhook_rotation_unsupported",
                        [
                            "flow",
                            "webhook-secret",
                            "rotate",
                            str(identifier(name + "/guard-flow")),
                        ],
                    ),
                    (
                        "managed_bundle_unsupported",
                        [
                            "agents",
                            "managed-config",
                            "claude-code",
                            "--output",
                            str(home / "bundle"),
                            "--cli-path",
                            binary,
                            "--register",
                        ],
                    ),
                ):
                    unsupported = run(operation, args)
                    result["operations"][operation]["content_verified"] = (
                        unsupported.returncode != 0
                        and "upgrade the server" in unsupported.stderr.lower()
                    )
                    if not result["operations"][operation]["content_verified"]:
                        from evidence import scrub

                        result["operations"][operation]["diagnostic"] = scrub(
                            unsupported.stderr, [token, MODEL_KEY, PASSWORD]
                        )[:500]
            identities = []
            for attempt in (1, 2):
                runner_log = home / f"runner-{attempt}.log"
                log_handle = runner_log.open("w")
                runner = subprocess.Popen(
                    [
                        binary,
                        "runner",
                        "fg",
                        "--name",
                        name,
                        "--labels",
                        name,
                        "--flow-execution-consent",
                        "always",
                    ],
                    env={**env, "PRELOOP_URL": "http://127.0.0.1:8000"},
                    cwd=home,
                    stdout=log_handle,
                    stderr=log_handle,
                )
                try:
                    deadline = time.monotonic() + 45
                    found = None
                    while time.monotonic() < deadline and runner.poll() is None:
                        listing = client.get("/api/v1/runners")
                        if listing.status_code == 200:
                            data = listing.json()
                            rows = (
                                data
                                if isinstance(data, list)
                                else data.get("runners", [])
                            )
                            found = next(
                                (
                                    row
                                    for row in rows
                                    if row.get("name") == name
                                    and row.get("status") == "online"
                                ),
                                None,
                            )
                            if (
                                found
                                and "Connected. Waiting for jobs."
                                in runner_log.read_text(errors="replace")
                            ):
                                break
                            found = None
                        time.sleep(1)
                    identities.append(found["id"] if found else None)
                    if (
                        found
                        and attempt == 1
                        and phase == "candidate"
                        and cli == "candidate"
                    ):
                        result["operations"]["flow"] = exercise_flow(
                            client, found["id"], name
                        )
                finally:
                    runner.send_signal(signal.SIGINT)
                    try:
                        runner.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        runner.kill()
                        runner.wait(timeout=5)
                    log_handle.close()
                result["operations"][f"runner_connect_{attempt}"] = {
                    "content_verified": bool(found),
                    "exit_code": runner.returncode,
                }
            result["operations"]["runner_reconnect"] = {
                "content_verified": identities[0] is not None
                and identities[0] == identities[1]
            }
    return result


def main() -> None:
    """Emit a credential-free result even when a particular cell fails."""
    results = []
    for cli in ("baseline", "candidate"):
        result: dict[str, Any] = {"cli": cli, "server": sys.argv[1], "operations": {}}
        try:
            exercise(f"/tools/{cli}/preloop", sys.argv[1], cli, result)
        except Exception as exc:
            result["error"] = type(exc).__name__
            result["location"] = [
                f"{frame.name}:{frame.lineno}"
                for frame in traceback.extract_tb(exc.__traceback__)[-6:]
            ]
        results.append(result)
    print("MATRIX_RESULT=" + json.dumps(results), flush=True)


if __name__ == "__main__":
    main()
