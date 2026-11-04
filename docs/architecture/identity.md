# Identity, Membership and SSO

Canonical person identities, account memberships, users, teams and invitations, edition overlays, enterprise sign-on persistence, session authentication assurance and membership offboarding.

## Users, teams and edition overlays

Preloop (OSS) owns user, invitation and team CRUD in
`preloop.api.endpoints.users`, `invitations` and `teams`. Models, schemas and
CRUD remain in `preloop.models`. Core registers these routers once, including
the unauthenticated invitation inspection/acceptance routes. Blocking CRUD
handlers use FastAPI's synchronous thread-pool dispatch. User and team
management capability flags are present without commercial plugins, including
with `DISABLE_PROPRIETARY_PLUGINS=true`.

Core applies no access control between users of the same account: there are
no roles, no role assignment and no owner protection, and every active member
can do everything in the account. Without the RBAC plugin
`preloop.utils.permissions.members_have_full_access()` is true, so
`user_holds_permission` grants every permission, `ensure_permission_in_oss`
and the kill-switch toggle admit every member, and `/auth/users/me` reports a
null permission list. Explicitly disabling commercial plugins uses the same
boolean flag parser for loading and authorization. Missing optional RBAC packages
mean core; broken installed RBAC imports refuse startup, and failed commercial
profile permission lookups return an empty list rather than unrestricted access.
Endpoints keep their `@require_permission` decorators;
they are no-ops in core and enforced by the RBAC plugin. Tenant isolation is
not an access control between users and stays: controllers validate account
ownership before reading or mutating users, teams or invitations. Invitations
that request roles answer 402 `upgrade_required` in core, and roles stored on
an invitation by a commercial edition are not granted on acceptance. Bulk hard
deletion still skips the primary user of an account with other members,
because an account needs a primary user.

The role catalog route (`GET /api/v1/roles`) is registered only by the commercial
RBAC plugin. Core user/team responses retain the `roles`/`inherited_roles` fields
for API compatibility but return empty lists without its access-control service;
legacy invitation metadata likewise does not advertise commercial role grants.
Team membership labels remain descriptive and grant no account privileges.

`preloop.services.multiuser` resolves optional services through PluginManager:
`multiuser_seat_gate`, `multiuser_billing_service`, `multiuser_membership` and
`multiuser_access_control`. The last implements the `AccessControl` protocol
(`validate_invitation_roles`, `apply_invitation_roles`,
`ensure_can_remove_member`). Moved controllers import no proprietary packages.
The RBAC and account hierarchy plugins in Preloop Cloud and Preloop Enterprise
supply seat policies, subscription housekeeping, canonical membership linking
and all access control: roles and role assignment (`/users/{id}/roles`,
`/teams/{id}/roles`), owner protection, RBAC/ABAC and permission enforcement
on the core endpoints. They register only their additional routes, never a
second copy of a core route. Invitation context includes the invitee email so
the overlay can recognize an existing seat across its account tree.

Preloop Cloud and Preloop Enterprise also retain resource rules and sharing,
team budgets, subaccount hierarchy, pooled billing, SSO/SAML/SCIM, fleet
findings, multi-approver/escalation workflows, weekly governance review
and advanced reporting. The audit hash chain, retention, legal holds and
signed exports are in core. The console shows no role
controls in core; role actions open the upgrade affordance. Per-user
devices/runners/agents, basic approvals and basic usage views remain in core.

## Canonical person identities

`Person` owns the chosen username, address and verification, shared name/avatar,
password, global suspension status and identity security generation. Existing
`User.id` values remain account membership IDs. Their account, role, team, API
key and historical actor bindings remain unchanged. Membership `is_active`
revokes one account's access; `Person.identity_status = suspended` suspends the
human globally. There is no transient User proxy and no account ID mutation.

