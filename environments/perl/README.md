# Opt-in Perl toolchain image for Codex

`Dockerfile` here builds a Codex-compatible agent image that adds a distro
Perl toolchain: `perl`, `cpanm`, `perlver` (`Perl::MinimumVersion`),
`perlcritic` and `prove`. It extends a Codex-compatible base you choose,
pinned by digest at build time, and adds nothing else (no application,
PostgreSQL or browser layers). It is never a default: nothing changes until
an operator points an executor at it.

`environments/preloop/Dockerfile` is a separate project fixture image. Both
images run the same build gate, `perl-toolchain-smoke.sh`, with the
fixtures in `fixtures/`.

## What the check means

The contract is static Perl 5.10 syntax compatibility checking plus tests
run under the installed distro interpreter (Perl 5.38 on Ubuntu 24.04).
This is not execution on Perl 5.10, and it says nothing about whether your
dependencies support 5.10. If a project must be tested on a real 5.10
interpreter, that is a separate environment requirement. This image does
not downgrade Perl.

`Perl::MinimumVersion` 1.40 reads syntax and pragmas. It reports
`compatible-5_10.pl` (defined-or) as 5.010 and `postfix-dereference.pl` as
5.020 because of `use feature 'postderef'`. It does not flag a bare `->@*`
without that pragma (the default from Perl 5.24), so review prose should not
rely on it for that construct. Read the minimum version it reports, never
`perlver`'s exit status, and do not call `perlver --version` (1.40 rejects
it).

## Build and capture the digest

From the repository root:

```bash
BASE=ghcr.io/openai/codex-universal@sha256:<digest>
docker build -f environments/perl/Dockerfile \
  --build-arg CODEX_BASE_IMAGE="$BASE" \
  -t <registry>/preloop-codex-perl:<tag> .
docker push <registry>/preloop-codex-perl:<tag>
docker buildx imagetools inspect <registry>/preloop-codex-perl:<tag> \
  --format '{{.Name}}@{{.Manifest.Digest}}'
```

The build refuses a base without `@sha256:`. The smoke runs during the
build, so a missing tool fails the build. The image sets no `USER`,
`WORKDIR`, `ENTRYPOINT` or `CMD`: the base entrypoint stays, and the
executor chooses the user (the hosted Docker executor runs `10000:10000`;
Kubernetes runs root, or uid 1000 with `AGENT_RUN_AS_NON_ROOT=true`).

## Smoke as the agent user, offline

```bash
environments/perl/run-smoke.sh <image>                     # as 10000:10000
environments/perl/run-smoke.sh <image> --user 1000:1000    # Kubernetes non-root
environments/perl/run-smoke.sh <image> --negative-tool prove
```

`run-smoke.sh` uses `docker run --network none`. `--negative-tool` removes
one tool in a throwaway layer and passes only if the smoke then fails and
names that tool. Keep this output as the evidence:

| Field | Where it comes from |
| --- | --- |
| Effective image reference and digest | `image`, `image id`, `repo digests`, plus the pushed digest above |
| Base image | `base image` (the `org.opencontainers.image.base.name` label) |
| Executor type | hosted Docker, hosted Kubernetes, private runner, or native host |
| Agent user | `user:` line |
| Perl and tool versions | `perl:`, module, `cpanm:`, `perlcritic:`, `prove:` lines |
| Minimum-version results | `minimum version:` line |
| Command exit statuses | `smoke exit status`, `negative smoke ... exit status` |

## Selecting the image

Executors do not share one override. The precedence below is what the code
does today (`backend/preloop/agents/images.py`, `container.py`,
`remote_runner.py`) and is pinned by `backend/tests/agents/test_agent_images.py`.

**Hosted Codex (Docker or Kubernetes).** Set `CODEX_IMAGE` on the process
that launches agents (the API/worker deployment):

```bash
CODEX_IMAGE=<registry>/preloop-codex-perl@sha256:<digest>
```

Order: an approved `environment_profile` image, then `CODEX_IMAGE`, then
`ghcr.io/openai/codex-universal:latest`. A flow's `agent_config.image` or
`docker_image` is ignored by hosted executors. `CODEX_IMAGE` applies to
every hosted Codex flow on that deployment.

**Private Docker runner.** Set the image on the flow's agent config:

```json
{"image": "<registry>/preloop-codex-perl@sha256:<digest>"}
```

Order: `agent_config.image`, then the legacy `agent_config.docker_image`,
then the server's `CODEX_IMAGE`, then the shipped default. The runner host
must be able to pull the image.

**Native host profiles (`host_exec_profile`, for example Copilot or
Cursor).** These run on the runner host, not in a container, so no image
applies and any image key is dropped from the lease. Install the same tools
on the host and run the same smoke there:

```bash
sudo apt-get install -y --no-install-recommends perl cpanminus \
  libperl-minimumversion-perl libperl-critic-perl \
  libtest-harness-perl libtest-simple-perl          # Debian/Ubuntu
cpanm Perl::MinimumVersion Perl::Critic             # other hosts
environments/perl/perl-toolchain-smoke.sh environments/perl/fixtures
```

## Out of scope

Choosing the deployed digest or host, running the smoke there and keeping
the receipts, and running compatible and incompatible review examples with
the configured model and saved flow belong to release operations. Fixtures
and image builds here do not satisfy that acceptance.
