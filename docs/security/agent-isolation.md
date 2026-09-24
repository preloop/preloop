# Agent pod isolation

An agent pod runs code a model wrote, on a repository the model can edit,
with a credential the platform minted for it. It is the least trusted thing
in the deployment. This page states what such a pod can reach, what the
chart's NetworkPolicy changes, and what is still open afterwards.

## What runs, and where

The executor creates one Job per flow execution
(`backend/preloop/agents/container.py`). Its pod carries the labels
`app=agent-execution`, `preloop.agent_type`, `preloop.flow_id`, and
`preloop.execution_id`. The namespace comes from
`AGENT_EXECUTION_NAMESPACE`: the chart points it at a dedicated namespace
when `agentExecution.namespace.create` is true, and at the release
namespace otherwise. The second case is the easy one to deploy and the
dangerous one to leave unguarded, because the database, NATS, the console,
and the other tenants' agent pods are then neighbours.

Nothing in the cluster dials into an agent pod. Output leaves it two ways:

- the pod log stream, which the runner reads through the API server
  (`read_namespaced_pod_log`), carrying the result artifact, the evidence
  archive, and the workspace snapshot as base64 lines;
- direct uploads made by the pod itself, when checkpointing or evidence
  upload is enabled (`backend/preloop/services/checkpoint_runtime.py`),
  which are outbound HTTP calls to the public Preloop URL.

The kubelet does dial in. Flows with an environment profile get native
sidecar containers in the same pod, each with a TCP `startupProbe`
(`_environment_sidecars` in `container.py`), and the kubelet runs that
probe from the node's own network namespace. So the policy denies ingress
from every pod and relies on the CNI admitting the local host, which the
three CNIs the chart names all do:

- Cilium: host-to-local-endpoint traffic is governed by `--allow-localhost`
  (default `auto`), not by NetworkPolicy; the Cilium variant below also
  names the `host` entity explicitly.
- Calico: "Calico allows connections the host makes to the workloads
  running on that host. Some orchestrators like Kubernetes depend on this
  connectivity for health checking the workload." (Calico docs, Protect
  hosts.)
- Antrea: node-to-local-pod traffic "will always be allowed to make sure
  that agents on a Node (e.g. system daemons, kubelet) can communicate with
  all Pods on that Node to perform liveness and readiness probes" (Antrea
  network policy docs; the OVS pipeline marks probe packets and bypasses
  the ingress tables for them).

On a CNI that does enforce node-sourced traffic, list the node addresses
in `agentExecution.networkPolicy.nodeCidrs` and the policy re-admits them
on ingress; nothing else is admitted either way. A probe that fails under
the policy shows up as a sidecar that never becomes ready and a Job that
ends with the sidecar's `startupProbe` failure in `kubectl describe pod`.

## Without a NetworkPolicy

A cluster with no policy gives every pod a flat network. From an agent pod
that means:

- `preloop-db-rw:5432`, with whatever credentials it can find. If the chart
  was installed with its default values, those credentials are also the
  defaults, and the connection string used to sit in the API pod spec,
  which any in-cluster reader could fetch.
- `preloop-nats:4222`, the event bus every worker reads from.
- the console and the API, on every port, not just HTTP.
- other agent pods, which belong to other flows and possibly other
  accounts.
- the cloud metadata service, if the node exposes one.

None of that is needed to run an agent.

## With the policy

`helm/preloop/templates/agent-networkpolicy.yaml` selects pods labelled
`app=agent-execution` and:

- denies all ingress (or admits only `nodeCidrs`, see above);
- allows egress to the DNS pods on 53, selected by
  `agentExecution.networkPolicy.dns.*` (kube-dns in `kube-system` by
  default; Helm merges maps, so a cluster whose DNS lives elsewhere nulls
  the default label keys and adds its own, as the values comment shows);
- allows egress to the API and gateway pods on the HTTP ports. Both 80 and
  8000 are listed: a ClusterIP connection is translated to the pod IP and
  target port before policy is evaluated on most CNIs, so a rule naming
  only the service port silently drops the traffic;
- allows egress to `0.0.0.0/0` minus the cluster pod and service CIDRs
  (`agentExecution.networkPolicy.clusterCidrs`; when that is empty the
  deprecated `excludeCIDRs`, and when both are empty the three RFC1918
  ranges plus `169.254.169.254/32`), which is what model providers, git
  remotes, and package registries need;