Core `crud_person` resolves a membership's explicit Person link, returns a typed
`IdentityProfile`, updates canonical profile fields and resolves credentials.
`PersonAuthIdentity` owns login subjects in a `(source, provider, subject)`
namespace: provider must include issuer/tenant where subjects are not global.
The legacy schema did not capture LDAP/AD/SAML issuer; their migrated provider
namespace is `legacy-account:<account_id>`, pending an explicitly proven relink.
New and migrated OAuth login links use source `oauth_<provider>` with the
stored provider label and `oauth_id` (falling back to legacy `external_id`).
Initial identity creation registers that link atomically and preserves the
originating membership; explicitly linked memberships cannot claim new subjects.
OAuth integration grants remain account scoped and retain their account cascade;
they do not constitute the global login identity.

Passkey IDs, keys and counters stay unchanged. Their `person_id` owns access;
nullable `user_id` records originating membership provenance. Current legacy
passwords from already explicitly linked memberships are retained under the
same Person in `PersonPasswordCredential`; a nonprimary password differing from
the validated primary, a nonprimary passkey, or a differing nonprimary provider
subject quarantines the identity. These credentials previously authorized only
a membership, so granting them global identity access requires fresh ownership
proof. Preservation prevents data loss during operator repair and never recreates
a credential previously removed by consolidation. New explicitly linked
memberships cannot introduce identity credentials. A canonical password reset retires all retained hashes
and clears membership password caches. Security changes increment both identity
and membership token generations to support the existing JWT generation checks.
They also revoke opaque OAuth access/refresh tokens and CLI sessions across
linked memberships and discard pending OAuth authorization codes. Account API
keys retain their account-scoped semantics.
Deleting a membership, its account, the former primary membership, or the last
membership retains Person and login credentials. Explicit `delete_identity`
requires zero remaining memberships and destroys the identity-owned credentials.
A retained verified identity continues claiming its verified address.

The identity expand/backfill revisions are staged and additive. Drain credential
writers for both revisions, then deploy canonical authentication and profile
consumers together before resuming writes. A valid primary membership supplies
its chosen username verbatim; a missing/stale pointer falls back deterministically
by most recent login, creation time and ID. No suffix stripping or email merge
occurs. Conflicting linked addresses or provider claims quarantine the identities
with non-secret reasons; operators must establish ownership before repairing
canonical values. Unrelated provisional identities retain separate Person IDs.
An authorized administrator can then call `crud_person.repair_conflict` with a
fresh canonical profile/password, a non-secret ownership-evidence reference and
explicit acceptance of credential retirement. The operation clears quarantine,
stores the reference in `migration_resolution`, retires all disputed passwords,
passkeys and provider links, clears verification and revokes every linked session.
All other credential types require fresh enrollment. Ordinary status/profile/reset
operations cannot lift quarantine. The backfill precomputes username ownership
and writes profiles and credentials in batches of at most 1,000 records; rehearse
its write-blocking lock duration on a production-sized copy before rollout.

`python -m preloop.models.identity_audit` reports rollout counts and conflict
reasons without passwords, provider subjects or addresses. The CLI starts a
read-only database transaction and never initializes extensions. A SELECT only
database role adds operational enforcement. The old User profile columns
are temporary compatibility caches updated only by canonical operations; unique
membership usernames remain legacy aliases. Old password readers work only
before the first canonical password reset, which intentionally clears their
cache. These are rollout compatibility columns, not independent writable identity
stores. Arbitrary legacy writers must be retired at cutover; ORM changes to a
membership's verification flag no longer verify a Person. Credential backfill
cannot safely be reversed into membership cascades; downgrade refuses and rollback
uses a forward repair. Membership expansion policy and enterprise endpoints stay
in EE; core identity ownership and retention need no plugin registration.

Human authentication resolves the canonical Person in core, independently of
enterprise login hooks. Account memberships retain their original user IDs,
account-scoped roles and JWT generation checks. Password and passkey sign-in,
JWT refresh, OAuth code exchange and opaque OAuth access also require an active
Person, membership and account. API keys retain their existing account-bound
ownership and revocation semantics.

