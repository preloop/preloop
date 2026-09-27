#!/usr/bin/env sh
# Helm chart render checks for the preloop chart, focused on the CNPG backup
# wiring. Run from the repo root. Requires helm 3 and network access for
# `helm dependency build` (nats subchart).
set -eu

CHART=./helm/preloop

# The chart fails closed on an empty or placeholder jwtSecret (see
# templates/secret.yaml), so every render below supplies a test value.
JWT_TEST_SECRET="helm-render-check-signing-key-not-a-real-secret"
render() { helm template t "$CHART" --set environment.jwtSecret="$JWT_TEST_SECRET" "$@"; }

echo "==> helm dependency build"
helm repo add nats https://nats-io.github.io/k8s/helm/charts >/dev/null 2>&1 || true
helm dependency build "$CHART" >/dev/null

echo "==> helm lint"
helm lint "$CHART" --set environment.jwtSecret="$JWT_TEST_SECRET"

fail() { echo "FAIL: $1" >&2; exit 1; }

echo "==> jwtSecret: empty and placeholder values must refuse to render"
if helm template t "$CHART" >/dev/null 2>&1; then
  fail "chart rendered with the default (empty) jwtSecret"
fi
if helm template t "$CHART" \
  --set environment.jwtSecret=change-this-in-production >/dev/null 2>&1; then
  fail "chart rendered with the published placeholder jwtSecret"
fi
helm template t "$CHART" --set existingSecret=preloop-app >/dev/null 2>&1 \
  || fail "existingSecret does not bypass the jwtSecret guard"

echo "==> defaults: backup must be OFF"
out=$(render)
echo "$out" | grep -q "kind: ScheduledBackup" && fail "ScheduledBackup rendered with defaults"
echo "$out" | grep -q "barmanObjectStore" && fail "barmanObjectStore rendered with defaults"

echo "==> prod profile: backup + ScheduledBackup ON"
out=$(render -f "$CHART/values-backup-prod.yaml")
echo "$out" | grep -q "kind: ScheduledBackup" || fail "ScheduledBackup missing (prod profile)"
echo "$out" | grep -q "barmanObjectStore" || fail "barmanObjectStore missing (prod profile)"
echo "$out" | grep -q 'retentionPolicy: "30d"' || fail "prod retention wrong"
echo "$out" | grep -q 'schedule: "0 0 2 \* \* \*"' || fail "prod schedule wrong"
echo "$out" | grep -q "backupOwnerReference: none" || fail "prod backupOwnerReference should be none"

echo "==> staging profile: backup + ScheduledBackup ON"
out=$(render -f "$CHART/values-backup-staging.yaml")
echo "$out" | grep -q "kind: ScheduledBackup" || fail "ScheduledBackup missing (staging profile)"
echo "$out" | grep -q 'retentionPolicy: "7d"' || fail "staging retention wrong"
echo "$out" | grep -q 'schedule: "0 0 3 \* \* \*"' || fail "staging schedule wrong"
echo "$out" | grep -q "backupOwnerReference: none" || fail "staging backupOwnerReference should be none"

echo "==> backup.enabled without destinationPath must fail fast"
if render --set database.cnpg.backup.enabled=true >/dev/null 2>&1; then
  fail "template rendered despite missing destinationPath"
fi

echo "==> scheduled.enabled=false suppresses ScheduledBackup only"
out=$(render -f "$CHART/values-backup-prod.yaml" \
  --set database.cnpg.backup.scheduled.enabled=false)
echo "$out" | grep -q "kind: ScheduledBackup" && fail "ScheduledBackup rendered when scheduled.enabled=false"
echo "$out" | grep -q "barmanObjectStore" || fail "WAL archiving suppressed by scheduled.enabled=false"

echo "==> endpointURL / serverName render when set"
out=$(render -f "$CHART/values-backup-prod.yaml" \
  --set database.cnpg.backup.endpointURL=https://minio.example.com \
  --set database.cnpg.backup.serverName=preloop-db-v2)
echo "$out" | grep -q "endpointURL: https://minio.example.com" || fail "endpointURL not rendered"
echo "$out" | grep -q "serverName: preloop-db-v2" || fail "serverName not rendered"