- allows nothing else in-cluster. The database, NATS, the console, and
  other agent pods fall off the list.

`agentExecution.networkPolicy.extraEgress` takes verbatim rules for the
endpoints a particular deployment needs (an in-cluster registry, a
node-local DNS cache on a link-local address, an internal artifact store).

A second, opt-in policy
(`agentExecution.networkPolicy.controlPlaneIngress`) says the same thing
from the API and gateway side: traffic from `app=agent-execution` pods is
accepted on the HTTP ports only, everything else is accepted as before. It
needs the cluster pod CIDRs, because an ingress rule that did not re-admit
node-sourced traffic would also cut off load balancer health checks and
host-network ingress controllers.

Policies only do something on a CNI that enforces them. Cilium, Calico, and
Antrea do. On a CNI that does not, these objects render and have no effect.

## Upgrading from the previous chart

The previous template read `excludeCIDRs` and `additionalEgressRules`. The
new keys, `clusterCidrs` and `extraEgress`, ship empty and fall back to
the old ones, so a release that customised either list (a cluster on
`100.64.0.0/10`, say) keeps it across the upgrade without touching values.
Helm lays saved values over the new chart's defaults, which is why the new
defaults have to be empty: a populated `clusterCidrs` default would win
over a saved `excludeCIDRs` every time. `helm upgrade` prints a notice
while the deprecated key is the one in use.

## Cilium

Cilium evaluates policy against identities, not addresses, and by default
does not match a NetworkPolicy `ipBlock` against anything inside the
cluster: "By default, ipBlock rules in NetworkPolicy do not match
intra-cluster IPs (such as Pod or Node IPs). Setting the
`--policy-cidr-match-mode` option (or equivalent Helm value
`policyCIDRMatchMode`) to `pods` or `nodes` allows ipBlock rules to match
intra-cluster IPs." (Cilium docs, Kubernetes NetworkPolicy.)

The consequence for the plain policy: the `0.0.0.0/0` rule never matches a
node. When the deployment's public URL resolves to an address that an
ingress or load balancer terminates on a node, which is the usual shape
of a default install, an agent pod calling that URL is calling a node,
and the plain policy drops it. Checkpoint uploads, evidence uploads, and
any flow that uses the public URL for MCP or the gateway fail with a
connection timeout while the database stays correctly unreachable.

`agentExecution.networkPolicy.cilium.enabled=true` renders a
`CiliumNetworkPolicy` in place of the plain object, with the same allow
list expressed in Cilium's terms:

- ingress: `fromEntities: [host]`, the kubelet, and nothing else
  (`cilium.allowHostIngress=false` denies all ingress instead, with an
  explicit `ingressDeny` from `all`);
- egress to the DNS pods, selected by namespace and pod label
  (`k8s:io.kubernetes.pod.namespace` plus `dns.podSelectorLabels`);
- egress to the API and gateway pods, selected by the chart's labels in
  the release namespace, on `controlPlanePorts`. Every label key inside
  these `toEndpoints` selectors carries the `k8s:` source prefix. Cilium
  reads a bare key there as `any:`, which matches the label from any
  source rather than the pod label alone; the prefixed form is the one
  Cilium documents, and the render checks fail on a bare key;
- egress to `toEntities: [world, host]` (`cilium.internetEntities`).
  `world` is every address outside the cluster, so the CIDR carve-out is
  unnecessary: the database, NATS, the console, and other pods are cluster
  identities and never match. `host` is the node the pod runs on, which is
  where the public hairpin lands. Add `remote-node` if that address can
  land on a different node than the one running the pod;
- `egressDeny: toCIDR` for `cilium.egressDenyCidrs`, by default the cloud
  metadata address, which Cilium classes as `world` and which a deny rule
  removes regardless of the allow above;
- `cilium.extraEgress`, appended verbatim. These are CiliumNetworkPolicy
  egress rules; the plain `extraEgress` list is not translated, and label
  keys inside their `toEndpoints` should carry `k8s:` like the chart's own.

The namespace-wide default deny for a dedicated agent namespace stays a
plain NetworkPolicy in both modes; it selects pods, not addresses, so
Cilium enforces it as written. `controlPlaneIngress` should stay off on
Cilium, since it relies on an `ipBlock` to re-admit node-sourced traffic
to the API and gateway.

