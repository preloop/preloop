# Database credentials: where they live and how to rotate them

Editions: OSS, Cloud, Enterprise. Unless stated otherwise, everything on this page ships in OSS.

The chart used to render `DATABASE_URL` as a literal environment value in
every pod spec, built from `database.cnpg.auth.password`, whose default
value is the word `postgres`. Two consequences: the connection string was
readable by anyone who could read a Deployment (a wider audience than
`kubectl get secret` under most RBAC), and an install that never overrode
the default ran its superuser with a guessable password.

The chart now renders those credentials into a Secret and references them
with `secretKeyRef`. This page covers the rest: what to change on a running
deployment, and how to rotate what is already out there.

## Where a credential can live now

| Source | Values | Notes |
|---|---|---|
| Operator Secret (preferred) | `database.urlFromSecret.name` / `.key` | The URL never enters values or the chart's Secret. |
| Chart Secret | `database.cnpg.auth.*` or `database.externalDatabase.*` | The chart builds the URL and stores it in `<release>-credentials`, key `database-url`. |
| Pod spec literal | none | No longer rendered. |

The same applies to the SMTP password: `config.smtp.passwordSecret.name`
takes an operator Secret, `config.smtp.password` still works and is copied
into `<release>-credentials` under `smtp-password`.

Pods do not restart when a Secret changes, so the deployments carry a
`checksum/credentials` annotation. Changing a credential through values
rolls the pods the way a literal env value used to. The annotation hashes
only the chart-managed Secret: after rotating an operator Secret
(`database.urlFromSecret`, `config.smtp.passwordSecret`) the hash does not
move, so restart the deployments yourself:

```bash
kubectl -n <namespace> rollout restart deployment -l app.kubernetes.io/instance=<release>
```

## Rotating the CNPG superuser

Deployments that are still on the default password should treat it as
compromised: anything with network access to the database could have used
it. Rotate first, then reduce what the superuser is good for.

CNPG owns the password through the Secret named in `superuserSecret`. Write
a new one and let CNPG reconcile:

```bash
# 1. Generate and store a new password.
NEW_PW="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
kubectl -n <namespace> patch secret <release>-db-superuser \
  --type merge \
  -p "{\"stringData\":{\"password\":\"${NEW_PW}\"}}"

# 2. Wait for the operator to apply it.
kubectl -n <namespace> get cluster <release>-db -w

# 3. Confirm from inside the cluster pod.
kubectl -n <namespace> exec -it <release>-db-1 -- psql -U postgres -c 'select 1'
```

Keep the new value in the same place you keep the rest of the release
values (a sealed secret, a secret manager, not git). If the application
still builds its URL from `database.cnpg.auth.password`, set that value to
the new password in the same change, or the API pods will fail to connect
after the next upgrade rewrites the credentials Secret.

## Better: stop using the superuser for the application

The chart bootstraps CNPG with `bootstrap.initdb.owner: postgres` and
supplies the owner credentials itself (`<release>-db-user`), so the
application connects as the superuser and there may be no CNPG-generated
app Secret at all. Check before assuming one:

```bash
kubectl -n <namespace> get secret | grep -- '-db-'
# <release>-db-app exists only when CNPG generated the owner credentials.
```

If `<release>-db-app` exists, read it and use it. If it does not, create a
dedicated login role once, with a generated password:

```sql
CREATE ROLE preloop_app LOGIN PASSWORD '<generated>';
GRANT CONNECT ON DATABASE preloop TO preloop_app;
```

Either way, build a URL from those credentials and hand it to the chart as
an operator Secret:

```bash
kubectl -n <namespace> create secret generic preloop-db \
  --from-literal=database-url='postgresql://<user>:<password>@<release>-db-rw:5432/preloop'
```

```yaml
database:
  urlFromSecret:
    name: preloop-db
    key: database-url
```

The application user needs ownership of the schema it migrates. On a
deployment bootstrapped with `owner: postgres` (what this chart does),
grant that explicitly before switching, then run a migration to check:

```sql
GRANT ALL ON SCHEMA public TO <user>;
GRANT ALL ON ALL TABLES IN SCHEMA public TO <user>;
GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO <user>;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT ALL ON TABLES TO <user>;
```

## Then: turn the superuser password off

Once nothing connects as `postgres` over the network:

```yaml
database:
  cnpg:
    enableSuperuserAccess: false
```

CNPG then disables the password entirely. Operator access stays available
through `kubectl cnpg psql <cluster>` (or `kubectl exec` into the instance
pod), which authenticates as a local peer rather than over TCP. The chart
stops rendering the superuser Secret in that mode.

## Checking the result

```bash
# No credential should be visible in a pod spec.
kubectl -n <namespace> get deploy -o yaml | grep -c 'postgresql://'   # expect 0

# The reference should be a secretKeyRef.
kubectl -n <namespace> get deploy <release>-api \
  -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="DATABASE_URL")]}'

# Who can read the Secret.
kubectl -n <namespace> auth can-i get secret --as=system:serviceaccount:<namespace>:default
```

A NetworkPolicy is the other half of this: a rotated password does not help
if an untrusted pod can still reach port 5432 and try. See
`docs/security/agent-isolation.md`.
