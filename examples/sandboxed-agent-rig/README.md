# Sandboxed agent rig

A Docker Compose rig that onboards, resumes and offboards Claude Code inside
containers whose configuration is read-only in the ways real sandboxes make it
read-only. It runs against a local Preloop stack and a mocked model provider,
so it needs no account, no subscription and no network access beyond pulling
images.

Guide for users: [Sandboxed and containerised agents](../../docs/guide/clients/sandboxed-agents.md).

## What it covers

| Variant | Settings file | `~/.preloop` | Claude login | Shows |
| --- | --- | --- | --- | --- |
| `writable` | writable volume | writable volume | done inside the container (credentials file) | the ordinary case: one review, one Enter, step lines, receipt, extension report, one real request; cancelling changes nothing; no terminal prints the plan and refuses |
| `ro-settings` | single file bind-mounted read-only from the host | writable volume | done inside the container (credentials file) | manual merge, resume that names what is missing, a trailing comma located by line, one real request, discovery report, offboard with printed removal |
| `ro-everything` | read-only bind mount, read-only root filesystem | not writable | `CLAUDE_CODE_OAUTH_TOKEN` in the environment | resume token, `--manual-output`, the environment-variable form with `--from-env` |
| `wrong-owner` | writable mount, file owned by another uid (0644) | writable volume | none (API key upstream) | `permission denied (owned by uid 0, running as uid 10001)` |
| `no-login` | read-only bind mount | writable volume | none | MCP-only onboarding, nothing to probe |
| `stale-login` | read-only bind mount | writable volume | a token the provider rejects | the rejected login is not reported as onboarded and its source is named; `--cancel` removes what onboarding created |

The container user is uid 10001 in every variant, which is neither root nor a
typical host user.

## Run it

```bash
./run.sh                 # all variants
./run.sh ro-settings     # one variant (writable, ro-settings, ro-everything, wrong-owner, no-login, stale-login)
KEEP=1 ./run.sh          # leave the stack up to look around
```

Requirements: Docker with Compose v2, `bash`, `python3`. The first run builds
the server image and the agent image (Claude Code plus the `preloop` CLI built
from this checkout), which takes a few minutes.

Each variant writes a transcript of exactly what a user sees to `out/<variant>.txt`:
the commands as typed, their output and exit status. Keys are redacted in the
transcripts, including the throwaway ones the local stack issues. The script
exits non-zero if an expected line is missing or a forbidden one (such as
`Onboarded` before the change is in place) appears.

Lines starting with `host$` are steps a user does on the host, outside the
sandbox. The script performs them for you by writing the file from outside the
container.

## Measuring onboarding

`cli/scripts/measure-onboarding.sh` uses the same stack to measure what a
person goes through: it runs `preloop agents onboard` in a pseudo-terminal 80
columns wide, answers each prompt with its default, and prints the number of
interactions, the rows printed before the apply decision, the total rows, the
longest line and the seconds from the first prompt to the gateway probe
result. It covers an ordinary writable home (the `agent-writable` container)
and `ro-settings`. Given thresholds it exits non-zero when onboarding asks
more questions or the review grows past them, so it can gate a CI job.

```bash
cli/scripts/measure-onboarding.sh
MAX_INTERACTIONS=writable=1,ro-settings=2 MAX_ROWS_BEFORE=18 cli/scripts/measure-onboarding.sh
```

The `writable` container installs the Agent Control plugin from the npm
registry, so that variant needs network access to it.

## What is real and what is mocked

Real: the Preloop API, gateway and database from this checkout; the `preloop`
CLI built from this checkout; Claude Code from npm; Docker's read-only bind
mounts, uid mismatch and read-only root filesystem.

Mocked: the model provider. `mock/mock_anthropic.py` answers the Messages API
(JSON and server-sent events) and rejects any credential containing the word
`stale` with the 401 a revoked login gets. The gateway reaches it because each
model's endpoint points at it; `MODEL_ENDPOINT_ALLOWED_CIDRS` allows the rig's
private network. Subscription logins in the rig are synthetic strings with a
far future expiry, so the server never tries to refresh them.

## A note on editing a bind-mounted file

A single file mounted into a container follows the file's inode. Editors and
tools that save by writing a new file and renaming it over the old one (`sed -i`,
many editors' safe-write mode, `mv`) leave the container looking at the old
content until it is restarted. Overwrite the file in place instead (`cp new
settings.json`, or `cat new > settings.json`), or mount the directory rather
than the file. The rig overwrites in place.
