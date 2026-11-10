"""Disposable Docker/Kubernetes environment and HTTP recovery integration.

Requires a migrated disposable DATABASE_URL, a locally built profile image,
pgvector image, and an explicitly selected disposable kubeconfig. Never uses
the operator's current Kubernetes context. No model/provider calls are made.

``--browser`` (Docker only) instead runs the fixture-site browser QA flow
(``browser_fixture_flow.py``) in one hosted executor with the egress proxy and
the fixture site as sidecars, then checks ``result.json`` and the proxy log.
Sidecar images must be pullable by digest, for example from a local registry::

    docker run -d -p 5000:5000 registry:2
    docker build -t localhost:5000/egress-proxy environments/egress-proxy
    docker build -t localhost:5000/fixture-site environments/fixture-site
    docker push localhost:5000/egress-proxy   # repeat for fixture-site
    python scripts/tests/flow_environment_integration.py --browser \\
        --image <preloop environment image> \\
        --proxy-image localhost:5000/egress-proxy@sha256:<digest> \\
        --fixture-image localhost:5000/fixture-site@sha256:<digest>
"""

import argparse
import asyncio
import base64
import io
import json
import os
import tarfile
import tempfile
from datetime import datetime
import threading
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

os.environ["PRELOOP_DISABLE_TELEMETRY"] = "true"

import uvicorn
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from preloop.api.endpoints.flow_artifacts import router
from preloop.config import settings
from preloop.models import models
from preloop.models.crud import crud_flow_execution

# Initialize CRUD before Codex: its runtime resolver imports secret_service,
# which imports the CRUD package and otherwise recurses into ai_model startup.
from preloop.agents.codex import CodexAgent
from preloop.services.checkpoint_runtime import checkpoint_context


from browser_fixture_flow import RESULT_MAX_BYTES, unexpected_denials
from kubernetes_guard import disposable_client

REPO = Path(__file__).resolve().parents[2]


class ProbeAgent(CodexAgent):
    """Exercise production provisioning/script hooks without invoking a model."""

    async def _init_kubernetes_clients(self) -> None:
        """Never fall back to an operator context, including after-import env changes."""
        if self._k8s_initialized:
            return
        from kubernetes_asyncio import client

        self._k8s_api_client = await disposable_client(
            config_file=self.config["test_kubeconfig"],
            context=self.config["test_kube_context"],
            expected_host=self.config["test_expected_api_server"],
        )
        print(
            json.dumps(
                {
                    "kubernetes_preflight": "passed",
                    "context": self.config["test_kube_context"],
                    "actual_api_server": self._k8s_api_client.configuration.host,
                }
            ),
            flush=True,
        )
        self._k8s_batch_api = client.BatchV1Api(self._k8s_api_client)
        self._k8s_core_api = client.CoreV1Api(self._k8s_api_client)
        self._k8s_initialized = True

    async def _prepare_environment(
        self, execution_context: dict[str, Any]
    ) -> dict[str, str]:
        return {}

    def _build_codex_script(self, execution_context: dict[str, Any]) -> str:
        init = self._prepare_init_commands(execution_context)
        probe = execution_context["probe_script"]
        return "set -e\n" + init + "\n" + probe