echo "==> service role, flow inflight, gateway memory request"
out=$(render)
echo "$out" | grep -A1 'name: PRELOOP_SERVICE_ROLE' | grep -q 'value: "gateway"' \
  || fail "PRELOOP_SERVICE_ROLE=gateway missing"
echo "$out" | grep -A1 'name: PRELOOP_SERVICE_ROLE' | grep -q 'value: "api"' \
  || fail "PRELOOP_SERVICE_ROLE=api missing"
echo "$out" | grep -A1 'name: FLOW_EXECUTION_MAX_INFLIGHT' | grep -q 'value: "10"' \
  || fail "FLOW_EXECUTION_MAX_INFLIGHT missing"
echo "$out" | grep -q 'memory: 768Mi' || fail "gateway memory request 768Mi missing"

echo "==> agent isolation: policy renders by default, in the release namespace"
np=$(render --namespace preloop \
  --show-only templates/agent-networkpolicy.yaml | grep -v '^ *#' | grep -v '^$')
echo "$np" | grep -q "name: t-preloop-agent-execution" \
  || fail "agent NetworkPolicy missing with defaults"
echo "$np" | grep -q "namespace: preloop" \
  || fail "agent policy not placed in the release namespace"
echo "$np" | grep -A2 'podSelector:' | grep -q 'app: agent-execution' \
  || fail "agent policy does not select app=agent-execution pods"
echo "$np" | grep -q 'ingress: \[\]' || fail "agent policy does not deny ingress"
echo "$np" | grep -q 'port: 53' || fail "agent policy blocks DNS"
echo "$np" | grep -q 'k8s-app: kube-dns' || fail "DNS rule is not pinned to the DNS pods"
echo "$np" | grep -q 'app.kubernetes.io/component: api' \
  || fail "agent policy has no egress to the API"
echo "$np" | grep -q 'app.kubernetes.io/component: gateway' \
  || fail "agent policy has no egress to the gateway"
echo "$np" | grep -q 'port: 8000' \
  || fail "agent policy omits the container port (ClusterIP traffic is DNATed to it)"
echo "$np" | grep -q 'cidr: 0.0.0.0/0' || fail "agent policy has no internet egress"
echo "$np" | grep -q '10.0.0.0/8' || fail "cluster CIDRs not carved out of the internet rule"
echo "$np" | grep -q '169.254.169.254/32' || fail "metadata address not carved out of the internet rule"
# Negative assertions: the reason this policy exists.
echo "$np" | grep -q '5432' && fail "agent policy allows the database port"
echo "$np" | grep -q '4222' && fail "agent policy allows the NATS port"
echo "$np" | grep -q 'component: console' && fail "agent policy allows the console"

echo "==> agent isolation: dedicated namespace keeps the namespace-wide deny"
out=$(render --set agentExecution.namespace.create=true)
echo "$out" | grep -q "name: agent-execution-isolation" \
  || fail "namespace default-deny missing when namespace.create=true"
echo "$out" | grep -q "namespace: agent-executions" \
  || fail "agent policy not placed in the agent namespace"

echo "==> agent isolation: can be turned off"
out=$(render --set agentExecution.networkPolicy.enabled=false)
echo "$out" | grep -q "kind: NetworkPolicy" \
  && fail "NetworkPolicy rendered while networkPolicy.enabled=false"

echo "==> agent isolation: no CIDR value at all falls back to RFC1918 plus metadata"
np=$(render --set 'agentExecution.networkPolicy.clusterCidrs=null' \
  --set 'agentExecution.networkPolicy.excludeCIDRs=null' \
  --show-only templates/agent-networkpolicy.yaml | grep -v '^ *#')
for cidr in 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 169.254.169.254/32; do
  echo "$np" | grep -q "$cidr" || fail "fallback carve-out missing $cidr"
done

echo "==> agent isolation: a legacy excludeCIDRs list survives the upgrade unchanged"
# Helm lays saved values over the new chart defaults, so the only way the
# deprecated key can still be read is for the new key to default to empty.
np=$(render \
  --set 'agentExecution.networkPolicy.excludeCIDRs={100.64.0.0/10}' \
  --show-only templates/agent-networkpolicy.yaml | grep -v '^ *#')