Verification and recovery enumerate canonical identities by normalized email;
explicitly linked provisional memberships receive one message with the chosen
username. Equal-address unrelated identities remain separate. New links carry
purpose, expiry, Person ID, expected email and identity generation. Consumption
changes that generation; changed-address links and replay are refused. Old
row-bound links resolve only their explicit Person link and remain usable only
before an identity security change. Address-only links remain invalid.

Passkeys are owned by Person; their nullable user ID records registration
provenance. They remain manageable across eligible accounts and after that
membership is removed. Reset, password change and sign-out-everywhere invalidate
sessions across every membership without changing tenant permissions. Removing
one membership leaves canonical credentials and other memberships intact.

OAuth/SSO identities remain exempt from `REQUIRE_EMAIL_VERIFICATION` when a
canonical PersonAuthIdentity provider credential exists. The exemption follows
the Person across membership changes, never a mutable membership `user_source`
flag, and does not itself mark the canonical email as verified. Local identities
without a provider credential still require explicit email verification.

The hosted-model capability adds enabled, non-retired global models to the
account model catalog and flow selectors, using each model's configured alias
or provider/model identifier. No provider or model name defines built-in status.
The model is read-only, and retired system rows remain available only for
historical references. The catalog lists built-ins first; their Default badge
appears only when the system row is default and the account has no configured
default of the same service kind. OSS deployments do not expose built-ins by default.
Built-in detail usage is scoped to the requesting account; operator credentials
and routing metadata are never included in the public catalog. Preset cloning
prefers a usable account model and otherwise binds the built-in. Hosted allowance
and Free's one-time credit continue to be enforced by the gateway at execution.

### Canonical identity presentation

A profile read carries `person_id` separately from `id` (the current account
membership) and `account_id`. Names, email verification and avatars come from the
canonical Person. Self profile and avatar edits update that Person from either
membership; an email change follows the canonical verification/conflict contract
and revokes every session. Username and recovery-email edits require an
authenticated login session; API keys may edit display names but cannot change
those identifiers through either OSS or EE profile routes. An unavailable
canonical self profile returns 401. Tenant membership administration changes account
access, not another person's profile or credentials.

The core identity directory searches canonical fields inside one account for
member lists. Its platform queries paginate people and expand explicit
memberships, preserving historical event membership IDs and account attribution.
A quarantined migration identity displays an unavailable state without exposing
a generated membership alias or relaxing authentication. Account seat counts
continue to count active membership rows. No presentation migration is required;
apply the identity schema/backfill and authentication changes first.

## Enterprise authentication persistence boundary

`preloop.models.crud.sso_login` supplies account-scoped identity lookup, serialized
provider-subject enrollment, membership persistence and expiring administrator
proof lookup for enterprise protocols. Verified IdP email only resolves an existing
destination-account member whose canonical address is already verified; matching another account's email cannot establish
identity. Known provider subjects can identify a person independently of email.
The caller owns seat admission and authorization; these core helpers assign no
roles and create no accounts. Connection updates advance their revision even
when a write-only secret retains the same secret-reference id. Protocol handling,
role policy and token issuance remain in the enterprise SSO plugin.

## SSO grant provenance

`crud.sso_group_grants` reconciles complete OIDC, SAML and SCIM snapshots without
committing. Each connection/source claim references a physical role or team
assignment. Existing assignments default to `manually_granted=true`; only rows
created by the mapper are eligible for automatic removal after their final claim
disappears. Manual replacement and explicit team edits remain manual. Connection
deletion revokes its claims before cascading configuration, retaining assignments
claimed by other sources. Account hierarchy `access_grant_id` stays independent.
The CRUD boundary validates tenant references and rejects owner authority;
protocol adapters and role selection remain in the enterprise plugin.

## Session authentication assurance

