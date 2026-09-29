# Upgrading Preloop

Preloop upgrades in place: the installer and the Helm chart both take care of
database backups and schema migrations for you. This page covers the
Docker-Compose (OSS) stack, Kubernetes/Helm deployments, and the CLI, plus how
to roll back if something goes wrong.

!!! tip "What's new?"
    See the [release notes on GitHub](https://github.com/preloop/preloop/releases)
    before upgrading for the changes in each version.

## Docker Compose (OSS install script)

Re-run the same command you installed with, it upgrades in place:

```bash
curl -fsSL https://preloop.ai/install/oss | sh
```

The installer detects the existing install in `~/.preloop-oss` and:

- **keeps your configuration**: public URL, TLS setup, SMTP credentials,
  `SECRET_KEY`, and the database password are preserved. Setting an
  environment variable when re-running overrides that one setting; everything
  else stays as it was.
- **backs up the database first**: a dump is written to
  `~/.preloop-oss/backups/` before anything changes.
- **pulls the new images** and restarts the stack.
- **applies schema migrations automatically**: the `migrate` service runs
  `alembic upgrade head` before the API starts.
- **removes containers for services a new version dropped.**

### Pinning a version

To upgrade (or stay) on a specific release, set `PRELOOP_VERSION`:

```bash
curl -fsSL https://preloop.ai/install/oss | PRELOOP_VERSION=0.12.2 sh
```

You can also edit `PRELOOP_VERSION` in `~/.preloop-oss/.env` and run
`docker compose up -d` again from `~/.preloop-oss`.

## Kubernetes (Helm)

Preloop's chart lives at
[`helm/preloop`](https://github.com/preloop/preloop/tree/main/helm/preloop) in
the main repository. Upgrade with `helm upgrade`, pointing at the image tag
you want:

```bash
helm upgrade preloop ./helm/preloop \
  --namespace preloop \
  --reuse-values \
  --set image.tag=<new-version>
```

Database migrations run automatically: the chart ships a `post-upgrade` Job
(`migration-job`) that executes `alembic upgrade head` against the configured
database after each `helm upgrade`, so no manual migration step is needed.

## CLI

Re-run the CLI installer to upgrade to the latest release:

```bash
curl -fsSL https://preloop.ai/install/cli | sh
```

The CLI checks for updates at most once per day and prints a notice when a
newer version is available. Check your installed version with
`preloop version`.

## Rolling back

If an upgrade misbehaves:

1. **Pin the previous version** and re-run the installer:

    ```bash
    PRELOOP_VERSION=<previous-version> curl -fsSL https://preloop.ai/install/oss | sh
    ```

2. **Restore the database dump** taken before the upgrade if the newer
   version's migrations are incompatible with the older code. Dumps live in
   `~/.preloop-oss/backups/`; restore the latest one into the `postgres`
   service, e.g.:

    ```bash
    cd ~/.preloop-oss
    docker compose exec -T postgres psql -U postgres -d preloop < backups/<dump-file>
    ```

For Helm deployments, `helm rollback` returns the workloads to the previous
release; restore a database backup if you need to undo migrations.

## Disabling telemetry (internal/test installs)

Self-hosted instances send an anonymous daily version check to preloop.ai
(instance id, version, edition) that also powers the update notice above. Set
`PRELOOP_DISABLE_TELEMETRY=true` on the server (add it to
`~/.preloop-oss/.env`, then `docker compose up -d`), and in the shell where you
run the CLI, to disable it entirely: opted-out installs never phone home and
never appear in instance tracking or adoption metrics. Update notices are
suppressed too, since they ride on the check-in response. This is the standing
rule for internal, CI, or throwaway test installs.

## See also

- [Install the OSS stack](installation.md)
- [Release notes / changelog](https://github.com/preloop/preloop/releases)
