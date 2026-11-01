# Universal Codex image

`Dockerfile` here builds `preloop-codex-universal`, the hosted Codex image
recipe. It extends `ghcr.io/openai/codex-universal` pinned by digest and adds
one apt layer: `perl`, `cpanm`, `perlver` (`Perl::MinimumVersion`),
`perlcritic`, `prove`, and `shellcheck`. Python, Java, Go, Rust, and
TypeScript come from the base image. The image sets no `WORKDIR`,
`ENTRYPOINT`, or `CMD`.

`.github/workflows/publish-universal-image.yml` builds linux/amd64 and
linux/arm64 and smokes amd64. A pull request and the weekly schedule do not
push. A merge to main that changes this recipe, the Perl fixtures it copies,
or the workflow publishes, as does `workflow_dispatch` on main and a `v*`
tag. Published tags are `sha-<12 hex chars of the commit>` and, on a version
tag, `vX.Y.Z`. The job summary prints
`ghcr.io/preloop/preloop-codex-universal@sha256:<digest>`. Publishing does
not automatically update the hosted default. The container package must be
public in GitHub package settings; after pushing and attesting the index,
the workflow verifies anonymous digest access and fails if access is unavailable.
The image also installs the Preloop CLI at `/opt/preloop/bin/preloop`
(pinned release, SHA-256 checked, with an offline `permission-hook --help`
gate). Hosted Codex uses it for the native command policy hook; on an image
without it, the run downloads the same pinned release at start and verifies
the checksum.
The image creates the unprivileged `preloop-agent` user (uid 10001) that
hosted Codex runs as. The CLI and `/opt/preloop` are root-owned, the CLI is
execute-only (0711), and `/root` is traverse-only so the base toolchains stay
usable but not writable. `check-agent-user.sh` runs at build time and from
`run-smoke.sh`: it fails if the agent user can write the CLI or its
directories, cannot run it, or cannot pass the language smoke (including the
Maven and Perl fixtures). The image `USER` stays root because the Codex
entrypoint sets up runtimes as root; the hosted launch drops to the agent user
before Codex starts.
`environments/universal/bin/preloop-preflight`
is installed at `/usr/local/bin/preloop-preflight`. It writes
`/tmp/preloop-preflight.json`. The schema is
`environments/universal/bin/preloop-preflight.schema.json`. The
opt-in Perl image under `environments/perl/` stays available for private
runners.

## Build

From the repository root:

```bash
docker build -f environments/universal/Dockerfile \
  -t preloop-codex-universal:local .
```

The `CODEX_BASE_IMAGE` default is the index digest recorded in the Dockerfile,
so a build with no build-arg is still digest-pinned. The build refuses a base
without `@sha256:`. Override it only with another digest:

```bash
docker build -f environments/universal/Dockerfile \
  --build-arg CODEX_BASE_IMAGE=ghcr.io/openai/codex-universal@sha256:<digest> \
  -t preloop-codex-universal:local .
```

The smoke runs during the build, offline, through `/opt/entrypoint.sh` with
the `CODEX_ENV_*` values hosted Codex sets (`environments/universal/codex-env.sh`,
the same defaults as `backend/preloop/agents/codex.py`). A missing tool fails
the build. Upstream setup selects those already installed runtimes, and that
selection is stored in the image because the setup script writes it.

A dependency-free Maven fixture is packaged once while the build still has
network access, into `/opt/preloop-universal/m2`. The smoke then runs
`mvn -o -q package` against that cache.

## Smoke

Both architecture builds run `preloop-preflight --help` offline after installing
the binary. The publish workflow also checks the final image for an executable
`preloop-preflight` and runs `--help` offline before its language smoke.
The guard comes from the checkout, so it also rejects older pinned images
whose embedded smoke did not require preflight. A missing or broken binary
fails the job before the image is pushed.

```bash
environments/universal/run-smoke.sh preloop-codex-universal:local
environments/universal/run-smoke.sh preloop-codex-universal:local \
  --negative-tool shellcheck
```

`run-smoke.sh` uses `docker run --network none` and `--entrypoint /opt/entrypoint.sh`.
The build bakes `/opt/preloop-environment.json` as root. A later `--user`
run reads that file and does not rewrite it, because `/opt` is not writable
by the runtime user.
The pinned upstream base keeps several toolchains under `/root`, so arbitrary
non-root users may still fail the required-tool check. Reading the manifest
does not make those toolchains accessible.
A successful run prints `PASS preloop-preflight`, followed by one `PASS` line
for each of Python, Java, Go, Rust, TypeScript, Perl, and Shell (8 lines total),
and exits 0. `--negative-tool` removes
one required command in a throwaway layer and passes only if the smoke then
exits non-zero and names that tool (`missing required tool: <name>`).

Perl reuses `environments/perl/perl-toolchain-smoke.sh`. The smoke also runs
`perlver --blame` on `compatible-5_10.pl` and requires it to print a minimum
version. Read that version, not `perlver`'s exit status, and do not call
`perlver --version` (1.40 rejects it).

## Evidence

Keep this output with the image:

| Field | Where it comes from |
| --- | --- |
| Image id and repo digests | `run-smoke.sh` `image id` and `repo digests` lines |
| Compressed size | `docker image inspect --format '{{.Size}}'` (`size bytes` line) |
| Base image | `org.opencontainers.image.base.name` (`base image` line) |
| Smoke log | the `run-smoke.sh` stdout, including eight `PASS` lines (preflight and seven languages) and `perlver --blame` |
| Tool manifest | `/opt/preloop-environment.json` inside the image |

`/opt/preloop-environment.json` has `version` 1 and `harness` `codex` (the
environment profile protocol) plus `base_image` and a `tools` object. Each
tool value is the version line captured at build time.

## Size

Measured on 2026-10-14, linux/arm64, against base
`ghcr.io/openai/codex-universal@sha256:905e512f36460e1be4cfedb30928a8a28299edb0fcd5de7998ceaa72d27fe304`.
`docker image inspect` `.Size` on this image is the compressed blob size
(the base reports 10386292023 bytes, 10.39 GB).

| Measure | Value |
| --- | --- |
| Base compressed size | 10386292023 bytes |
| Image compressed size | 10436478863 bytes |
| Compressed size increase | 50.19 MB |

`docker history` reports larger unpacked layer sizes (the apt layer is about
121 MB unpacked, the Maven cache about 21.5 MB, the smoke layer about
39.3 MB). The figure that matters for a pull is the compressed increase
below. The compressed size increase is under 300 MB.

Compressed size increase: 50.19 MB
