# Code signing policy

Free code signing provided by [SignPath.io](https://about.signpath.io/),
certificate by [SignPath Foundation](https://signpath.org/).

## What is signed

Windows CLI release binaries published on
[GitHub Releases](https://github.com/preloop/preloop/releases):

- `preloop-windows-amd64.exe`
- `preloop-windows-arm64.exe`

These artifacts are built by GitHub Actions from this repository on version
tags (`v*`) and submitted to SignPath for Authenticode signing before they are
attached to the release. macOS and Linux CLI binaries are not Authenticode-signed.

See also [windows-code-signing.md](./windows-code-signing.md) for CI wiring and
maintainer setup.

## Team roles

Per [SignPath Foundation conditions for Open Source projects](https://signpath.org/terms.html):

| Role | Members |
|------|---------|
| **Authors** | [preloop organization members](https://github.com/orgs/preloop/people) trusted to modify source in this repository |
| **Reviewers** | [preloop organization members](https://github.com/orgs/preloop/people) who review pull requests |
| **Approvers** | [preloop organization owners](https://github.com/orgs/preloop/people?query=role%3Aowner) who approve SignPath signing requests |

## Privacy policy

CLI and self-hosted instance telemetry (optional, opt-out) is documented in
[SECURITY.md § Telemetry](../SECURITY.md#telemetry). Set
`PRELOOP_DISABLE_TELEMETRY=true` to disable it.

For Preloop Cloud / hosted services, see
[https://preloop.ai/privacy](https://preloop.ai/privacy).
