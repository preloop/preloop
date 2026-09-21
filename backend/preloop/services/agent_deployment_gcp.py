"""GCE provisioning without cloud credentials on the created agent VM."""

import asyncio
import os
import re
from contextlib import asynccontextmanager
from typing import AsyncIterator
from uuid import UUID, uuid4

import asyncssh
import google.auth
import httpx
from google.auth.transport.requests import Request
from pydantic import SecretStr

from preloop.schemas.agent_deployment import AgentDeploymentSSH
from preloop.services.agent_deployment import DeploymentError, deployment_vm_name


class GCPDeployment:
    """One uniquely named, account-labelled VM owned by this deployment."""

    def __init__(self, account_id: str, request_id: UUID, size: str) -> None:
        self.project = os.getenv("PRELOOP_DEPLOY_GCP_PROJECT", "")
        self.zone = os.getenv("PRELOOP_DEPLOY_GCP_ZONE", "")
        if not all(
            re.fullmatch(r"[a-z][a-z0-9-]{2,62}", value)
            for value in (self.project, self.zone)
        ):
            raise DeploymentError(
                "GCP project and zone must be configured by the operator"
            )
        self.name = deployment_vm_name(account_id, request_id)
        self.account_id = account_id
        self.request_id = request_id
        self.size = size
        self.created = False
        self.attempt = uuid4().hex
        self.operation_name: str | None = None
        self.insert_completed = False
        self.client: httpx.AsyncClient | None = None

    async def request(self, method: str, path: str, body: dict | None = None) -> dict:
        """Use server ADC; only fixed Compute API URLs are ever called."""
        assert self.client is not None
        try:
            response = await self.client.request(method, path, json=body)
        except httpx.HTTPError as exc:
            raise DeploymentError(
                "GCP provisioning connection failed; the operation outcome will be checked during cleanup"
            ) from exc
        if response.status_code == 409:
            if method == "POST":
                self.created = False
            raise DeploymentError(
                "This deployment already has a VM. Check its status before starting a new deployment."
            )
        if response.status_code == 404 and method == "DELETE":
            return {}
        if response.is_error:
            if method == "POST" and 400 <= response.status_code < 500:
                self.created = False
            raise DeploymentError(
                f"GCP provisioning request failed (HTTP {response.status_code}); check the provisioner's permissions and quota"
            )
        return response.json()

    async def operation(self, result: dict) -> None:
        """Wait for a zone operation, surfacing a safe failure without payloads."""
        name = result.get("name")
        if not name:
            return
        for _ in range(90):
            current = await self.request("GET", f"zones/{self.zone}/operations/{name}")
            if current.get("status") == "DONE":
                if name == self.operation_name:
                    self.insert_completed = True
                if current.get("error"):
                    raise DeploymentError(
                        "GCP could not complete the operation; check capacity, permissions and quota"
                    )
                return
            await asyncio.sleep(2)
        raise DeploymentError("GCP provisioning operation timed out")

    async def create(self) -> AgentDeploymentSSH:
        """Create a VM with no service account and verify host key via GCE API."""
        key = asyncssh.generate_private_key("ssh-ed25519")
        public_key = key.export_public_key().decode().strip()
        machine = {
            "standard": "e2-standard-2",
            "performance": "e2-standard-4",
            "high-mem": "e2-highmem-4",
        }[self.size]
        marker = f"PRELOOP_HOSTKEY_{self.request_id.hex}="
        startup = (
            "#!/bin/bash\nset -eu\nssh-keygen -A\necho '"
            + marker
            + '\'"$(cat /etc/ssh/ssh_host_ed25519_key.pub)" > /dev/ttyS0\n'
        )
        network = os.getenv("PRELOOP_DEPLOY_GCP_NETWORK", "global/networks/default")
        # Resource paths are operator configuration, never user-supplied URLs.
        if not re.fullmatch(
            r"(?:projects/[a-z0-9-]+/)?global/networks/[a-z0-9-]+", network
        ):
            raise DeploymentError("The configured GCP network path is invalid")
        body = {
            "name": self.name,
            "machineType": f"zones/{self.zone}/machineTypes/{machine}",
            "labels": {
                "preloop-deployment": self.request_id.hex,
                "preloop-account": self.account_id,
                "preloop-attempt": self.attempt,
            },
            "disks": [
                {
                    "boot": True,
                    "autoDelete": True,
                    "initializeParams": {
                        "sourceImage": "projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64",
                        "diskSizeGb": "30",
                    },
                }
            ],
            "networkInterfaces": [
                {
                    "network": network,
                    "accessConfigs": [
                        {"name": "External NAT", "type": "ONE_TO_ONE_NAT"}
                    ],
                }
            ],
            "serviceAccounts": [],
            "metadata": {
                "items": [
                    {"key": "ssh-keys", "value": f"preloop:{public_key}"},
                    {"key": "block-project-ssh-keys", "value": "true"},
                    {"key": "enable-oslogin", "value": "false"},
                    {"key": "startup-script", "value": startup},
                ]
            },
        }
        # A deterministic resource name prevents duplicate VMs on retry. The
        # attempt label prevents concurrent retry cleanup deleting another
        # attempt's resource, including an already successful deployment.
        ttl = os.getenv("PRELOOP_DEPLOY_GCP_MAX_RUN_SECONDS", "")
        if ttl:
            try:
                seconds = int(ttl)
            except ValueError as exc:
                raise DeploymentError("GCP maximum runtime must be an integer") from exc
            if not 60 <= seconds <= 604800:
                raise DeploymentError(
                    "GCP maximum runtime must be between 60 and 604800 seconds"
                )
            body["scheduling"] = {
                "maxRunDuration": {"seconds": str(seconds)},
                "instanceTerminationAction": "DELETE",
                "automaticRestart": False,
            }
        self.created = True
        result = await self.request("POST", f"zones/{self.zone}/instances", body)
        self.operation_name = result.get("name")
        await self.operation(result)
        for _ in range(90):
            instance = await self.request(
                "GET", f"zones/{self.zone}/instances/{self.name}"
            )
            output = await self.request(
                "GET", f"zones/{self.zone}/instances/{self.name}/serialPort?port=1"
            )
            matches = re.findall(
                re.escape(marker) + r"(ssh-ed25519 [A-Za-z0-9+/=]+)",
                output.get("contents", ""),
            )
            addresses = [
                item.get("natIP")
                for interface in instance.get("networkInterfaces", [])
                for item in interface.get("accessConfigs", [])
                if item.get("natIP")
            ]
            if matches and addresses:
                return AgentDeploymentSSH(
                    host=addresses[0],
                    username="preloop",
                    host_key=matches[-1],
                    private_key=SecretStr(key.export_private_key().decode()),
                )
            await asyncio.sleep(2)
        raise DeploymentError("The new VM did not report its SSH host key in time")

    async def cleanup(self) -> None:
        """Delete only a VM whose server-side labels prove this request owns it."""
        if not self.created:
            return
        assert self.client is not None
        if self.operation_name:
            try:
                await self.operation({"name": self.operation_name})
            except DeploymentError:
                pass  # A failed operation may still leave a resource.
        response = await self.client.get(f"zones/{self.zone}/instances/{self.name}")
        if response.status_code == 404 and self.insert_completed:
            return
        if response.status_code == 404:
            # A lost HTTP response does not prove insertion was rejected.
            for _ in range(30):
                await asyncio.sleep(2)
                response = await self.client.get(
                    f"zones/{self.zone}/instances/{self.name}"
                )
                if response.status_code != 404:
                    break
            if response.status_code == 404:
                raise DeploymentError(
                    f"GCP creation outcome is uncertain for {self.name}; inspect it in GCP"
                )
        if response.is_error:
            raise DeploymentError(
                f"VM cleanup could not verify {self.name}; inspect it in GCP"
            )
        labels = response.json().get("labels", {})
        if (
            labels.get("preloop-deployment") != self.request_id.hex
            or labels.get("preloop-account") != self.account_id
            or labels.get("preloop-attempt") != self.attempt
        ):
            raise DeploymentError(
                "VM cleanup refused because its ownership labels do not match"
            )
        await self.operation(
            await self.request("DELETE", f"zones/{self.zone}/instances/{self.name}")
        )


