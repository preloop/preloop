# OpenSSF Best Practices Badge: Readiness Guide

The [OpenSSF Best Practices badge](https://www.bestpractices.dev/) is a
maintainer self-assessment against published criteria. It is separate from
[OpenSSF Scorecard](https://scorecard.dev/), which scans repository practices.
A Scorecard result or a signed release does not grant the Best Practices badge.

This is a bounded evidence review dated **2026-09-10**, not a completed
questionnaire or a claim of certification. Use the
[official passing criteria and their details](https://www.bestpractices.dev/en/criteria/0?details=true&rationale=true)
as the authority. Criterion identifiers below match that questionnaire.

## How to obtain the badge

1. A maintainer signs in at [bestpractices.dev](https://www.bestpractices.dev/)
   with their own account. Search for an existing Preloop entry before creating
   one; if none exists, [register the project](https://www.bestpractices.dev/en/projects/new)
   with repository URL `https://github.com/preloop/preloop`.
2. Complete the passing questionnaire, attaching public evidence URLs and
   explanations. Review every question, including those not covered here.
   Use the site's allowed N/A answers only when their conditions hold.
3. Confirm the maintainer-only facts below and resolve applicable gaps. Save
   progress while evidence is incomplete; do not select Met to raise a score.
4. Once the site actually grants passing status, use the badge URL for the
   assigned project ID in README.md. Keep answers current as practices change.

This guide does not register the project, submit answers, or authorize new
security commitments. In particular, the support-period section of
[SECURITY.md](../SECURITY.md) is explicitly a draft and is not evidence of an
approved support commitment.

## Evidence already available

“Evidence available” means there is a useful source for an answer, not that
all related criteria have been independently verified.

| Passing criteria | Repository evidence | What to verify when answering |
| --- | --- | --- |
| `description_good`, `interact`, `documentation_basics`, `documentation_interface` | [README](../README.md), [documentation](https://docs.preloop.ai), [OpenAPI schema](../openapi.yaml), [CLI guide](../cli/README.md) | Check that current user-facing interfaces and installation instructions are covered. |
| `contribution`, `contribution_requirements`, `test_policy`, `tests_documented_added` | [CONTRIBUTING.md](../CONTRIBUTING.md) already says new features and bug fixes should include tests when practical, and documents style and PR submission. | No additional tests-for-features policy is needed just to fill these answers. |
| `floss_license`, `floss_license_osi`, `license_location` | Root [Apache-2.0 LICENSE](../LICENSE) | Assess the public OSS project; do not extend its license claim to separate proprietary software. |
| `repo_public`, `repo_track`, `repo_interim`, `repo_distributed`, `discussion`, `report_process`, `report_tracker`, `report_archive` | Public [repository](https://github.com/preloop/preloop), PRs and [issues](https://github.com/preloop/preloop/issues) | An issue tracker provides an archive, but does not prove response-time or response-rate criteria. |
| `version_unique`, `version_semver`, `version_tags`, `release_notes` | [Releases](https://github.com/preloop/preloop/releases), tags, [CHANGELOG](../CHANGELOG.md), [release instructions](../RELEASING.md) | Inspect actual release notes and version identifiers, not only the release scripts. |
| `build`, `build_common_tools`, `build_floss_tools`, `test`, `test_invocation`, `test_continuous_integration`, `warnings` | [CI workflow](../.github/workflows/ci.yml), [TESTING.md](../TESTING.md), pyproject.toml, CLI Makefile, frontend package.json | CI contains Python, browser, runtime-plugin and Go test jobs, linting and builds. Check relevant successful runs for the assessed revision. |
| `vulnerability_report_process`, `vulnerability_report_private` | [SECURITY.md reporting instructions](../SECURITY.md#reporting-a-vulnerability), [private reporting form](https://github.com/preloop/preloop/security/advisories/new) | GitHub API confirmed private vulnerability reporting enabled on 2026-09-10. The published email path also exists. |
| `static_analysis`, `static_analysis_common_vulnerabilities`, `static_analysis_often` | [CodeQL workflow](../.github/workflows/codeql.yml), [CodeQL runs](https://github.com/preloop/preloop/actions/workflows/codeql.yml), Ruff and Go vet in CI | CodeQL already runs on PRs, main pushes and a weekly schedule. PR language jobs are path-filtered; main/scheduled scans cover Go, Python, JS/TS and Actions. Verify analysis of proposed major releases before claiming `static_analysis`. |
| `crypto_password_storage` | [Password hashing implementation](../backend/preloop/api/auth/jwt.py) uses bcrypt with `gensalt()`. | This supports the password-storage answer; it does not establish every cryptographic criterion. |
| `delivery_mitm`, `delivery_unsigned` | HTTPS [GitHub Releases](https://github.com/preloop/preloop/releases), [release verification instructions](release-verification.md) | Inspect real downloads and verification paths. Passing delivery criteria permit HTTPS; Windows Authenticode is not a separate passing requirement. |

## Facts still requiring review or maintainer confirmation

| Passing criteria | Evidence or decision still needed |
| --- | --- |
| `know_secure_design`, `know_common_errors` | A primary developer must truthfully confirm knowledge of the secure-design principles and vulnerability classes listed in the official details, including mitigations. A security product or an architecture document cannot establish an individual's expertise. |
| `vulnerability_report_response` | Review private and public vulnerability reports from the last six months. Every initial response must have been within 14 days. If no reports were received, the official details allow N/A. The current “as soon as possible” wording is neither a 14-day commitment nor proof of actual response times. |
| `report_responses`, `enhancement_responses` | Check acknowledgement/response history over the stated 2-12 month window. Do not infer majority response rates from a few examples. |
| `tests_are_added`, `warnings_fixed`, `test_most` | Link tests added with the most recent major changes and evidence of warning handling. Coverage reports help, but a coverage percentage alone does not prove most branches and functionality are tested. `test_most` is suggested. |
| `release_notes_vulns` | Review actual releases for publicly known runtime vulnerabilities in the project itself that already had a CVE or similar identifier when released. Identify applicable fixes in release notes. The official criterion excludes dependency vulnerabilities and permits N/A if there have been no qualifying vulnerabilities; the maintainer must confirm that history. |
| `vulnerabilities_fixed_60_days`, `vulnerabilities_critical_fixed` | Confirm there are no unpatched medium-or-higher vulnerabilities publicly known for more than 60 days, and review critical-fix timeliness. Use a fresh scan, applicability analysis, advisory dates and the assessed release. An upgrade prepared locally does not establish a released fix. |
| `static_analysis_fixed`, `dynamic_analysis_fixed` | Review confirmed exploitable medium-or-higher findings and remediation timing. Successful scanner jobs alone do not prove these criteria. CodeQL's workflow currently sets `upload: false` because of the documented default-setup conflict; distinguish analysis execution from finding ingestion and follow-up. |
| `no_leaked_credentials` | [Secret scanning](../.github/workflows/secret-scan.yml) checks the working tree and history. A maintainer must confirm any previously exposed valid credentials were revoked and scan exceptions are justified. Do not publish credentials as evidence. |
| `crypto_published`, `crypto_call`, `crypto_floss`, `crypto_keylength`, `crypto_working`, `crypto_weaknesses`, `crypto_pfs`, `crypto_random` | Review default cryptographic settings, all relevant call sites and protocol compatibility paths against the exact criteria. Standard libraries are useful evidence, but dependency names do not prove safe parameters, randomness or absence of custom cryptography. |
| `dynamic_analysis`, `dynamic_analysis_unsafe`, `dynamic_analysis_enable_assertions` | Identify actual tools, scope, assertions and release runs. Dynamic analysis is suggested at passing level; ordinary integration tests do not automatically prove fuzzing or memory-safety analysis. Apply the memory-unsafe-language N/A rule only after checking the project's own code. |

The September 2026 dependency review found four actionable npm advisories in
nanoid and Hono. Confirm their repairs have landed and scan the release being
assessed. `GO-2026-5932` concerns `golang.org/x/crypto/openpgp`; the CLI uses
`scrypt`, and package-aware `govulncheck` plus platform dependency lists found
no imported OpenPGP package. Retain that applicability evidence instead of
calling every module-level alert an exploitable vulnerability, or claiming
that no vulnerabilities exist from one clean scan.

## Related improvements are separate evidence

[Release signing and provenance](release-verification.md) helps consumers
verify artifacts and supports Scorecard's Signed-Releases check. Keep it
working for future releases. [Windows Authenticode](windows-code-signing.md)
remains a separate setup task; it is not a prerequisite for starting the
passing questionnaire.

Repository rules were inspected on 2026-09-10: the active `main-pr` ruleset
requires one approving review, but contains no required CI-status rule.
That is review enforcement, not proof that tests must pass before a merge.
Recheck applicable rules and recent runs before making stronger claims.
Neither branch protection nor a higher Scorecard number automatically earns
the Best Practices badge.