echo "$np" | grep -q '100.64.0.0/10' || fail "legacy excludeCIDRs carve-out dropped"
echo "$np" | grep -q '10.0.0.0/8' && fail "legacy excludeCIDRs was merged with the fallback instead of replacing it"
np=$(render \
  --set 'agentExecution.networkPolicy.clusterCidrs={10.244.0.0/16}' \
  --set 'agentExecution.networkPolicy.excludeCIDRs={100.64.0.0/10}' \
  --show-only templates/agent-networkpolicy.yaml | grep -v '^ *#')
echo "$np" | grep -q '10.244.0.0/16' || fail "clusterCidrs not rendered when both keys are set"
echo "$np" | grep -q '100.64.0.0/10' && fail "excludeCIDRs still read although clusterCidrs is set"
render --set 'agentExecution.networkPolicy.excludeCIDRs={100.64.0.0/10}' >/dev/null \
  || fail "chart does not render with a legacy excludeCIDRs value"
# helm template does not emit NOTES.txt, so the deprecation notice is checked
# in the template source, the way backend/tests/helm/test_agent_isolation.py does.
grep -q 'excludeCIDRs is deprecated' "$CHART/templates/NOTES.txt" \
  || fail "NOTES.txt lost the excludeCIDRs deprecation notice"

echo "==> agent isolation: legacy additionalEgressRules still appended"
np=$(render \
  --set 'agentExecution.networkPolicy.additionalEgressRules[0].to[0].ipBlock.cidr=10.42.7.0/24' \
  --show-only templates/agent-networkpolicy.yaml | grep -v '^ *#')
echo "$np" | grep -q '10.42.7.0/24' || fail "legacy additionalEgressRules dropped"

echo "==> agent isolation: nodeCidrs re-admits kubelet probes and nothing else"
np=$(render \
  --set 'agentExecution.networkPolicy.nodeCidrs={10.0.0.0/24}' \
  --show-only templates/agent-networkpolicy.yaml | grep -v '^ *#')
echo "$np" | grep -q 'ingress: \[\]' && fail "ingress still empty although nodeCidrs is set"
echo "$np" | grep -A3 '^  ingress:' | grep -q 'ipBlock' || fail "nodeCidrs did not render an ingress ipBlock"
echo "$np" | grep -q '10.0.0.0/24' || fail "node CIDR missing from the ingress rule"

# Every label key inside a Cilium toEndpoints or fromEndpoints selector must
# name its source. Cilium reads a bare key there as any:, which also matches
# labels from other sources; k8s: is the pod label and the documented form.
bare_endpoint_keys() {
  echo "$1" | grep -v '^ *#' | awk '
    { match($0, /^ */); indent = RLENGTH }
    inside && indent <= stop { inside = 0 }
    labels && indent <= ldepth { labels = 0 }
    /(to|from)Endpoints:/ { inside = 1; stop = ($0 ~ /^ *- /) ? indent + 2 : indent; next }
    inside && /matchLabels:/ { labels = 1; ldepth = ($0 ~ /^ *- /) ? indent + 2 : indent; next }
    inside && labels && NF > 0 && substr($1, 1, length($1) - 1) !~ /:/ { print }
  '
}
assert_prefixed_endpoints() {
  bare=$(bare_endpoint_keys "$1")
  [ -z "$bare" ] || fail "un-prefixed label key inside a Cilium endpoint selector: $bare"
}
# The guard must catch a bare key and pass a prefixed one, or it proves nothing.
[ -n "$(bare_endpoint_keys "$(printf '    - toEndpoints:\n        - matchLabels:\n            app: x\n')")" ] \
  || fail "the endpoint selector guard does not catch a bare key"
[ -z "$(bare_endpoint_keys "$(printf '    - toEndpoints:\n        - matchLabels:\n            k8s:app: x\n            any:tier: dns\n            container:app: x\n      toPorts: []\n')")" ] \
  || fail "the endpoint selector guard flags a prefixed key or a sibling field"

echo "==> agent isolation: Cilium variant replaces the plain policy"
out=$(render --namespace preloop \
  --set agentExecution.networkPolicy.cilium.enabled=true)
echo "$out" | grep -B2 -A6 'kind: NetworkPolicy' | grep -q 'name: t-preloop-agent-execution' \
  && fail "plain agent NetworkPolicy still rendered alongside the Cilium policy"
cnp=$(render --namespace preloop \
  --set agentExecution.networkPolicy.cilium.enabled=true \
  --show-only templates/agent-ciliumnetworkpolicy.yaml | grep -v '^ *#')