async def _access_token() -> str:
    """Obtain an access token using the provisioner's server credentials."""

    def resolve() -> str:
        credentials, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/compute"]
        )
        credentials.refresh(Request())
        return str(credentials.token)

    try:
        return await asyncio.to_thread(resolve)
    except Exception as exc:
        raise DeploymentError("GCP provisioner credentials are not available") from exc


@asynccontextmanager
async def provision_gcp(
    account_id: str, request_id: UUID, size: str
) -> AsyncIterator[tuple[AgentDeploymentSSH, str]]:
    """Retain a successful VM; clean failed, timed-out and cancelled attempts."""
    vm = GCPDeployment(account_id, request_id, size)
    token = await _access_token()
    async with httpx.AsyncClient(
        base_url=f"https://compute.googleapis.com/compute/v1/projects/{vm.project}/",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    ) as client:
        vm.client = client
        try:
            ssh = await vm.create()
            # The guest agent can publish its host key just before sshd/user
            # metadata processing is ready. Bound this startup readiness probe.
            from preloop.services.agent_deployment import parse_host_key

            for attempt in range(30):
                try:
                    async with asyncssh.connect(
                        ssh.host,
                        username=ssh.username,
                        known_hosts=([parse_host_key(ssh.host_key)], [], []),
                        client_keys=[
                            asyncssh.import_private_key(
                                ssh.private_key.get_secret_value()
                            )
                        ],
                        agent_path=None,
                        config=None,
                        connect_timeout=5,
                        login_timeout=5,
                    ):
                        break
                except (OSError, asyncssh.Error):
                    if attempt == 29:
                        raise DeploymentError(
                            "The new VM's SSH service did not become ready"
                        )
                    await asyncio.sleep(2)
            yield ssh, vm.name
        except BaseException:
            # Shield cleanup from client/task cancellation, but bound it. A
            # failure explicitly names the retained resource for an operator.
            cleanup = asyncio.create_task(vm.cleanup())
            try:
                await asyncio.wait_for(asyncio.shield(cleanup), timeout=200)
            except asyncio.CancelledError:
                await asyncio.wait_for(asyncio.shield(cleanup), timeout=200)
                raise
            except Exception as exc:
                cleanup.cancel()
                await asyncio.gather(cleanup, return_exceptions=True)
                raise DeploymentError(
                    f"Deployment failed; VM {vm.name} could not be removed automatically. Inspect and remove it in GCP."
                ) from exc
            raise
