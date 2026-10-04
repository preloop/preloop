# OpenSSF Best Practices badge

Editions: OSS, Cloud, Enterprise. Unless stated otherwise, everything on this page ships in OSS.

This page is a maintainer checklist for the [OpenSSF Best Practices badge](https://www.bestpractices.dev/)
and for the Scorecard check that reports
`no effort to earn an OpenSSF best practices badge detected`.
It is not a completed questionnaire and it does not claim the badge.

The authority for answers is the
[passing criteria](https://www.bestpractices.dev/en/criteria/0).
A dated evidence review from 2026-09-10 is in
[OpenSSF Best Practices Badge: Readiness Guide](../OPENSSF_BEST_PRACTICES.md).
Where the two disagree, prefer this page and the live repository.

## How Scorecard detects the badge

Scorecard does not read `README.md`, `SECURITY.md`, or any other file in this
repository for this check. A badge image, a placeholder URL, or a made-up
project id in the README does not change the result.

The check is named `CII-Best-Practices` in
[Scorecard's check list](https://github.com/ossf/scorecard/blob/main/internal/checknames/checknames.go)
and [check documentation](https://github.com/ossf/scorecard/blob/main/docs/checks.md#cii-best-practices).
Dashboards often label the same check OpenSSF Best Practices. The documented
remediation is to sign up at bestpractices.dev.

Detection, from current `main` of
[ossf/scorecard](https://github.com/ossf/scorecard):

1. [`checks/raw/cii_best_practices.go`](https://github.com/ossf/scorecard/blob/main/checks/raw/cii_best_practices.go)
   calls `CIIClient.GetBadgeLevel` with the repository URI.
2. The default client in
   [`clients/cii_http_client.go`](https://github.com/ossf/scorecard/blob/main/clients/cii_http_client.go)
   sends `GET https://www.bestpractices.dev/projects.json?url=https://github.com/preloop/preloop`
   (the repo URI is prefixed with `https://`).
3. An empty JSON array is `NotFound`. The probe
   [`probes/hasOpenSSFBadge`](https://github.com/ossf/scorecard/blob/main/probes/hasOpenSSFBadge/impl.go)
   then returns a negative finding.
4. [`checks/evaluation/cii_best_practices.go`](https://github.com/ossf/scorecard/blob/main/checks/evaluation/cii_best_practices.go)
   turns that finding into score 0 and the text
   `no effort to earn an OpenSSF best practices badge detected`.

`badge_level` on the first JSON object sets the score:

| `badge_level` contains | Score | Result text |
| --- | --- | --- |
| (no project; empty array) | 0 | no effort to earn an OpenSSF best practices badge detected |
| `in_progress` | 2 | badge detected: InProgress |
| `passing` | 5 | badge detected: Passing |
| `silver` | 7 | badge detected: Silver |
| `gold` | 10 | badge detected: Gold |

`in_progress` is matched before `passing` (`strings.Contains` on
`badge_level`). Registering the project is enough to leave "no effort".
Passing, silver, and gold require the site to record those levels.

Checked on 2026-10-04:
`https://www.bestpractices.dev/projects.json?url=https://github.com/preloop/preloop`
returned `[]`. There is no project id to put in the README.

A separate blob client reads a precomputed `result.json` from a bucket. The
public Scorecard cron and the
[check docs](https://github.com/ossf/scorecard/blob/main/docs/checks.md#cii-best-practices)
use the bestpractices.dev API keyed by repository URL. Neither path searches
this git tree.

## Register the project

A maintainer account on bestpractices.dev is required. This repository cannot
enroll itself, and an agent must not invent a project id.

1. Sign in at [bestpractices.dev](https://www.bestpractices.dev/) with a
   maintainer account.
2. Search for an existing Preloop entry. If none exists, open
   [the new-project form with this repository URL filled in](https://www.bestpractices.dev/en/projects/new?url=https://github.com/preloop/preloop).
3. Submit the project. The site assigns a numeric id and starts the entry at
   `in_progress`. Saving partial answers is enough for that level. Do not mark
   a criterion Met to raise a percentage.
4. After the entry exists, confirm the API returns a non-empty array for
   `https://www.bestpractices.dev/projects.json?url=https://github.com/preloop/preloop`
   and that `badge_level` contains `in_progress`. The next Scorecard run can
   then report InProgress (score 2).
5. Fill the passing questionnaire using the map below. When the site records
   passing, the same API field becomes `passing` and Scorecard's score becomes 5.

## README badge, after a real id exists

Add this to `README.md` only after the site shows a numeric project id.
Replace `PROJECT_ID` with that id. Do not commit the placeholder, and do not
guess an id.

```markdown
[![OpenSSF Best Practices](https://www.bestpractices.dev/projects/PROJECT_ID/badge)](https://www.bestpractices.dev/projects/PROJECT_ID)
```

That markdown is for readers. Scorecard ignores it. Linking the badge from the
repository front page is a silver criterion (`documentation_achievements`),
which applies after the project has a real achievement to link.

## Passing criteria

Identifiers match the
[passing questionnaire](https://www.bestpractices.dev/en/criteria/0).
"Evidence" means a public URL a maintainer can paste. It does not mean the
criterion has been marked Met. Suggested criteria are included when the
repository already answers them; they are not required to pass.

### Evidence already in the repository

| Criteria | Evidence |
| --- | --- |
| `description_good`, `interact` | [README.md](https://github.com/preloop/preloop/blob/main/README.md), [docs/index.md](../index.md), [docs.preloop.ai](https://docs.preloop.ai) |
| `contribution`, `contribution_requirements` | [CONTRIBUTING.md](https://github.com/preloop/preloop/blob/main/CONTRIBUTING.md): fork, branch, pull request, review before merge, Ruff and frontend format checks |
| `floss_license`, `floss_license_osi`, `license_location` | Root [LICENSE](https://github.com/preloop/preloop/blob/main/LICENSE), Apache-2.0, OSI-approved. Answer for the public OSS tree. Do not extend the claim to proprietary clients. |
| `documentation_basics`, `documentation_interface` | Docs site, [CLI guide](https://github.com/preloop/preloop/blob/main/cli/README.md), [openapi.yaml](https://github.com/preloop/preloop/blob/main/openapi.yaml) |
| `sites_https` | `https://github.com/preloop/preloop`, `https://docs.preloop.ai`, `https://preloop.ai`, GitHub Releases |
| `discussion`, `english` | GitHub issues and pull requests; documentation and issue text are in English |
| `repo_public`, `repo_track`, `repo_interim`, `repo_distributed` | Public git history on GitHub, including pre-release commits |
| `version_unique`, `version_semver`, `version_tags` | Git tags and [GitHub Releases](https://github.com/preloop/preloop/releases). [RELEASING.md](https://github.com/preloop/preloop/blob/main/RELEASING.md) describes the version flow. |
| `release_notes` | [CHANGELOG.md](https://github.com/preloop/preloop/blob/main/CHANGELOG.md) and the GitHub release notes. Confirm the notes are a human summary, not `git log`. |
| `report_process`, `report_tracker`, `report_archive` | [GitHub issues](https://github.com/preloop/preloop/issues) |
| `vulnerability_report_process`, `vulnerability_report_private` | [SECURITY.md](https://github.com/preloop/preloop/blob/main/SECURITY.md) (`security@preloop.ai`) and [private vulnerability reporting](https://github.com/preloop/preloop/security/advisories/new) |
| `build`, `build_common_tools`, `build_floss_tools` | `pyproject.toml`, `cli` Makefile, `frontend/package.json`, [.github/workflows/ci.yml](https://github.com/preloop/preloop/blob/main/.github/workflows/ci.yml) |
| `test`, `test_invocation`, `test_continuous_integration` | [TESTING.md](https://github.com/preloop/preloop/blob/main/TESTING.md) (`pytest`, `npm run test`) and the CI workflow |
| `test_policy`, `tests_documented_added` | CONTRIBUTING.md: new features and bug fixes should include tests when practical |
| `warnings` | CI runs `ruff check` and `go vet ./...` |
| `crypto_password_storage` | [backend/preloop/api/auth/jwt.py](https://github.com/preloop/preloop/blob/main/backend/preloop/api/auth/jwt.py) hashes passwords with `bcrypt` and `gensalt()` |
| `delivery_mitm`, `delivery_unsigned` | HTTPS GitHub Releases. [Release verification](../release-verification.md) covers Sigstore provenance. Passing allows HTTPS. It does not require a separate signature. |
| `static_analysis`, `static_analysis_common_vulnerabilities`, `static_analysis_often` | [.github/workflows/codeql.yml](https://github.com/preloop/preloop/blob/main/.github/workflows/codeql.yml) runs on pull requests, pushes to `main`, and weekly. Pull-request language jobs are path-filtered. `main` and the weekly run cover Go, Python, JavaScript/TypeScript, and Actions. The workflow uploads SARIF (`upload: true`). Ruff and `go vet` also run in CI. |
| `dynamic_analysis` (suggested) | [.github/workflows/fuzz.yml](https://github.com/preloop/preloop/blob/main/.github/workflows/fuzz.yml) and [docs/fuzzing.md](../fuzzing.md): native Go fuzzers for two parsers, on relevant pull requests and nightly. This is not a whole-product dynamic scan. |
| `dynamic_analysis_unsafe` (suggested; N/A may apply) | No `cgo` or C/C++ sources were found in this tree. If the software produced by the project stays in Go, Python, and JavaScript, the memory-unsafe clause allows N/A. Confirm that before selecting it. |
| `no_leaked_credentials` | [.github/workflows/secret-scan.yml](https://github.com/preloop/preloop/blob/main/.github/workflows/secret-scan.yml) and [historical dispositions](secrets-history.md). The maintainer still confirms that any previously valid credential was revoked. Do not paste secrets into the form. |

`maintained` is a MUST. Activity on `main`, releases, and issue responses is
the evidence, and a maintainer has to affirm it.

### Gaps before marking Passing

These are the items that still need a human answer or a fresh check. Do not
mark them Met from this page alone.

| Criteria | Remaining work |
| --- | --- |
| `know_secure_design`, `know_common_errors` | A primary developer attests, on the form, to the secure-design principles and vulnerability classes named in the criterion details, including at least one mitigation for each. An architecture document does not answer this. |
| `vulnerability_report_response` | MUST: every vulnerability report in the last 6 months had an initial response within 14 days. [SECURITY.md](https://github.com/preloop/preloop/blob/main/SECURITY.md) says acknowledgement is "as soon as possible", which is not a 14-day record. If no reports arrived in that window, the criterion allows N/A. |
| `report_responses`, `enhancement_responses` | MUST / SHOULD: a majority of bug reports, and of enhancement requests, from the last 2–12 months were acknowledged. Count the tracker. A handful of answered issues is not a rate. |
| `tests_are_added` | MUST: show that tests were added with the most recent major changes, matching the policy in CONTRIBUTING.md. |
| `test_most` (suggested) | [TESTING.md](https://github.com/preloop/preloop/blob/main/TESTING.md) records a coverage target (CI fails under 60%, goal 75% overall). A percentage does not by itself prove most branches and functionality are tested. |
| `warnings_fixed`, `warnings_strict` (strict is suggested) | Show that enabled Ruff and `go vet` findings are addressed, not only that the jobs exist. |
| `release_notes_vulns` | For each release, identify every publicly known runtime vulnerability in the project itself that already had a CVE or similar identifier, and point at the release note. Dependency advisories do not count. N/A is allowed when there were no such vulnerabilities. The maintainer confirms that history. |
| `vulnerabilities_fixed_60_days`, `vulnerabilities_critical_fixed` | Confirm no unpatched medium-or-higher vulnerability has been publicly known for more than 60 days, on the release being assessed. A local upgrade or a clean scan of one module is not that confirmation. |
| `static_analysis_fixed`, `dynamic_analysis_fixed` | MUST: confirmed exploitable medium-or-higher findings from static or dynamic analysis were fixed in a timely way. A green CodeQL or fuzz job does not answer this. N/A applies only when no such finding was confirmed. |
| `crypto_published`, `crypto_call`, `crypto_floss`, `crypto_keylength`, `crypto_working`, `crypto_weaknesses`, `crypto_pfs`, `crypto_random` | Review default protocols, key lengths, and call sites against the criterion text. Password hashing above covers `crypto_password_storage` only. Standard-library imports are not a complete answer. |
| `dynamic_analysis_enable_assertions` (suggested) | State which fuzz or test configuration enables assertions, and that those assertions are not required in production builds. |

Silver and gold are separate questionnaires. They are not required to clear
"no effort" or to reach the passing score of 5. For later silver work, the
tree already has [CODE_OF_CONDUCT.md](https://github.com/preloop/preloop/blob/main/CODE_OF_CONDUCT.md)
and [docs/architecture/governance.md](../architecture/governance.md). A
Developer Certificate of Origin or other contributor assertion (`dco`) was
not found.

## What this page does not do

It does not register the project, submit answers, or add a badge URL to the
README. The support period in SECURITY.md is a signed commitment (release
manager, 2026-09-27) for users of Preloop. It is a separate statement from
the badge criteria.