echo "$cnp" | grep -q 'kind: CiliumNetworkPolicy' || fail "CiliumNetworkPolicy missing"
echo "$cnp" | grep -q 'name: t-preloop-agent-execution' || fail "Cilium policy has the wrong name"
echo "$cnp" | grep -A2 'endpointSelector:' | grep -q 'app: agent-execution' \
  || fail "Cilium policy does not select app=agent-execution"
echo "$cnp" | grep -A2 '^  ingress:' | grep -q 'fromEntities' || fail "Cilium ingress rule missing"
echo "$cnp" | grep -A3 '^  ingress:' | grep -q '\- host' || fail "Cilium ingress does not admit the host (kubelet)"
echo "$cnp" | grep -q 'ingressDeny' && fail "ingressDeny rendered while allowHostIngress is true"
echo "$cnp" | grep -q 'k8s:io.kubernetes.pod.namespace: kube-system' || fail "Cilium DNS rule missing"
echo "$cnp" | grep -q 'k8s:k8s-app: kube-dns' || fail "Cilium DNS rule not pinned to the DNS pods"
echo "$cnp" | grep -q 'port: "53"' || fail "Cilium DNS port missing"
echo "$cnp" | grep -q 'k8s:io.kubernetes.pod.namespace: preloop' || fail "Cilium control plane rule not scoped to the release namespace"
echo "$cnp" | grep -q 'k8s:app.kubernetes.io/component: api' || fail "Cilium policy has no egress to the API"
echo "$cnp" | grep -q 'k8s:app.kubernetes.io/component: gateway' || fail "Cilium policy has no egress to the gateway"
echo "$cnp" | grep -q 'k8s:app.kubernetes.io/name: preloop' || fail "Cilium control plane rule lost the chart name label"
echo "$cnp" | grep -q 'k8s:app.kubernetes.io/instance: t' || fail "Cilium control plane rule lost the release label"
echo "$cnp" | grep -q 'port: "8000"' || fail "Cilium policy omits the container port"
echo "$cnp" | grep -A3 'toEntities:' | grep -q '\- world' || fail "Cilium internet rule missing world"
echo "$cnp" | grep -A3 'toEntities:' | grep -q '\- host' || fail "Cilium internet rule missing host (public hairpin)"
echo "$cnp" | grep -A3 'egressDeny:' | grep -q '169.254.169.254/32' || fail "Cilium policy does not deny the metadata address"
echo "$cnp" | grep -q 'ipBlock' && fail "Cilium policy contains an ipBlock, which Cilium does not match against nodes"
echo "$cnp" | grep -q '5432' && fail "Cilium policy allows the database port"
echo "$cnp" | grep -q '4222' && fail "Cilium policy allows the NATS port"
echo "$cnp" | grep -q 'component: console' && fail "Cilium policy allows the console"
assert_prefixed_endpoints "$cnp"

echo "==> agent isolation: Cilium variant, strict ingress and knobs"
cnp=$(render --namespace preloop \
  --set agentExecution.networkPolicy.cilium.enabled=true \
  --set agentExecution.networkPolicy.cilium.allowHostIngress=false \
  --set 'agentExecution.networkPolicy.cilium.internetEntities={world,host,remote-node}' \
  --set 'agentExecution.networkPolicy.internetPorts[0].port=443' \
  --set 'agentExecution.networkPolicy.cilium.extraEgress[0].toCIDR[0]=203.0.113.0/24' \
  --show-only templates/agent-ciliumnetworkpolicy.yaml | grep -v '^ *#')
echo "$cnp" | grep -A1 '^  ingress:' | grep -q '\- {}' || fail "strict Cilium ingress missing the empty rule"
echo "$cnp" | grep -A2 'ingressDeny:' | grep -q '\- all' || fail "strict Cilium ingress missing the deny from all"
echo "$cnp" | grep -A4 'toEntities:' | grep -q '\- remote-node' || fail "internetEntities override not rendered"
echo "$cnp" | grep -q 'port: "443"' || fail "internetPorts not applied to the Cilium internet rule"
echo "$cnp" | grep -q '203.0.113.0/24' || fail "cilium.extraEgress not appended"
assert_prefixed_endpoints "$cnp"
cnp=$(render --namespace preloop \
  --set agentExecution.networkPolicy.cilium.enabled=true \
  --set 'agentExecution.networkPolicy.dns.podSelectorLabels.k8s-app=null' \
  --set 'agentExecution.networkPolicy.dns.podSelectorLabels.app=coredns' \
  --set 'agentExecution.networkPolicy.dns.podSelectorLabels.any:tier=dns' \
  --show-only templates/agent-ciliumnetworkpolicy.yaml | grep -v '^ *#')
