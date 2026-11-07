# Single sign-on and SCIM provisioning

Editions: Preloop Cloud, Preloop Enterprise. Preloop (OSS) stores the
configuration tables but does not serve the sign-in or SCIM endpoints.

Account administrators connect an OIDC or SAML identity provider, verify the
email domains it may sign in, map provider groups to teams, and issue SCIM
tokens so the provider can create and remove members.

## Turn it on (self-hosted Enterprise)

Set the Helm values on the API (see `helm/preloop/values.yaml`):

| Value | Environment variable | Purpose |
|---|---|---|
| `sso.enabled` | `ENTERPRISE_SSO_ENABLED` | Mounts the sign-in and administration routes. Turning it off does not lift enforcement on an account; disable enforcement first. |
| `sso.publicUrl` | `SSO_PUBLIC_URL` | Public https origin for the OIDC redirect URI and the SAML service provider URLs. Empty uses `environment.preloopUrl`. |
| `sso.allowPrivateIssuers` | `GATEWAY_IDP_ALLOW_PRIVATE_ISSUERS` | Accept issuer and metadata URLs on private addresses when a connection is saved. Single-tenant installs only. |

Per connection the identity provider sees
`<origin>/api/v1/auth/sso/<id>/callback` (OIDC redirect URI and SAML entity
id), `<origin>/api/v1/auth/sso/<id>/metadata` and, for SAML,
`<origin>/api/v1/auth/sso/<id>/acs`.

## Configure a connection in the console

Open **Account settings > Single sign-on**. The tab needs account
administration permission.

1. Add an OIDC or SAML connection. Client secrets and signing keys are
   write-only: the form clears them after saving and never shows them again.
   SAML metadata imports are previewed before they are applied, and only the
   public service provider certificate is displayed.
2. Verify the email domain the connection signs in.
3. Map provider groups to teams. Assignments the mapper created are removed
   when their last group claim disappears; access you granted by hand stays.
4. Run **Test sign-in**. Enforcement can be switched on only after the
   signed-in administrator completed a test of the current configuration
   within the last 24 hours, with a verified domain and an enabled connection.

Owners keep emergency access to the account while enforcement is on; that
access is audited and notified.

## Sign-in page

The login page asks for an email first only when single sign-on is actually
configured: an enabled connection in an account with a verified domain
(`sso_login` in `GET /features`). When discovery finds no provider for the
email, the password form opens with the email filled in. Password and passkey
sign-in stay available for recovery.

## SCIM provisioning

Create a SCIM token on the same tab and choose the single sign-on connection
it provisions for. The choice is required when the account has more than one
connection and preselected when it has one. The token is shown once. A token
that is not bound to a connection cannot provision and is marked for
replacement in the token list.

SCIM links an existing member of the account or creates a member who can
complete the first sign-in only through that connection. It never matches a
person in another account. Groups apply only to existing team mappings.
Deprovisioning revokes access the same way as
[removing a member](../removing-members.md).

## Related

- [Roles and permissions](roles.md)
- [Managing teams](managing-teams.md)
- [Identity, membership and SSO (architecture)](../../architecture/identity.md)
