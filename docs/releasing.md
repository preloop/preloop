# Releasing Preloop

Releases have two phases: a reviewed preparation PR, then an immutable tag on
its merged main commit. Branch protection remains enabled. The tag push runs
`.github/workflows/release.yml`, which builds, signs and verifies the assets
before publishing the GitHub release.

From a clean checkout with `gh` authenticated and the development environment
active:

```sh
./scripts/release.sh 0.17.1 --yes --open-pr
```

The helper prefers a remote named `github`, then `origin`, and verifies it
points to `github.com/preloop/preloop`. Use `--remote NAME` to select explicitly;
a private GitLab mirror is never used for the public release.

This fetches main, creates `release/v0.17.1`, updates version metadata and the
changelog, runs pre-commit, commits and opens a PR. Without `--open-pr`, the
script leaves preparation local. Review the changelog and run release validation.
Merge the PR through the normal protected-branch process after checks and review
pass. Preparation never creates or pushes a tag.

Record the PR's **merge commit SHA**, then use a clean checkout of that exact
commit (including for squash merges):

```sh
git fetch github main  # use origin instead for a GitHub-only clone
git switch --detach MERGE_COMMIT_SHA
./scripts/release.sh 0.17.1 --finalize --target-commit MERGE_COMMIT_SHA --push-tag
```

Finalization makes no file changes. It requires a clean checkout, checks that
the chosen commit is reachable from the fetched release remote main branch and that all shipped version metadata
matches, and checks the signed-release workflow. It refuses to replace an
existing tag pointing elsewhere. Omitting `--target-commit` requires the current
fetched main tip. Omitting `--push-tag` creates only a local annotated tag.
A retry accepts an existing tag only when it points to the same commit.

The script follows the **tag** workflow and verifies required signed release
assets. If monitoring is interrupted, resume without editing files or tagging:

```sh
./scripts/release.sh 0.17.1 --monitor-release
```

Do not manually publish a GitHub release to work around a failed tag workflow.
Fix a release failure without moving an already published tag; use a new patch
version when released source must change. Enterprise Edition has a separate
private release process that pins the published OSS release.
