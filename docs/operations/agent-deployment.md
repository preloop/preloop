# Deploy agents onto remote Linux hosts

The console's SSH and GCP deployment actions run the same `preloop agents
install-runtime` and live validation path as the CLI. Success requires a real
registered agent, its selected model binding, runtime version, and validated
control plugin. Installer output is not streamed into the API or audit log
because upstream tools can include credentials in their diagnostics.

Enable these methods explicitly on the API process:

```dotenv
PRELOOP_AGENT_DEPLOYMENT_ENABLED=true
PRELOOP_URL=https://preloop.example.com
# Optional. Private networks are refused unless the operator lists them.
PRELOOP_DEPLOY_SSH_ALLOWED_CIDRS=192.168.10.0/24
# Optional GCP backend, using the API process's Application Default Credentials.
PRELOOP_DEPLOY_GCP_PROJECT=my-project
PRELOOP_DEPLOY_GCP_ZONE=us-central1-a
PRELOOP_DEPLOY_GCP_NETWORK=global/networks/default
```

SSH deployment requires an account owner or administrator, a password or
unencrypted private key, and an independently verified SSH host public key
such as `ssh-ed25519 AAAA...`. Obtain the host key from the host's console or
another trusted administrator. A network scan alone does not authenticate it.
The API never uses its own SSH agent, personal keys, or automatic trust of an
unknown host. It resolves the destination once and pins that address. Loopback,
link-local, multicast and metadata destinations are always refused.

Linux targets need Bash, curl, Python 3, flock, internet access to the runtime
publishers, and any sudo access required by their official installers. Agent
installation may take several minutes. Configure the reverse proxy read timeout
to at least 930 seconds. The shipped Docker and Helm console nginx templates
apply this budget to the exact deployment endpoint. For an NGINX ingress in
front of the console, also set `ingress.annotations` keys
`nginx.ingress.kubernetes.io/proxy-read-timeout` and
`nginx.ingress.kubernetes.io/proxy-send-timeout` to `"930"` (or longer).
The API bounds work to 700 seconds and reserves up to
200 seconds to clean up a failed GCP VM. Closing the browser may not cancel a
request already accepted by the server. Check the agent registry and GCP
resource before retrying; reuse the deployment's idempotency key after an
uncertain HTTP result. The deterministic VM name prevents duplicate resources.

GCP creates Ubuntu 24.04 VMs without an attached service account. It obtains the
new SSH host key from the authenticated Compute serial-output API, blocks
project-wide SSH keys, and uses a transient deployment key. The provisioner's
cloud credentials remain on the Preloop API server. Failed installations,
validation failures, timeouts, and task cancellations remove newly created VMs;
a cleanup failure identifies the resource in the API error for manual removal.
A successful VM remains until the operator deletes it, and continues to incur
normal GCP charges.

Use a dedicated provisioner identity with only:

- `compute.instances.create`, `get`, `delete`, `getSerialPortOutput`,
  `setMetadata`, `setLabels`
- `compute.disks.create`
- `compute.subnetworks.use`, `useExternalIp`, `compute.networks.use`
- `compute.zoneOperations.get`

Restrict its instance permissions to the selected zone and names beginning
`preloop-agent-` using an IAM condition, and permit SSH from the Preloop server
in the selected network's firewall. The Ubuntu public image supplies image-read
access. The service identity needs no IAM administration, storage access,
service-account impersonation, or key-management permission.

For an unreleased CLI candidate, the operator may set `PRELOOP_DEPLOY_CLI_URL`
to an HTTPS Linux binary URL and `PRELOOP_DEPLOY_CLI_SHA256` to its SHA256. Both
are required together and the host verifies the downloaded binary before use.
Otherwise deployment uses the normal published CLI installer. Candidate binaries
must match the host architecture; production should use the published installer.

The deployment API is synchronous: `POST /api/v1/agent-deployments` accepts
`idempotency_key`, `runtime` (`hermes` or `openclaw`), `model_id`, `target`
(`ssh` or `gcp`), optional `compute_size`, and SSH connection fields for SSH.
`GET /api/v1/agent-deployments/capabilities` reports configured methods.
SSH secrets are used only in request memory. Enrollment uses a hashed temporary
API key with the initiating owner's permissions, a twenty-minute expiry, and
revocation after success, failure, or cancellation. Official runtime and uv
installers run before this key is exported; the CLI's `--install-only` phase
requires no login and skips enrollment. Only the trusted target's subsequent
`--skip-install` onboarding phase receives it. The target is trusted with these
bootstrap permissions; this is not a narrower enrollment-only scope.

OpenClaw control uses a healthy existing Python environment or installs uv,
aiohttp and PyYAML in `~/.preloop-agent-control/venv`, without system pip.
Installation refuses successful onboarding if the required control plugin or
channel is not ready.

Account-scoped start/failure/completion
audit events record identifiers and the observed runtime version.

For disposable environments, set `PRELOOP_DEPLOY_GCP_MAX_RUN_SECONDS=14400`.
GCE will delete successful as well as failed test VMs after four hours. The
optional value must be between 60 seconds and seven days; unset means that a
successful VM has no automatic lifetime limit.