async def run_probe(context: dict[str, Any], *, kubernetes: bool) -> str:
    """Start and remove one real hosted executor after collecting its outcome."""
    agent = ProbeAgent(
        {
            "environment_profile": "integration",
            **context.get("test_kubernetes_config", {}),
        }
    )
    agent.use_kubernetes = kubernetes
    ref = None
    try:
        ref = await agent.start(context)
        deadline = time.monotonic() + 150
        while time.monotonic() < deadline:
            if context.get("loss_mode") and (
                not kubernetes or context["loss_mode"] == "pod"
            ):
                live_logs = await agent.get_logs(ref)
                text = (
                    "\n".join(live_logs)
                    if isinstance(live_logs, list)
                    else str(live_logs)
                )
                if "PERIODIC_CHECKPOINT_READY" in text:
                    if not kubernetes:
                        await agent._containers[ref].kill(signal="SIGKILL")
                        return text
                    pods = await agent._k8s_core_api.list_namespaced_pod(
                        agent.agent_namespace, label_selector=f"job-name={ref}"
                    )
                    assert pods.items, "agent pod disappeared before loss injection"
                    await agent._k8s_core_api.delete_namespaced_pod(
                        pods.items[0].metadata.name,
                        agent.agent_namespace,
                        grace_period_seconds=0,
                    )
                    return text
            if kubernetes:
                job = await agent._k8s_batch_api.read_namespaced_job(
                    ref, agent.agent_namespace
                )
                if job.status.succeeded or job.status.failed:
                    break
            else:
                state = (await agent._containers[ref].show())["State"]
                if not state["Running"]:
                    break
            await asyncio.sleep(0.5)
        else:
            raise AssertionError("probe execution timeout")
        logs = await agent.get_logs(ref)
        if context.get("collect_service_logs") and not kubernetes:
            # Sidecars are deleted by aclose(); read their output first.
            context["service_logs"] = {}
            for name, container in zip(
                context["collect_service_logs"],
                agent._environment_containers,
                strict=False,
            ):
                lines = await container.log(stdout=True, stderr=True)
                context["service_logs"][name] = "\n".join(
                    line.rstrip("\n") for line in lines
                )
        return "\n".join(logs) if isinstance(logs, list) else str(logs)
    finally:
        if ref:
            await agent.stop(ref)
        await agent.aclose()


BROWSER_PAYLOAD = (
    REPO / "environments" / "preloop" / "browser" / "enable.sh",
    REPO / "environments" / "preloop" / "browser" / "selfcheck.sh",
    REPO / "environments" / "preloop" / "browser" / "playwright-mcp.config.json",
    REPO / "scripts" / "tests" / "browser_fixture_flow.py",
)


def browser_payload_command() -> str:
    """Shell command that unpacks the browser scripts into ``/workspace``.

    The sandbox image does not contain the repository, and git cloning is
    off for this probe, so the scripts travel inline as a base64 tarball.
    """
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for path in BROWSER_PAYLOAD:
            archive.add(path, arcname=str(path.relative_to(REPO)))
    encoded = base64.b64encode(buffer.getvalue()).decode()
    return f"echo {encoded} | base64 -d | tar -xz -C /workspace"


def browser_profile(args: argparse.Namespace) -> dict[str, Any]:
    """The ``preloop-browser`` example profile, with the fixture site wired in."""
    return {
        "image": args.image,
        "harness": "codex",
        "setup_timeout_seconds": 180,
        "services": [
            {
                "name": "egress-proxy",
                "image": args.proxy_image,
                "port": 3128,
                "env": {
                    "EGRESS_ALLOWED_ORIGINS": "http://fixture-site:8080",
                    # Docker picks the execution network from its default
                    # pools; the carve-out covers them. Metadata stays denied.
                    "EGRESS_ALLOW_PRIVATE_CIDRS": "172.16.0.0/12,192.168.0.0/16",
                },
            },
            {"name": "fixture-site", "image": args.fixture_image, "port": 8080},
        ],
        "env": {
            "PRELOOP_BROWSER_PROXY": "http://${service.egress-proxy}:3128",
            "PRELOOP_HARNESS": "codex",
        },
        "setup_commands": [
            browser_payload_command(),
            "bash /workspace/environments/preloop/browser/enable.sh",
        ],
        # result.json is echoed to the log; no artifact server runs here.
    }


BROWSER_PROBE = """python3 - <<'PY'
import os, time, urllib.request
opener = urllib.request.build_opener(
    urllib.request.ProxyHandler({"http": os.environ["PRELOOP_BROWSER_PROXY"]})
)
for _ in range(60):
    try:
        opener.open("http://fixture-site:8080/healthz", timeout=2).read()
        break
    except Exception:
        time.sleep(1)
from datetime import datetime, timezone
# Proxy denials before this instant come from the setup self-check.
print("BROWSER_FLOW_START", datetime.now(timezone.utc).isoformat(), flush=True)
PY
/opt/preloop-tests/bin/python /workspace/scripts/tests/browser_fixture_flow.py \
  --base-url http://fixture-site:8080 --result /workspace/result.json \
  --tool-timeout 5 && echo BROWSER_FIXTURE_EXIT=0 || echo BROWSER_FIXTURE_EXIT=1
"""


