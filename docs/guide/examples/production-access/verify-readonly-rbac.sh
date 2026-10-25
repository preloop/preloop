#!/usr/bin/env bash
# Ask the API server what the k8s-readonly ServiceAccount may do, and fail if
# it can do anything this profile should not. Changes nothing on the cluster.
#
#   bash verify-readonly-rbac.sh
#   NAMESPACE=payments bash verify-readonly-rbac.sh   # check one namespace only
#
# Run it with a kubeconfig that may impersonate ServiceAccounts (a cluster
# administrator's). Set NAMESPACE when you bound view per namespace instead
# of with the ClusterRoleBinding in k8s-readonly-rbac.yaml.
set -euo pipefail

SUBJECT="system:serviceaccount:${SA_NAMESPACE:-agent-access}:${SA_NAME:-agent-readonly}"
if [ -n "${NAMESPACE:-}" ]; then
  scope="--namespace=$NAMESPACE"
else
  scope="--all-namespaces"
fi
failures=0

# check <yes|no> <namespaced|cluster> <verb> <resource> [subresource]
check() {
  local want="$1" kind="$2" verb="$3" resource="$4" sub="${5:-}" got=no
  local args=(auth can-i "$verb" "$resource" "--as=$SUBJECT" --quiet)
  if [ -n "$sub" ]; then
    args+=("--subresource=$sub")
  fi
  if [ "$kind" = namespaced ]; then
    args+=("$scope")
  fi
  # kubectl exits 0 for yes and non-zero for no or an error. An error counts
  # as no, so the "yes" checks also catch a kubeconfig that cannot
  # impersonate.
  if kubectl "${args[@]}" >/dev/null 2>&1; then
    got=yes
  fi
  local label="$verb $resource${sub:+/$sub}"
  if [ "$got" = "$want" ]; then
    printf 'ok    %-40s %s\n' "$label" "$got"
  else
    printf 'FAIL  %-40s %s (want %s)\n' "$label" "$got" "$want"
    failures=$((failures + 1))
  fi
}

# Reads the investigator needs.
check yes namespaced get pods
check yes namespaced list pods
check yes namespaced get pods log
check yes namespaced list events
check yes namespaced list deployments.apps
if [ -z "${NAMESPACE:-}" ]; then
  check yes cluster list namespaces
fi

# Writes, and reads that lead to credentials, this profile must not have.
check no namespaced create pods
check no namespaced delete pods
check no namespaced create pods exec
check no namespaced create pods attach
check no namespaced create pods portforward
check no namespaced patch deployments.apps
check no namespaced update deployments.apps scale
check no namespaced delete deployments.apps
check no namespaced create jobs.batch
check no namespaced get secrets
check no namespaced list secrets
check no namespaced create serviceaccounts token
check no namespaced '*' '*'
check no cluster delete namespaces
check no cluster get nodes proxy
check no cluster create clusterrolebindings
check no cluster impersonate users

# The checks above ask fixed questions. A chart can aggregate rules for any
# resource into view, and another binding can add more, so also read the
# rules the ServiceAccount has in each namespace. view only reads: any other
# verb, and any rule for Secrets, for all resources ('*') or for the exec,
# attach, portforward and proxy subresources, came from somewhere else.
# Kubernetes lets every authenticated user create the self-review objects.
# Each output line starts with "warn " or "rule ".
# shellcheck disable=SC2016 # an awk program: $0 and $1 are awk's
UNEXPECTED_RULES='
/^[Ww]arning:/ { print "warn " $0; next }
/^Resources[ \t]+Non-Resource URLs/ || NF == 0 { next }
{
  # The verbs are the last bracketed list on the line.
  n = split($0, part, /\[/)
  if (n < 2 || part[n] !~ /\]/) { print "rule " $0; next }
  verbs = part[n]
  sub(/\].*/, "", verbs)
  resource = ($0 ~ /^[ \t]/) ? "" : $1
  bad = resource ~ /^\*/ || resource == "secrets" || resource ~ "^secrets[./]" ||
    resource ~ "/(exec|attach|portforward|proxy)$"
  n = split(verbs, v, " ")
  for (i = 1; i <= n; i++) {
    if (v[i] == "get" || v[i] == "list" || v[i] == "watch") continue
    if (v[i] == "create" && resource ~ SELF_REVIEW) continue
    bad = 1
  }
  if (bad) { line = $0; gsub(/[ \t]+/, " ", line); sub(/^ /, "", line); print "rule " line }
}'
SELF_REVIEW='^(selfsubjectaccessreviews|selfsubjectrulesreviews)[.]authorization[.]k8s[.]io$|^selfsubjectreviews[.]authentication[.]k8s[.]io$'

if [ -n "${NAMESPACE:-}" ]; then
  namespaces="$NAMESPACE"
elif ! namespaces="$(kubectl get namespaces -o jsonpath='{.items[*].metadata.name}')"; then
  echo "could not list namespaces" >&2
  exit 1
fi
checked=0
extra=0
for ns in $namespaces; do
  checked=$((checked + 1))
  if ! rules="$(kubectl auth can-i --list "--as=$SUBJECT" "--namespace=$ns" 2>&1)"; then
    printf 'FAIL  %-40s %s\n' "rules in $ns" "$rules"
    extra=$((extra + 1))
    continue
  fi
  while IFS= read -r finding; do
    case "$finding" in
      "warn "*) printf 'warn  %-40s %s\n' "rules in $ns" "${finding#warn }" ;;
      "rule "*)
        printf 'FAIL  %-40s %s\n' "rules in $ns" "${finding#rule }"
        extra=$((extra + 1))
        ;;
    esac
  done <<EOF
$(printf '%s\n' "$rules" | awk -v SELF_REVIEW="$SELF_REVIEW" "$UNEXPECTED_RULES")
EOF
done
if [ "$extra" -eq 0 ]; then
  printf 'ok    %-40s %s\n' "rules in $checked namespace(s)" "get, list and watch only"
fi
failures=$((failures + extra))

if [ "$failures" -gt 0 ]; then
  echo "$failures check(s) failed for $SUBJECT" >&2
  exit 1
fi
echo "All checks passed for $SUBJECT"
