# OpenSSF Best Practices Badge: Readiness Checklist

Working notes for completing the questionnaire at
[bestpractices.dev](https://www.bestpractices.dev/) (passing level).
We do NOT claim the badge until the questionnaire is submitted and the site
grants it. This file maps each passing criterion to current repo evidence so
the questionnaire can be filled in quickly and honestly.

Status legend: MET (evidence exists), PARTIAL (some evidence, gap noted),
UNMET (honest gap, action listed).

## Basics

| Criterion | Status | Evidence / gap |
|---|---|---|
| Project website describes what the software does | MET | [README.md](../README.md), https://preloop.ai, https://docs.preloop.ai |
| Contribution process documented | MET | [CONTRIBUTING.md](../CONTRIBUTING.md) |
| Contribution requirements (style, tests) | MET | CONTRIBUTING.md (code style), [TESTING.md](../TESTING.md) |
| FLOSS license | MET | Apache-2.0, [LICENSE](../LICENSE), OSI approved |
| License file at standard location | MET | `LICENSE` at repo root |
| Basic documentation | MET | README, ARCHITECTURE.md, docs.preloop.ai |
| HTTPS project sites | MET | GitHub, preloop.ai, docs.preloop.ai all HTTPS |
| Discussion mechanism | MET | GitHub issues + Discord (linked from README) |
| English supported | MET | All docs and issues in English |

## Change control

| Criterion | Status | Evidence / gap |
|---|---|---|
| Public version-controlled source repo | MET | https://github.com/preloop/preloop |
| Interim versions available for review | MET | main branch, PRs public |
| Unique version numbering | MET | SemVer tags `vX.Y.Z`, [VERSION](../VERSION) file |
| Release notes per release | MET | [CHANGELOG.md](../CHANGELOG.md) (Keep a Changelog format, git-cliff) + GitHub release notes |
| Release notes identify fixed vulnerabilities | PARTIAL | Process exists via CHANGELOG; no CVEs published yet, so untested. Note in RELEASING.md when first one lands. |

## Reporting

| Criterion | Status | Evidence / gap |
|---|---|---|
| Bug reporting process | MET | GitHub issues |
| Bug tracker archive | MET | GitHub issues history |
| Vulnerability reporting process published | MET | [SECURITY.md](../SECURITY.md), security@preloop.ai |
| Private vulnerability reporting supported | MET | Email path in SECURITY.md; consider also enabling GitHub private vulnerability reporting in repo settings (small gap) |
| Initial response to vulnerability reports <= 14 days | PARTIAL | Committed to in SECURITY.md ("acknowledge receipt as soon as possible"); no reports received yet to demonstrate track record |

## Quality

| Criterion | Status | Evidence / gap |
|---|---|---|
| Working build system | MET | pyproject.toml, cli/Makefile, Dockerfile, CI builds all of it |
| Automated test suite | MET | pytest (backend), Web Test Runner (frontend), Go tests (CLI); run in [ci.yml](../.github/workflows/ci.yml) |
| New functionality includes tests (policy) | PARTIAL | Practiced and enforced via coverage gate (`--cov-fail-under=60`, Codecov); make the policy explicit in CONTRIBUTING.md (one-line addition) |
| Warning flags / linters enabled | MET | ruff + pre-commit in CI lint job |
| Test coverage measured | MET | Codecov upload in CI; target 75%, floor 60% (TESTING.md) |

## Security

| Criterion | Status | Evidence / gap |
|---|---|---|
| Developers know secure design basics | MET | Security-focused product (policy engine, approvals, audit); ARCHITECTURE.md documents trust boundaries |
| Crypto: published protocols only, no custom crypto | MET | Standard TLS, JWT, bcrypt via passlib (pyproject.toml); no homegrown crypto |
| Crypto: FLOSS implementations | MET | Python/Go standard ecosystem libraries |
| Secure delivery of releases | MET | HTTPS GitHub Releases; `SHA256SUMS` asset ships with every release; VirusTotal scan links appended to release notes for Windows binaries |
| Signed releases | PARTIAL | Checksums yes; Authenticode via SignPath is wired in [release.yml](../.github/workflows/release.yml) but PENDING SignPath approval/secrets ([docs/windows-code-signing.md](./windows-code-signing.md)). No GPG/Sigstore signing of tarballs yet. Action: enable SignPath, consider Sigstore cosign for archives. |
| No unpatched publicly known vulnerabilities (medium+) | MET | None known; dependency updates ongoing |
| Static analysis applied | PARTIAL | ruff covers Python; add a dedicated security scanner (bandit or CodeQL) and Go staticcheck/gosec. OpenSSF Scorecard action now runs weekly ([scorecard.yml](../.github/workflows/scorecard.yml)). |
| Dynamic analysis (suggested, not required) | UNMET | Not currently run; integration tests exercise real stack which partially covers this |

## Honest gaps summary (do these before or while filling the questionnaire)

1. SignPath Authenticode signing: pending approval; binaries currently publish
   unsigned with checksums. Track in docs/windows-code-signing.md.
2. No cryptographic signing of source archives (GPG/Sigstore). Optional for
   passing level but strengthens "signed releases".
3. Enable GitHub private vulnerability reporting to complement the email path.
4. Add one line to CONTRIBUTING.md making tests-for-new-functionality an
   explicit policy.
5. Add CodeQL or bandit + gosec for dedicated security static analysis.
6. No vulnerability-fix track record yet (no reports received); nothing to do,
   just answer honestly.

## How to claim the badge

1. Sign in at https://www.bestpractices.dev/ with the project GitHub account.
2. Register https://github.com/preloop/preloop.
3. Fill the passing questionnaire using the table above (most answers are Met).
4. Only after the site shows "passing" do we add the badge to README.md.
