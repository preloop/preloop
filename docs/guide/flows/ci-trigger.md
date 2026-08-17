# Trigger a flow from CI

GitHub Actions and GitLab CI call the Preloop CLI. Preloop does not dispatch
into your CI APIs. The CLI blocks (when stdin is not a TTY), streams
execution logs to the job, and exits non-zero on FAILED, STOPPED, or TIMEOUT.
The same logs stay visible in the Preloop console.

```yaml
# .github/workflows/preloop-flow.yml
name: Preloop flow
on:
  pull_request:
jobs:
  trigger:
    runs-on: ubuntu-latest
    steps:
      - name: Trigger Preloop flow
        env:
          PRELOOP_TOKEN: ${{ secrets.PRELOOP_TOKEN }}
          PRELOOP_URL: https://preloop.example.com
        run: |
          curl -fsSL https://preloop.ai/install/cli | sh
          preloop flow trigger pull-request-reviewer \
            --payload '{"pull_request":{"url":"https://github.com/example/repo/pull/1"}}'
```

`PRELOOP_TOKEN` is an account API token. OIDC exchange is not part of this
command yet. Use `--payload -` to pipe a JSON event file from a previous step.
Omit `--wait` in CI; waiting is the default when stdin is not a TTY.