echo "$cnp" | grep -q 'k8s:app: coredns' || fail "custom DNS pod label not rendered with the k8s: source"
echo "$cnp" | grep -q '^ *any:tier: dns' || fail "a caller-supplied label source was not kept as is"
echo "$cnp" | grep -q 'k8s:any:tier' && fail "a caller-supplied label source was prefixed twice"
assert_prefixed_endpoints "$cnp"

echo "==> agent isolation: Cilium variant keeps the namespace-wide deny in a dedicated namespace"
out=$(render \
  --set agentExecution.networkPolicy.cilium.enabled=true \
  --set agentExecution.namespace.create=true)
echo "$out" | grep -q 'kind: CiliumNetworkPolicy' || fail "Cilium policy missing in dedicated namespace mode"
echo "$out" | grep -q 'name: agent-execution-isolation' || fail "namespace default-deny dropped in Cilium mode"
echo "$out" | grep -q 'namespace: agent-executions' || fail "Cilium policy not placed in the agent namespace"
assert_prefixed_endpoints "$out"

echo "==> agent isolation: quota does not depend on the network policy"
out=$(render --set agentExecution.namespace.create=true \
  --set agentExecution.networkPolicy.enabled=false)
echo "$out" | grep -q 'kind: ResourceQuota' || fail "ResourceQuota dropped when networkPolicy.enabled=false"
echo "$out" | grep -q 'kind: NetworkPolicy' && fail "NetworkPolicy rendered while networkPolicy.enabled=false"

echo "==> control plane ingress policy: opt in, needs pod CIDRs"
if render \
  --set agentExecution.networkPolicy.controlPlaneIngress.enabled=true >/dev/null 2>&1; then
  fail "control plane policy rendered without podCidrs"
fi
out=$(render \
  --set agentExecution.networkPolicy.controlPlaneIngress.enabled=true \
  --set 'agentExecution.networkPolicy.controlPlaneIngress.podCidrs={10.244.0.0/16}')
echo "$out" | grep -q "name: t-preloop-control-plane-ingress" \
  || fail "control plane policy missing when enabled"
echo "$out" | grep -q "10.244.0.0/16" || fail "pod CIDR not carved out of the ipBlock"

echo "==> credentials: no literal DATABASE_URL or SMTP_PASSWORD in any pod spec"
out=$(render --set config.smtp.host=smtp.example.com \
  --set config.smtp.password=not-a-real-password)
echo "$out" | grep -A1 'name: DATABASE_URL' | grep -q 'value: postgresql' \
  && fail "DATABASE_URL still rendered as a literal env value"
hits=$(echo "$out" | grep -c 'not-a-real-password' || true)
[ "$hits" = "1" ] \
  || fail "SMTP password should appear once (in the Secret), saw $hits occurrences"
echo "$out" |  grep -A4 'name: DATABASE_URL' | grep -q 'key: "database-url"' \
  || fail "DATABASE_URL secretKeyRef missing"
echo "$out" | grep -q 'checksum/credentials:' \
  || fail "credentials checksum annotation missing (pods would not roll)"

echo "==> credentials: operator Secret wins and the chart Secret is dropped"
out=$(render --set database.urlFromSecret.name=my-db-secret)
echo "$out" |  grep -A4 'name: DATABASE_URL' | grep -q 'name: "my-db-secret"' \
  || fail "database.urlFromSecret not honoured"
echo "$out" | grep -q "t-preloop-credentials" \
  && fail "chart credentials Secret rendered even though urlFromSecret is set"

echo "==> superuser access can be disabled"
out=$(render --set database.cnpg.enableSuperuserAccess=false)
echo "$out" | grep -q "enableSuperuserAccess: false" \
  || fail "enableSuperuserAccess knob not wired"
echo "$out" | grep -q "t-preloop-db-superuser" \
  && fail "superuser Secret rendered while superuser access is disabled"

echo "All helm render checks passed."
