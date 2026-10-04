# Disposition of historical secret-scan findings

Editions: OSS, Cloud, Enterprise. Unless stated otherwise, everything on this page ships in OSS.

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

The register below covers all 11 classified findings (commit and path pairs)
from the release audit's independent history scan, matching the audit's
freeze floor: no row has been dropped. It also covers the three scanner
false-positive fingerprints that exist only in `.gitleaksignore`, so every
entry in that file has a disposition row here. One commit-and-path row below
can stand for several `.gitleaksignore` fingerprints (a fingerprint is
commit, path, rule and line). To keep the two files reconcilable, each
section below names the `.gitleaksignore` section it pairs with and that
section's fingerprint count, and every fingerprint's commit and path appear
as a row in the paired section here (14 fingerprints in total).
`backend/tests/test_secrets_history_parity.py` checks this pairing. Any new
finding must be added to both files in the same change.

## Assume compromised: reported privately, rotation not recorded here

Pairs with `.gitleaksignore` section: `assume compromised, rotate if not already done` (5 fingerprints).

Three credentials over five ignore-file fingerprints: a payment-provider
access token, and two API keys pasted into a docs example (the same two keys
appear in two consecutive commits of the same day, hence four fingerprints
for one incident). All are high-entropy credential shapes committed while
the repository was private and published when it went public. They were
reported privately through the process in [SECURITY.md](https://github.com/preloop/preloop/blob/main/SECURITY.md)
when the first gitleaks history scan surfaced them (scanning added
2026-09-08, #508) rather than in a public issue. **This repository contains
no record that they were rotated.** The honest status is therefore: treat as
compromised until the credential owner confirms rotation; do not mark this
class closed on the basis of this page.

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

Pairs with `.gitleaksignore` section: `superseded defaults, no longer in the tree` (3 fingerprints).

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

Pairs with `.gitleaksignore` section: `fixtures in trees that no longer exist` (3 fingerprints).

| Commit | Path | Disposition |
| --- | --- | --- |
| `e71706cd13436b34ea22a3d4d71ac3da715d1114` | `lib/preloop-sync/test_search.py` | sample key in a test file, removed tree |
| `e71706cd13436b34ea22a3d4d71ac3da715d1114` | `lib/preloop-sync/test_api.py` | sample key in a test file, removed tree |
| `93c5b68b95da6d862a37e0e1354aa01a50e19b2e` | `lib/frontend/src/views/authed/issues-view.ts` | demo token in an early view, removed tree |

Synthetic values in fixtures and demos under the long-removed `lib/` tree.
Never credentials for any live system; nothing to rotate.

## Hardening commits: the finding is the fix, no committed value

No `.gitleaksignore` counterpart (0 fingerprints): these two rows come from
the audit's history pickaxe, and gitleaks does not flag either commit.

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

## Scanner false positives: baselined so the history scan stays green

Pairs with `.gitleaksignore` section: `false positive on an unmerged feature branch` (3 fingerprints).

These three fingerprints exist only in `.gitleaksignore` (they are not among
the audit's 11 classified findings) and involve no credential at all:

| Commit | Path | Disposition |
| --- | --- | --- |
| `480bd991973f8a921d902b0dd3a401e6e11d3724` | `backend/preloop/services/record_signing.py` | a signing-key dataclass field annotated with a cryptography class name; a type name, not key material |
| `b8970d2a6f8d0354a26b271e65a052876861da2f` | `.gitleaksignore` | an earlier wording of the ignore file itself named the same field and tripped the same rule |
| `2895962e34089bf789a87fdf4870525cb4f59e8b` | `backend/preloop/models/crud/ci_principal.py` | adjacent Python call arguments interpreted as an API key; source syntax with no credential value, as detailed below |

Nothing was live, nothing rotates. They stay baselined because the history
scan walks every fetched commit and would otherwise fail every PR.

## CI identity source-code false positive

Commit `2895962e34089bf789a87fdf4870525cb4f59e8b` contains a CI principal
lookup with adjacent Python call arguments. The scanner interpreted the
attribute references as a generic API key. The finding is code syntax, not a
string literal or credential, and no token value exists there. The call was
reformatted; its single historical fingerprint is recorded in `.gitleaksignore`
without weakening the scanner policy. Nothing was live and nothing rotates.

## Keeping this page true

- A new history finding gets a `.gitleaksignore` entry (what it is) and a row
  here (what happened to it), in the same change.
- "Rotation status not recorded" is a valid entry. Writing "rotated" without
  a date and a person who confirmed it is not.
- The working tree stays clean without any of these entries; they exist only
  because history cannot be edited.
