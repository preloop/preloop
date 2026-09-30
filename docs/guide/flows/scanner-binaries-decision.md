---
status: non-normative
---

# Scanner binaries in the agent image: decision note

> **Status: findings / design note. Not shipped behaviour.** This page records observations or a proposed design. Nothing here is a product capability unless a linked release note says so.

Editions: OSS, Cloud, Enterprise. Unless stated otherwise, everything on this page ships in OSS.

Status: recommendation, measured 2026-09-15. Implementation is tracked
separately; nothing in this note changes a preset or an image.

The security presets verify an SBOM that a build already produced
(`backend/presets/004-sbom-verify.yaml`, `backend/presets/005-sbom-exploit-check.yaml`).
The default agent image ships no scanner binary
(`backend/preloop/agents/images.py`, `backend/preloop/agents/codex.py`),
so a repository that arrives without an SBOM is reported as skipped.
That is the honest result, and it is also the weakest answer we can give
a project that inherited a codebase with no build-side SBOM.

This note measures what it would cost to change that, and recommends one
option.

## Recommendation in one paragraph

Ship one scanner (Trivy) with its advisory database baked in, as a
derived, digest-pinned image registered in the operator-owned
environment profile registry, and not as a change to the default agent
image. Measured cost: 163.41 MB of additional pull bytes per node
(+1.57 percent over the 10.39 GB baseline), 4.4 s of additional pull
time at the throughput measured here, and no measurable container start
delta. What that buys, in the words we would use to a partner, is in
[What we could then say](#what-we-could-then-say).

## How the numbers were produced

One host, so treat the absolute seconds as this host and this link, and
the byte counts and ratios as portable.

- Apple M4 Max, macOS 26.6.2, Docker 28.0.1, `linux/arm64` daemon with
  the containerd image store, consumer broadband.
- Image under test:
  `ghcr.io/openai/codex-universal@sha256:905e512f36460e1be4cfedb30928a8a28299edb0fcd5de7998ceaa72d27fe304`
  (index), `arm64` manifest `sha256:243822ac2ff9...`, `amd64` manifest
  `sha256:1641c7bc30b0...`.
- Byte sizes come from `docker image inspect --format '{{.Size}}'`,
  which on the containerd store reports the sum of the compressed
  layers, that is, what a node downloads. Unpacked size comes from
  `docker history`.
- Candidate images are `FROM` the same base plus a `COPY` of the
  binaries, so every delta below is one added layer and nothing else.
- Container start is `docker run --rm --entrypoint /bin/bash <image> -lc
  'echo ready'`, five runs, median reported, timed with a small Python
  wrapper around `subprocess.run`.
- Scans ran against a checkout of this repository as a stand-in for a
  polyglot project with no SBOM.

`arm64` binaries were used for the runnable measurements because the
host is `arm64`. The `amd64` artifact sizes are listed next to them
because hosted execution runs `amd64`.

## Baseline: the default agent image

| Measure | Value | Command |
| --- | --- | --- |
| Pull bytes, `arm64` | 10,386,292,023 B (10.39 GB), 24 layers | `docker image inspect ghcr.io/openai/codex-universal:latest --format '{{.Size}}'` |
| Pull bytes, `amd64` | 10,918,599,235 B (10.92 GB), 24 layers | `docker manifest inspect <amd64 digest>` then sum `.layers[].size` |
| Unpacked, `arm64` | about 32.06 GB | `docker history <image>` summed |
| Cold pull, `amd64` | 295.98 s, that is 36.9 MB/s | `time docker pull --platform linux/amd64 ghcr.io/openai/codex-universal:latest` |
| Container start, warm image | 0.354 s median (0.342 to 0.425 over 5 runs) | `docker run --rm --entrypoint /bin/bash <image> -lc 'echo ready'` |

The baseline is a 10 GB image. That is the frame for every number below:
a scanner binary is a rounding error against it, and an advisory
database is not.

The 36.9 MB/s figure is the conversion used throughout this note to turn
added bytes into added seconds. It is one measurement on one link; a
hosted node in the same region as the registry will be faster.

## Candidates

All four candidate binaries are Apache License 2.0. Licence text ships
inside each release archive (`LICENSE` in the tarball) and the repository
metadata agrees (`gh api repos/<owner>/<repo>/license -q '.license.spdx_id'`
returns `Apache-2.0` for `aquasecurity/trivy`, `anchore/syft`,
`anchore/grype`, `google/osv-scanner`).

| Candidate | Version | Binary bytes `arm64` / `amd64` | Image delta (pull bytes) | Delta vs baseline | Added pull time at 36.9 MB/s | Start median | Licence |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Baseline | n/a | n/a | 0 | 0 | 0 s | 0.380 s | n/a |
| Trivy | 0.74.0 | 156,106,914 / 168,456,354 | 45,602,731 B (45.60 MB) | +0.44 % | 1.2 s | 0.397 s | Apache-2.0 |
| Syft + Grype | 1.51.1 / 0.118.0 | 80,937,122 + 83,820,706 / 87,220,386 + 90,443,938 | 54,586,986 B (54.59 MB) | +0.53 % | 1.5 s | 0.397 s | Apache-2.0 (both) |
| OSV-Scanner | 2.6.0 | 53,608,608 / 57,524,384 | 19,443,060 B (19.44 MB) | +0.19 % | 0.5 s | 0.436 s | Apache-2.0 |
| Trivy + baked database | 0.74.0 | as above | 163,409,424 B (163.41 MB) | +1.57 % | 4.4 s | 0.361 s | Apache-2.0 |
| Syft + Grype + baked database | as above | as above | 464,585,967 B (464.59 MB) | +4.47 % | 12.6 s | 0.378 s | Apache-2.0 |

The start medians above were taken in one batch (baseline 0.380 s) and
span 0.34 s to 0.44 s across every image including the baseline. There
is no measurable start cost to adding a binary or a database to a 10 GB
image: the cost is entirely in bytes pulled once per node per image
version.

What each candidate actually does, measured on this repository:

| Run | Result | Wall time |
| --- | --- | --- |
| `syft scan dir:/repo -o cyclonedx-json` | 397 components | 11.19 s first run, 2.51 s on a warm page cache (with the Grype match in the same container) |
| `grype sbom:/out/sbom.cdx.json -o json` | 3 matches, database identity in the result | 3.76 s |
| `trivy fs --scanners vuln --format json` | 8 result sets, 1 vulnerability | 0.86 s, 0.96 s with `--network none` |
| `trivy fs --format cyclonedx` | 315 components | 0.98 s |
| `osv-scanner scan source --offline-vulnerabilities --download-offline-databases` | 12 vulnerabilities on a two-line `requirements.txt`, 34.33 MB PyPI database fetched | 4.06 s including the download |

Syft catalogues more than Trivy on the same tree (397 vs 315 components)
because it runs more catalogers, including binary ones. Both write
CycloneDX. Both are an inventory of what the source declares, which is
not the same artifact as an SBOM emitted by the build.

## Advisory databases

The binary is cheap. The database is the decision.

| Database | Download size | Unpacked | Download time | Command |
| --- | --- | --- | --- | --- |
| `ghcr.io/aquasecurity/trivy-db:2` | 118,796,668 B (113.29 MiB) | 1.3 GB | 9.7 s by tag, 11.0 s by digest | `trivy --cache-dir /cache image --download-db-only` |
| Grype v6 vulnerability database | 156,415,792 B (`.tar.zst`) | 2.1 GB | 54.4 s update, 66.7 s offline import | `grype db update`, `grype db import <archive>` |
| OSV offline database, PyPI ecosystem | 34,325,875 B (`all.zip`) | read as a zip, not unpacked | 4.06 s including the scan | `osv-scanner scan source --offline-vulnerabilities --download-offline-databases` |

Downloading the database inside the run costs 10 s (Trivy) to 67 s
(Grype import) of every execution, and makes the result depend on
whatever the vendor published that minute. Baking it into the image
costs the pull bytes in the table above, once per node per image
version, and makes the result reproducible. Bake it.

The consequence of baking is that the image has an expiry date. A daily
rebuild re-pushes only the scanner layer, so a node that already has the
base pays 163.41 MB (4.4 s here) per rebuild it picks up, not 10.39 GB.

## Pinning and recording, concretely

Two runs a month apart have to be explainable. That needs the tool
version and the database snapshot pinned at build time and recorded in
the result.

Pinning, verified on this host:

- Trivy resolves the database by OCI digest:
  `trivy --db-repository ghcr.io/aquasecurity/trivy-db@sha256:bdcb45d84e4f72ca3b4055215aee889b1018c1ae7151a7198f02c6d3c7835c3c image --download-db-only`
  downloaded 113.29 MiB in 11.0 s. At run time `--skip-db-update` and
  `--skip-java-db-update` keep the baked snapshot, and the scan then
  succeeds with `--network none` (0.96 s, same findings as the networked
  run).
- Grype takes a pre-downloaded archive:
  `grype db import <archive>` with `GRYPE_DB_AUTO_UPDATE=false` imported
  the pinned `.tar.zst` in 66.7 s at build time, and
  `grype db status` then reports `From: manual import`, schema `v6.1.9`,
  built `2026-09-15T06:31:36Z`.
- OSV-Scanner reads `OSV_SCANNER_LOCAL_DB_CACHE_DIRECTORY` with
  `--offline-vulnerabilities`, but the per-ecosystem `all.zip` carries no
  published version or checksum, so pinning means mirroring the zip and
  recording our own digest.

Recording, using fields the result envelope already has
(`tool_versions` and, for the vulnerability schema, `db_versions`, see
`backend/preloop/cra/schemas.py` and
`backend/presets/005-sbom-exploit-check.yaml`):

1. The image build writes `/opt/preloop-scanners.json`, for example
   `{"trivy": {"version": "0.74.0", "db_repository": "ghcr.io/aquasecurity/trivy-db@sha256:<digest>", "db_updated_at": "2026-09-15T13:41:13Z", "baked_at": "<build time>"}}`.
   The digest is the one the build resolved, not a tag.
2. The preset prompt reads that file and copies `version` into
   `tool_versions` and the database fields into `db_versions`. Both keys
   are free-form objects today, so no schema change is needed.
3. The prompt treats a stale snapshot as a finding rather than a silent
   pass: if `db_updated_at` is older than the agreed window, the check
   records the age and does not claim a clean result.
4. Grype, if it were chosen instead, already writes
   `descriptor.db.status` with the built timestamp and the source
   archive checksum into its own JSON, which is the one thing it does
   better than Trivy here. Trivy needs the extra file above, or a call to
   `trivy version --format json`, which prints
   `VulnerabilityDB.UpdatedAt`, `NextUpdate` and `DownloadedAt`.

## The alternative: let a flow declare its own image

Today, on hosted execution, a flow cannot name an arbitrary image.

- The image comes from the per-agent-type default, overridable only by
  the operator through environment variables such as `CODEX_IMAGE`
  (`backend/preloop/agents/images.py`).
- The one flow-selectable override is an environment profile: the flow
  names a profile, and the name is looked up in a registry file the
  operator controls (`flow_environment_profiles_file`,
  `backend/preloop/config.py`). The profile's `image` must match
  `^[^\s]+@sha256:[a-f0-9]{64}$`, so a registry entry is a digest and
  cannot drift (`backend/preloop/services/flow_environment.py`). An
  unknown name is rejected with `environment_profile_not_approved`.
- A private runner does pass an `agent_config` image through
  (`backend/preloop/agents/remote_runner.py`), but that container runs on
  the customer's own infrastructure, so the trust boundary is theirs.
- Per-flow custom commands exist and are refused to anyone who is not a
  superuser (`backend/preloop/models/models/flow.py`,
  enforced in `backend/preloop/api/endpoints/flows.py`). It is a dogfood
  lever, not a product answer.

So "bring your own image" is not an option a partner can use today, and
opening it would mean unreviewed code running on shared infrastructure
holding the run's credentials, with egress through our gateway. What
stops a flow from running an image nobody reviewed is exactly the
registry: the operator adds a digest, the flow can only select a name.
Evidence is the second reason to keep it that way. A verdict is only
worth what the tool that produced it is worth; if the image is unknown,
`tool_versions` is self-reported by an unknown binary and the evidence
pack means nothing.

The useful half of the alternative is to use that registry instead of
changing the default image: publish a scanner image derived from the
default agent image, pin it by digest, register it as a profile, and let
the security presets select it. Runs that do not need a scanner keep the
image they have today. Two constraints come with it, both in the code
above: a profile is refused on a private runner
(`environment_protocol_unsupported_private_runner`), and a profile image
must carry `/opt/preloop-environment.json` for the setup protocol check,
so the derived image has to add that file.

## Recommendation, and its cost

Take it, in this shape:

1. Build a derived image `FROM` the current default agent image that
   adds the Trivy binary and its advisory database, resolved to a digest
   at build time, plus `/opt/preloop-scanners.json` and
   `/opt/preloop-environment.json`.
2. Publish it by digest and register it as an environment profile. Do
   not change `DEFAULT_AGENT_IMAGES`.
3. Rebuild daily so the baked snapshot stays fresh; the rebuild re-pushes
   one layer.

Cost, in the same units as the baseline: +163,409,424 B (163.41 MB) of
pull per node per image version against a 10,386,292,023 B baseline,
which is +1.57 percent and 4.4 s at the 36.9 MB/s measured here; +1.3 GB
unpacked on the node; container start unchanged within noise (0.361 s
median against a 0.380 s baseline); and one daily build job to own.

Trivy over Syft plus Grype because it is one binary and one database
instead of two, it costs 163.41 MB instead of 464.59 MB (+1.57 percent
instead of +4.47 percent), it pins the database by content digest, and
it both catalogues and matches. The price paid for that choice is a
coarser inventory (315 components against Syft's 397 on the same tree)
and having to write the database identity into the result ourselves
instead of getting it for free in the tool's own JSON. OSV-Scanner is
the cheapest binary at 19.44 MB and matches the OSV-first stance of the
exploit-check preset, but it does not produce an SBOM and its offline
database has no published snapshot identity, so it cannot answer the
reproducibility question that the evidence pack exists to answer.

## What we could then say

Today: "we verify the SBOM and the advisories you already have. If your
build does not emit an SBOM, that check is skipped and the result says
so."

Under this recommendation: "if your build emits an SBOM, we verify it.
If it does not, the run can produce a source inventory with a pinned
scanner version and match it against a vulnerability database snapshot
pinned by digest, and the result names the tool version, the database
digest and the date that snapshot was built, so a run today and a run
next month are explainable against each other. That inventory is what the
repository declares, not a build attestation, and it does not replace
the SBOM your build toolchain should produce."

The disclaimer on every result does not change: machine-generated
evidence for conformity assessment support, not a conformity assessment,
certification, or legal advice.

## Follow-up

One implementation issue, sized for a single pull request: #705,
build and publish the digest-pinned scanner image and register it as an
environment profile, with the build recording
`/opt/preloop-scanners.json`. The preset prompt changes that read that
file into `tool_versions` and `db_versions`, and the staleness rule, are
a second step and depend on the image existing.

If the recommendation is not taken, nothing changes: the presets keep
refusing to generate an SBOM, a repository without one is reported as
skipped, and that skip stays the correct result.