## The credential an agent carries

`create_flow_runtime_token`
(`backend/preloop/services/flow_runtime_token.py`) mints one API key per
execution, named `Flow Execution <execution id>`:

- scopes `mcp:read` and `mcp:write`;
- two hour expiry;
- `context_data` binding it to the flow, the execution, the runtime
  session, and the flow's allowed MCP servers and tools;
- owned by the account's primary active user, because MCP tools act on that
  account;
- revoked when the execution ends, by execution rather than by key id, so
  an execution handed between workers does not leave a live key behind
  (`revoke_flow_runtime_tokens`).

The key reaches the pod as `PRELOOP_API_TOKEN` (and inside `MCP_CONFIG_JSON`)
only when the flow allows MCP servers or tools.

The limits worth knowing:

- **Scopes are recorded, not enforced.** The generic API key path
  (`backend/preloop/api/auth/jwt.py`) authenticates the key and returns the
  owning user; it does not compare the requested route against the key's
  scopes, and the MCP HTTP layer says as much in
  `backend/preloop/services/mcp_http.py` ("we do not use scopes"). For two
  hours the token is as powerful as the user it belongs to.
- **The allow lists live in the token context, not in the token check.**
  `allowed_mcp_servers` and `allowed_mcp_tools` scope what the MCP layer
  offers the agent; they are not a second authorization boundary. Codex
  does not open a Preloop MCP session when both lists are empty, so an
  unused client cannot reconnect until the flow timeout.
- **Shell is a separate control.** `agent_config.sandbox_type: read-only`
  launches Codex with `--sandbox read-only`, disables the `shell_tool`
  feature, and does not pass `--yolo`. `config.toml` pins
  `approval_policy = "never"`, which `codex exec` already defaults to, so
  the run does not wait for a person. Any other value, including the
  preset default `exec`, keeps `--yolo`.

Reducing that blast radius is a backend change, not a chart change: enforce
the scopes on the key, and give the runtime principal its own role instead
of the primary user's.

## Residual risks

- **Cloud metadata service.** 169.254.169.254 is link-local, so a cluster
  CIDR carve-out does not cover it. The chart's fallback list and the
  Cilium variant's `egressDenyCidrs` both exclude it by default; an
  operator who sets `clusterCidrs` explicitly has to keep it in the list,
  and the node pool should block it as well (IMDSv2 hop limit 1, GKE
  metadata concealment), since a NetworkPolicy cannot express a deny.
- **Other namespaces.** The policy names the release namespace for the
  control plane and the internet for everything else. A workload in a third
  namespace with a routable ClusterIP is unreachable, but a workload
  reachable on a public address is not.
- **DNS exfiltration.** Egress to kube-dns on 53 is unrestricted in
  content. Data can leave in query names. Restricting that needs a DNS-aware
  policy (CiliumNetworkPolicy `toFQDNs`) or an egress proxy.
- **Egress to the whole internet.** The policy limits where an agent can go
  inside the cluster, not what it can post to a pastebin. Deployments that
  need that constraint should replace the `0.0.0.0/0` rule with an FQDN
  policy or route agents through a proxy (`extraEgress` plus the proxy env
  in `agentExecution`). A sandboxed browser uses the allowlist sidecar in
  `environments/egress-proxy` (`environments/egress-proxy/README.md`); that
  proxy, not an MCP tool list, is the network boundary.
- **The API is still one hop away.** MCP and the model gateway are exactly
  what the agent is supposed to reach, so the credential above, not the
  network, is what limits it.
- **Shared namespace, shared service account.** Agent Jobs do not set
  `serviceAccountName`, so they run as the namespace default account, and
  the token is only left unmounted in isolated publication mode
  (`automount_service_account_token=False`). A policy does not stop a pod
  from using a token it holds, so the default account in the agent
  namespace must stay unprivileged. The carve-out does help here: the API
  server's ClusterIP lives in the service CIDR, so the policy removes the
  usual path to it, and the agent manager Role
  (`helm/preloop/templates/agent-rbac.yaml`) is bound to the Preloop
  service account, not to the agents.

## Related

- `docs/operations/database-credentials.md`: rotating the database
  credentials the old topology exposed.
- `docs/architecture/security.md`: platform-wide model.
