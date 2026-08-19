#!/usr/bin/env sh
# Helm chart render checks for the preloop chart, focused on the CNPG backup
# wiring. Run from the repo root. Requires helm 3 and network access for
# `helm dependency build` (nats subchart).
set -eu

CHART=./helm/preloop

echo "==> helm dependency build"
helm repo add nats https://nats-io.github.io/k8s/helm/charts >/dev/null 2>&1 || true
helm dependency build "$CHART" >/dev/null

echo "==> helm lint"
helm lint "$CHART"

fail() { echo "FAIL: $1" >&2; exit 1; }

echo "==> defaults: backup must be OFF"
out=$(helm template t "$CHART")
echo "$out" | grep -q "kind: ScheduledBackup" && fail "ScheduledBackup rendered with defaults"
echo "$out" | grep -q "barmanObjectStore" && fail "barmanObjectStore rendered with defaults"

echo "==> prod profile: backup + ScheduledBackup ON"
out=$(helm template t "$CHART" -f "$CHART/values-backup-prod.yaml")
echo "$out" | grep -q "kind: ScheduledBackup" || fail "ScheduledBackup missing (prod profile)"
echo "$out" | grep -q "barmanObjectStore" || fail "barmanObjectStore missing (prod profile)"
echo "$out" | grep -q 'retentionPolicy: "30d"' || fail "prod retention wrong"
echo "$out" | grep -q 'schedule: "0 0 2 \* \* \*"' || fail "prod schedule wrong"

echo "==> staging profile: backup + ScheduledBackup ON"
out=$(helm template t "$CHART" -f "$CHART/values-backup-staging.yaml")
echo "$out" | grep -q "kind: ScheduledBackup" || fail "ScheduledBackup missing (staging profile)"
echo "$out" | grep -q 'retentionPolicy: "7d"' || fail "staging retention wrong"
echo "$out" | grep -q 'schedule: "0 0 3 \* \* \*"' || fail "staging schedule wrong"

echo "==> backup.enabled without destinationPath must fail fast"
if helm template t "$CHART" --set database.cnpg.backup.enabled=true >/dev/null 2>&1; then
  fail "template rendered despite missing destinationPath"
fi

echo "==> scheduled.enabled=false suppresses ScheduledBackup only"
out=$(helm template t "$CHART" -f "$CHART/values-backup-prod.yaml" \
  --set database.cnpg.backup.scheduled.enabled=false)
echo "$out" | grep -q "kind: ScheduledBackup" && fail "ScheduledBackup rendered when scheduled.enabled=false"
echo "$out" | grep -q "barmanObjectStore" || fail "WAL archiving suppressed by scheduled.enabled=false"

echo "==> endpointURL / serverName render when set"
out=$(helm template t "$CHART" -f "$CHART/values-backup-prod.yaml" \
  --set database.cnpg.backup.endpointURL=https://minio.example.com \
  --set database.cnpg.backup.serverName=preloop-db-v2)
echo "$out" | grep -q "endpointURL: https://minio.example.com" || fail "endpointURL not rendered"
echo "$out" | grep -q "serverName: preloop-db-v2" || fail "serverName not rendered"

echo "All helm render checks passed."
