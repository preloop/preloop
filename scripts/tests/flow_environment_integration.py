"""Disposable Docker/Kubernetes environment and HTTP recovery integration.

Requires a migrated disposable DATABASE_URL, a locally built profile image,
pgvector image, and an explicitly selected disposable kubeconfig. Never uses
the operator's current Kubernetes context. No model/provider calls are made.
"""

import argparse
import asyncio
import json
import os
import tempfile
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

from preloop.agents.codex import CodexAgent
from preloop.api.endpoints.flow_artifacts import router
from preloop.config import settings
from preloop.models import models
from preloop.models.crud import crud_flow_execution
from preloop.services.checkpoint_runtime import checkpoint_context


from kubernetes_guard import disposable_client


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
        return "\n".join(logs) if isinstance(logs, list) else str(logs)
    finally:
        if ref:
            await agent.stop(ref)
        await agent.aclose()


async def main(args: argparse.Namespace) -> None:
    """Verify real SQL/browser dependencies and recover after a killed sandbox."""
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
echo uncommitted > /workspace/repo/tracked
echo untracked > /workspace/repo/scratch
_preloop_checkpoint
kill -9 $$
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
echo RECOVERY_PROBE_OK
"""
                second = await run_probe(followup, kubernetes=bool(args.kubeconfig))
                assert "RECOVERY_PROBE_OK" in second, second[-3000:]
                assert "PRELOOP_ENVIRONMENT cache_hit" in second, second[-3000:]
                print(
                    json.dumps(
                        {
                            "runtime": "kubernetes" if args.kubeconfig else "docker",
                            "database": "passed",
                            "browser": "passed",
                            "killed_execution_recovery": "passed",
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
    parser.add_argument("--postgres-image", required=True)
    parser.add_argument("--endpoint", default="http://host.docker.internal:25440")
    parser.add_argument("--port", type=int, default=25440)
    parser.add_argument("--kubeconfig")
    parser.add_argument("--kube-context", default="kind-preloop-recovery-test")
    parser.add_argument("--expected-api-server")
    asyncio.run(main(parser.parse_args()))