Core carries a bounded authentication context through browser JWTs, account switch
extensions, CLI consent/exchange and opaque MCP OAuth access/refresh records. The
optional `authentication_policy` plugin service can enforce account methods at
issuance, access and refresh. A membership-local `non_sso_auth_generation` is separate
from canonical person/session revocation; all session types still honor ordinary
`auth_generation`. Original authentication account/generation remain distinct from
the selected account to prevent switches from upgrading or reviving assurance.
The schema also holds durable SSO owner notices; delivery and enforcement stay in EE.
SAML persistence remains edition-neutral: `AccountSsoConnection` stores the
public SP certificate paired with its SecretReference, and replay CRUD atomically
consumes expiring pending SAML requests. Protocol implementation and certificate
creation stay in EE. The shared identity CRUD accepts explicit OIDC/SAML sources
while preserving canonical verified-email linkage, account membership boundaries
and provider-link locking. The optional `ee` dependency extra supplies the SAML
XML toolkit; core modules do not import it.

Fresh password and passkey authentication can select an explicitly requested,
already-held active membership after credential verification. Passkey challenge
and completion bind the same optional account UUID. This makes owner recovery
reachable after a different account was last active without treating an existing
session switch as fresh credential proof. Unknown and unavailable destinations
share one refusal.

Email verification uses explicit `email_link` assurance and onboarding uses proven
password assurance before auto-login. Chat-derived requests carry `chat` assurance
and pass the same policy gate before internal bearer dispatch. MCP browser redirect
codes retain the original session assurance and recheck policy when redeemed;
their purpose restriction prevents use as general person sessions. Recovery owner
selection requires the global system owner role, and verified domain comparisons
normalize both stored claims and email domains to IDNA.

## Optional enterprise single sign-on settings

Account settings includes a Single sign-on tab. The Lit console checks the
`enterprise_sso` feature and the caller's account administration permission
before loading account-scoped provider, domain, policy, mapping or SCIM-token
configuration. Without the feature it offers the existing upgrade action.
Submitted client secrets and signing keys are cleared immediately and never
repopulated from configuration reads; SCIM tokens appear once until dismissed.

Email-first login is enabled only when the feature is advertised. Discovery
can offer a local SSO start URL or return the user to existing sign-in methods.
Provider callback/metadata URLs and test-sign-in availability come from the
server. Enforcement controls require a current configuration test completed by the signed-in administrator within 24 hours, a verified
domain and an enabled connection; missing support is explained in the UI.
The API remains responsible for the recent administrator test requirement and
authentication enforcement.

Test sign-in obtains a one-use start URL through an authenticated POST. SAML metadata imports are previewed before applying them to the unsaved editor; only public SP certificates are displayed. A refused account switch can request fresh password/passkey authentication bound to that account, preserving the existing session until credential proof succeeds. All owners retain emergency access, which the enterprise plugin audits and notifies.

Public SSO starts require the configured console/backend Origin or Referer before minting state. The validated local login link explicitly preserves the browser referrer rather than inheriting Shoelace's `noreferrer` default; originless shared URLs remain refused.

## Membership offboarding and machine credential ownership

Console removal and SCIM provisioning share `services.membership_lifecycle`.
Offboarding locks the account, Person and target membership, marks only that
membership inactive and increments only its token generation. In the same
transaction it revokes personal API/runtime keys, CLI and MCP OAuth grants,
ends their runtime sessions, removes team memberships and registered runners,
and records `access.revoked` with actual affected-row counts. Repeated removal
is a no-op. Reactivation checks canonical identity and seat availability without
restoring credentials. Credential issuance serializes with the same lifecycle
boundary and refuses an inactive membership. SCIM uses `commit=False` so its
provisioning record and revocation commit or roll back together.

Account-owned CI principals and durable managed-agent credentials retain their
keys. `ApiKey.user_id` is nullable creator attribution with `ON DELETE SET NULL`;
account deletion still cascades its credentials. A durable credential requires
an active account, agent, key and matching persisted credential binding. Its
runtime principal contains the agent ID separately from creator attribution
and has no human membership ID. Gateway, native permissions, control, desktop
and MCP adapters use that machine identity; ordinary human/admin REST
authentication rejects it. App configuration acknowledgment is a dedicated
self-credential endpoint. Runtime policy checks never inherit the creator's
human permissions. Migration `20261117_membership_offboarding` refuses downgrade
while credentials with deleted creators remain, preserving those credentials.