def check_browser_run(logs: str, proxy_log: str) -> dict[str, Any]:
    """Assert the fixture flow passed and the proxy denied exactly what it should.

    Args:
        logs: Sandbox output of the browser probe.
        proxy_log: Output of the ``egress-proxy`` sidecar.

    Returns:
        The parsed ``result.json`` document.
    """
    assert "BROWSER_FIXTURE_EXIT=0" in logs, logs[-3000:]
    line = next(
        item for item in logs.splitlines() if item.startswith("BROWSER_FIXTURE_RESULT ")
    )
    raw = line.removeprefix("BROWSER_FIXTURE_RESULT ")
    assert len(raw.encode()) + 1 <= RESULT_MAX_BYTES, raw
    assert "fixture-pass" not in raw, "credential leaked into result.json"
    result = json.loads(raw)
    assert result["pass"] is True, result
    assert [note.split(":", 1)[0] for note in result["notes"]] == [
        "login",
        "rejected_origin",
        "redirect_escape",
        "timeout",
        "cleanup",
    ], result
    # selfcheck.sh also sends example.org and a loopback probe through the
    # proxy during setup; count only denials logged after the flow started.
    marker = next(
        item for item in logs.splitlines() if item.startswith("BROWSER_FLOW_START ")
    )
    since = datetime.fromisoformat(marker.split(" ", 1)[1].strip())
    missing, extra = unexpected_denials(
        proxy_log, since=since, allowed_dial_hosts=("fixture-site",)
    )
    assert not missing and not extra, proxy_log[-3000:]
    return result


async def main_browser(args: argparse.Namespace) -> None:
    """Run the fixture browser flow in one real Docker-hosted executor."""
    if args.kubeconfig:
        raise ValueError("--browser runs on Docker only")
    if not args.proxy_image or not args.fixture_image:
        raise ValueError("--browser needs --proxy-image and --fixture-image")
    settings.preloop_url = args.endpoint
    with tempfile.TemporaryDirectory() as directory:
        registry = Path(directory) / "profiles.json"
        registry.write_text(json.dumps({"integration": browser_profile(args)}))
        settings.flow_environment_profiles_file = str(registry)
        engine = create_engine(os.environ["DATABASE_URL"])
        try:
            with Session(engine) as db:
                account = models.Account(organization_name="Disposable browser")
                db.add(account)
                db.flush()
                flow = models.Flow(
                    account_id=account.id,
                    name="Disposable browser probe",
                    prompt_template="probe",
                    agent_type="codex",
                    agent_config={},
                )
                db.add(flow)
                db.flush()
                execution = models.FlowExecution(flow_id=flow.id, status="RUNNING")
                db.add(execution)
                db.commit()
                context: dict[str, Any] = {
                    "account_id": account.id,
                    "flow_id": str(flow.id),
                    "execution_id": str(execution.id),
                    "prompt": "No model invocation",
                    "agent_config": {"environment_profile": "integration"},
                    "git_clone_config": {"enabled": False, "repositories": []},
                    "trigger_event_data": {},
                    "probe_script": BROWSER_PROBE,
                    "collect_service_logs": ["egress-proxy", "fixture-site"],
                }
                logs = await run_probe(context, kubernetes=False)
                result = check_browser_run(
                    logs, context["service_logs"]["egress-proxy"]
                )
                print(json.dumps({"runtime": "docker", "browser_fixture": result}))
                db.delete(account)
                db.commit()
        finally:
            engine.dispose()


