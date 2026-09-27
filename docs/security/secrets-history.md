# Disposition of historical secret-scan findings

The gitleaks history scan (`.github/workflows/secret-scan.yml`) walks the
full history of `main` on every push and PR. It reports clean because every
historical finding was triaged one by one into `.gitleaksignore`, each with a
note saying what it is. That file answers "what was found"; this page answers
the question a release audit asks next: **was the credential rotated, or was
it never live?**

Ground rules, same as the ignore file: findings are identified by commit SHA
and path only. No secret value, full or partial, appears here. History is not
rewritten (that would break every downstream clone), so these entries are
permanent.

The register below covers all 11 classified findings from the release audit's
independent history scan. The count matches the audit's freeze floor: no row
has been dropped, and any new finding must be added here as well as to
`.gitleaksignore`.

## Assume compromised: reported privately, rotation not recorded here

These four are high-entropy credential shapes committed while the repository
was private and published when it went public. They were reported privately
through the process in [SECURITY.md](../../SECURITY.md) when the first
gitleaks history scan surfaced them (scanning added 2026-09-08, #508) rather
than in a public issue. **This repository contains no record that they were
rotated.** The honest status is therefore: treat as compromised until the
credential owner confirms rotation; do not mark this class closed on the
basis of this page.

| Commit | Path | Shape |
| --- | --- | --- |
| `2219fbe7795f14f1fb11a5b5704b4f5465f39857` | `spacebridge/config.py` | payment-provider access token (2025-08-08) |
| `fb032be5def2256ea3e5982bc20164ff23e888bb` | `docs/index.md` | two API keys pasted into a docs example (2025-04-16) |
| `2d7e50b1c9194ab1c046aea392a9c83d487a3846` | `docs/index.md` | same two keys, second commit of the same day |

Rotation status: **not recorded in this repository.** The keys belong to
external services; confirming or performing rotation happens in those
services' dashboards, outside this repo. If rotation has been confirmed,
record the date here in place of this sentence.

## Superseded defaults: published by design, removed at HEAD

| Commit | Path | Disposition |
| --- | --- | --- |
| `8fdb45ddc204b2b7880af359b23f097c9de01b13` | `scripts/test_agent_api.py` | hardcoded fallback API token in a manual test script |
| `32227f41686e2f0e1fbeacbea64ec14c3b6d2d17` | `scripts/test_agent_api.py` | same fallback, later commit |
| `9924b0e3448437e64f70a806977cbb98d378a409` | `helm/spacebridge/values.yaml` | JWT signing-key default in the retired spacebridge chart |

The script fallback was removed in #508; the script now exits when the token
is unset. Whether the token was ever valid against a live deployment is not
recorded; treat it as the assume-compromised class if in doubt.

The chart default was never a per-deployment secret: it was published in the
chart for anyone to read, which is exactly why it is a finding. Its preloop
equivalent was first reduced to a documented placeholder (#508) and the chart
now refuses to install with an empty or placeholder signing key. Any
deployment that ever kept a published default must set its own key; that
action lives with the operator, not in this repository.

## Test fixtures and demo values: never live

| Commit | Path | Disposition |
| --- | --- | --- |
| `e71706cd13436b34ea22a3d4d71ac3da715d1114` | `lib/preloop-sync/test_search.py` | sample key in a test file, removed tree |
| `e71706cd13436b34ea22a3d4d71ac3da715d1114` | `lib/preloop-sync/test_api.py` | sample key in a test file, removed tree |
| `93c5b68b95da6d862a37e0e1354aa01a50e19b2e` | `lib/frontend/src/views/authed/issues-view.ts` | demo token in an early view, removed tree |

Synthetic values in fixtures and demos under the long-removed `lib/` tree.
Never credentials for any live system; nothing to rotate.

## Hardening commits: the finding is the fix, no committed value

| Commit | Path | Disposition |
| --- | --- | --- |
| `38434c56d98cbc2e37551a4966307922e0f5d762` | `backend/preloop/utils/git_credentials.py` | the fix that stopped embedding tracker tokens in git remote URLs (#173) |
| `4af45e9d1ddf5d0a8e3f0353871c5a2938fba404` | `helm/preloop/templates/api-deployment.yaml` | the fix that moved literal credentials out of pod specs |

Both commits are remediations; the scanner and the pickaxe match credential
terminology in the code and templates, not credential values. The repository
never contained the affected credentials: they are operator-supplied at
runtime (tracker tokens, database and SMTP credentials). Operators of
deployments that predate these fixes rotate on their side;
[docs/operations/database-credentials.md](../operations/database-credentials.md)
is the rotation guide for the second one.

## Keeping this page true

- A new history finding gets a `.gitleaksignore` entry (what it is) and a row
  here (what happened to it), in the same change.
- "Rotation status not recorded" is a valid entry. Writing "rotated" without
  a date and a person who confirmed it is not.
- The working tree stays clean without any of these entries; they exist only
  because history cannot be edited.
