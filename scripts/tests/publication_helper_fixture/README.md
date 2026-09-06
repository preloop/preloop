# Local private publication lifecycle fixture

This test-only image runs the actual freeze/publish Python helper, local Git
bundle validation, ancestry checks, metadata, and credential revocation. A
`sitecustomize` fixture replaces only provider HTTP and Git network operations.
Git fetch establishes the local two-commit fixture's `HEAD~1` as the base ref;
local Git operations and the publisher's ancestry checks remain real. No real
provider credentials, model calls, pushes, or pull requests are used.

Never configure this image as an operational runner's publication helper.
The fake adapter deliberately reports provider writes as successful.

From the repository root with a local Docker engine:

```sh
docker build -f scripts/tests/publication_helper_fixture/Dockerfile \
  -t preloop-publication-helper-fixture:local .
docker image inspect preloop-publication-helper-fixture:local \
  --format '{{json .RepoDigests}}'
```

Use the returned immutable repository digest as
`PRELOOP_PRIVATE_PUBLICATION_FIXTURE_IMAGE` when running the opt-in CLI Docker
publication test. The test retains the production digest validator and exercises
real agent removal, bundle freeze, fresh verifier containers, private protocol,
publisher helper, acknowledgement, and completion. The test base image is pinned;
`--build-arg HELPER_BASE=...` can select an equivalent local Preloop runtime image
containing Python, Git, and backend dependencies.

From `cli/`, run the full lifecycle using that digest:

```sh
PRELOOP_DISABLE_TELEMETRY=true KUBECONFIG=/dev/null \
PRELOOP_PRIVATE_PUBLICATION_DOCKER_SMOKE=1 \
PRELOOP_PRIVATE_PUBLICATION_FIXTURE_IMAGE=<local-helper-repository-digest> \
go test ./internal/cmd -run '^TestPublicationDockerFullLifecycleSmoke$' -count=1 -v
```