async def main(args: argparse.Namespace) -> None:
    """Verify real SQL/browser dependencies and recover after a killed sandbox."""
    if args.browser:
        await main_browser(args)
        return
    if not args.postgres_image:
        raise ValueError("--postgres-image is required without --browser")
    args.loss_mode = args.loss_mode or ("pod" if args.kubeconfig else "process")
    if (args.loss_mode == "pod") != bool(args.kubeconfig):
        raise ValueError("pod loss requires Kubernetes; process loss requires Docker")
    if args.kubeconfig:
        os.environ["KUBECONFIG"] = str(Path(args.kubeconfig).resolve())
        os.environ["AGENT_EXECUTION_NAMESPACE"] = "default"
    settings.flow_artifact_direct_upload = True
    settings.preloop_url = args.endpoint
    settings.flow_checkpoint_interval_seconds = 30
    with tempfile.TemporaryDirectory() as directory:
        registry = Path(directory) / "profiles.json"
        registry.write_text(
            json.dumps(
                {
                    "integration": {
                        "image": args.image,
                        "harness": "codex",
                        "setup_timeout_seconds": 30,
                        "setup_commands": [
                            "mkdir -p /workspace/cache",
                            "echo dependency-setup > /workspace/cache/ready",
                        ],
                        "cache_paths": ["cache"],
                        "services": [
                            {
                                "name": "postgres",
                                "image": args.postgres_image,
                                "port": 5432,
                                "env": {
                                    "POSTGRES_PASSWORD": "disposable",
                                    "POSTGRES_DB": "probe",
                                },
                            }
                        ],
                        "env": {
                            "DATABASE_URL": "postgresql://postgres:disposable@${service.postgres}:5432/probe"
                        },
                    }
                }
            )
        )
        settings.flow_environment_profiles_file = str(registry)
        app = FastAPI()
        app.include_router(router, prefix="/api/v1")
        server = uvicorn.Server(
            uvicorn.Config(app, host="0.0.0.0", port=args.port, log_level="error")
        )
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        while not server.started:
            await asyncio.sleep(0.05)
        engine = create_engine(os.environ["DATABASE_URL"])
        try:
            with Session(engine) as db:
                account = models.Account(organization_name="Disposable integration")
                db.add(account)
                db.flush()
                flow = models.Flow(
                    account_id=account.id,
                    name="Disposable probe",
                    prompt_template="probe",
                    agent_type="codex",
                    agent_config={},
                )
                db.add(flow)
                db.flush()
                thread_id = str(uuid4())
                original = models.FlowExecution(
                    flow_id=flow.id,
                    status="RUNNING",
                    trigger_event_details={"_session_thread_id": thread_id},
                )
                db.add(original)
                db.commit()
                assert crud_flow_execution.get(db, id=original.id) is not None
                scope = {
                    "test_kubernetes_config": {
                        "test_kubeconfig": args.kubeconfig,
                        "test_kube_context": args.kube_context,
                        "test_expected_api_server": args.expected_api_server,
                    },
                    "account_id": account.id,
                    "flow_id": str(flow.id),
                    "prompt": "No model invocation",
                    "agent_config": {"environment_profile": "integration"},
                    "git_clone_config": {"enabled": False, "repositories": []},
                }
                context = {
                    **scope,
                    "execution_id": str(original.id),
                    "trigger_event_data": original.trigger_event_details,
                    "loss_mode": args.loss_mode,
                }
                context["checkpoint_env"] = checkpoint_context(db, context)
                context[
                    "probe_script"
                ] = """python -c "import os,psycopg; c=psycopg.connect(os.environ['DATABASE_URL']); c.execute('CREATE TABLE probe (n int)'); c.execute('INSERT INTO probe VALUES (42)'); assert c.execute('SELECT n FROM probe').fetchone()[0]==42; print('DATABASE_PROBE_OK')"
node -e "const {chromium}=require('playwright'); (async()=>{const browser=await chromium.launch({headless:true,args:['--no-sandbox']}); const page=await browser.newPage(); await page.setContent('<button>Run</button><output>idle</output>'); await page.locator('button').evaluate(el=>el.onclick=()=>document.querySelector('output').textContent='done'); await page.click('button'); if(await page.locator('output').textContent()!=='done')process.exit(1); await browser.close(); console.log('BROWSER_PROBE_OK')})()"
git init /workspace/repo >/dev/null
git -C /workspace/repo config user.name 'Test User'
git -C /workspace/repo config user.email test@example.com
echo committed > /workspace/repo/tracked
git -C /workspace/repo add tracked
git -C /workspace/repo commit -m unpushed >/dev/null
git -C /workspace/repo rev-parse HEAD > /workspace/expected-head
git -C /workspace/repo push /tmp/nonexistent-remote HEAD:main && exit 99
echo staged > /workspace/repo/staged
git -C /workspace/repo add staged
echo uncommitted > /workspace/repo/tracked
echo untracked > /workspace/repo/scratch
python3 -c "from pathlib import Path; import os; Path('/workspace/repo/large-source.bin').write_bytes(os.urandom(3 * 1024 * 1024))"
sha256sum /workspace/repo/large-source.bin | cut -d' ' -f1 > /workspace/expected-digest
# No manual upload: recovery must use the periodic loop started by init.
while [ ! -s /tmp/preloop-checkpoint-reference.json ]; do sleep 1; done
sleep 2
echo after-checkpoint > /workspace/repo/scratch
echo PERIODIC_CHECKPOINT_READY
# The controller kills the Docker container or deletes the Kubernetes pod.
sleep 120
exit 99
"""
                first = await run_probe(context, kubernetes=bool(args.kubeconfig))
                assert "DATABASE_PROBE_OK" in first and "BROWSER_PROBE_OK" in first, (
                    first[-3000:]
                )
                assert "PRELOOP_CHECKPOINT committed" in first, first[-3000:]
                original.status = "FAILED"
                db.commit()
                resumed = models.FlowExecution(
                    flow_id=flow.id,
                    status="RUNNING",
                    trigger_event_details={
                        "_session_thread_id": thread_id,
                        "_resume": {
                            "execution_id": str(original.id),
                            "thread_id": thread_id,
                        },
                    },
                )
                db.add(resumed)
                db.commit()
                followup = {
                    **scope,
                    "execution_id": str(resumed.id),
                    "trigger_event_data": resumed.trigger_event_details,
                }
                followup["checkpoint_resume_authorized"] = True
                followup["thread_id"] = thread_id
                followup["checkpoint_env"] = checkpoint_context(db, followup)
                followup[
                    "probe_script"
                ] = """test "$(git -C /workspace/repo rev-parse HEAD)" = "$(cat /workspace/expected-head)"
test "$(cat /workspace/repo/tracked)" = uncommitted
test "$(cat /workspace/repo/scratch)" = untracked
test "$(git -C /workspace/repo show :staged)" = staged
test "$(sha256sum /workspace/repo/large-source.bin | cut -d' ' -f1)" = "$(cat /workspace/expected-digest)"
echo RECOVERY_PROBE_OK
"""
                second = await run_probe(followup, kubernetes=bool(args.kubeconfig))
                assert "RECOVERY_PROBE_OK" in second, second[-3000:]
                assert "PRELOOP_ENVIRONMENT cache_hit" in second, second[-3000:]
                import re

                age = re.search(
                    r"PRELOOP_CHECKPOINT restored age_seconds=(\d+)", second
                )
                assert age and int(age.group(1)) >= 1, second[-3000:]
                print(
                    json.dumps(
                        {
                            "runtime": "kubernetes" if args.kubeconfig else "docker",
                            "database": "passed",
                            "browser": "passed",
                            "killed_execution_recovery": "passed",
                            "loss_mode": args.loss_mode,
                            "periodic_checkpoint_over_2mib": "passed",
                            "staged_unstaged_untracked": "passed",
                            "post_checkpoint_write_excluded": "passed",
                            "checkpoint_age_seconds": int(age.group(1)),
                            "cached_setup": "passed",
                        }
                    )
                )
                db.delete(account)
                db.commit()
        finally:
            server.should_exit = True
            thread.join(timeout=5)
            engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--postgres-image")
    parser.add_argument(
        "--browser",
        action="store_true",
        help="run the fixture-site browser QA flow instead of the recovery probe",
    )
    parser.add_argument("--proxy-image", help="egress-proxy image by digest")
    parser.add_argument("--fixture-image", help="fixture-site image by digest")
    parser.add_argument("--endpoint", default="http://host.docker.internal:25440")
    parser.add_argument("--port", type=int, default=25440)
    parser.add_argument("--kubeconfig")
    parser.add_argument("--loss-mode", choices=["process", "pod"])
    parser.add_argument("--kube-context", default="kind-preloop-recovery-test")
    parser.add_argument("--expected-api-server")
    asyncio.run(main(parser.parse_args()))
