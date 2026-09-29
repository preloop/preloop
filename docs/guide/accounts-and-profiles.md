# Accounts, subaccounts and CLI profiles

Editions: OSS, Cloud, Enterprise. Unless stated otherwise, everything on this page ships in OSS.

Multi-account, subaccounts, sharing and access rules are served by an
extension plugin. The open-source server does not have these endpoints. The
console and the CLI show the related views and commands only when
`GET /api/v1/features` reports the matching capability:

| Capability | Console | CLI |
|---|---|---|
| `multi_account` | Header account switcher, account chooser at sign-in | `accounts` works against the server's memberships |
| `account_hierarchy` | Settings > Subaccounts, Settings > Access grants, sharing on resource pages, read-only shared views, usage and attention by subaccount | `subaccounts`, `share` |
| `abac_rules` | Tags on resource pages, Policies > Access rules | `tags`, `access` |

All three are `false` unless a plugin sets them. With all three off, none of
the gated routes, nav items or panels is registered and their code is never
downloaded. If an account endpoint answers 404 anyway (for example after the
plugin was removed), the view hides without an error toast. A 404 on one item,
such as a sibling's subaccount id, shows "not found".

## CLI profiles and accounts

`~/.preloop/config.yaml` can hold several named profiles, and each profile
can hold one token pair per account. A file written before profiles existed
keeps working unchanged: its top-level keys are the `default` profile.

```yaml
# The default profile, exactly as before profiles existed.
access_token: <token>
refresh_token: <token>
api_url: https://preloop.ai

current_profile: work          # optional; used when no flag or env var picks one
profiles:
  work:
    api_url: https://preloop.example.com
    access_token: <login token>
    refresh_token: <login token>
    current_account: north     # set by `preloop accounts switch`
    accounts:
      north:
        account_id: 6c1f...
        name: North site
        access_token: <token for north>
        refresh_token: <token for north>
```

Profile and account names are lowercase letters, digits, `-` and `_`.

### Which session a command uses

1. Profile: `--profile`, then `PRELOOP_PROFILE`, then `current_profile`, then
   `default`.
2. Account: `--account`, then `PRELOOP_ACCOUNT`, then the profile's
   `current_account`. With no account, the profile's own token pair is used.
3. Token: `--token` and `PRELOOP_TOKEN` still override everything.

If an account is asked for and the profile holds no tokens for it, the
command stops with a hint to run `preloop accounts switch <slug>`. It never
falls back to another account's tokens.

### Commands

- `preloop accounts list` lists the accounts you belong to and marks the
  current one.
- `preloop accounts switch <slug|id>` asks the server for a token pair for
  that account, stores both tokens under the profile and makes it current.
- `preloop accounts current` prints the profile and account in use;
  `preloop auth status` shows them too.
- `preloop subaccounts list|create|rename|detach|delete` (`account_hierarchy`).
- `preloop share list|add|rm` (`account_hierarchy`). `share add` adds a share
  and leaves existing shares alone; `share rm <share-id>` stops one.
- `preloop tags list|set|rm <kind> <id>` (`abac_rules`). Keys the parent
  account governs are read-only.
- `preloop access rules list`, `preloop access rules apply -f <file|->` and
  `preloop access explain --subject --action --resource` (`abac_rules`).
  `apply` replaces this account's rules with the file, so the file is the
  source of truth.

The gated groups are hidden from `preloop --help` unless the server reports
their capability. Running one anyway prints which capability is off.

## Endpoint contract for the extension

All paths are under `/api/v1`. A 404 on a collection means "capability off" to
both clients. `{kind}` is one of `ai_model`, `mcp_server`, `managed_agent`,
`flow`, `runner_pool` or `policy`. For `runner_pool` the id is the pool name,
and for `policy` it is the id of the active baseline version.

| Method | Path | Body and notes |
|---|---|---|
| GET | `/me/memberships` | `{items:[{account_id, account_name, slug, parent_account_id, last_used_at}]}` |
| POST | `/auth/switch-account` | `{account_id}`, returns `{access_token, refresh_token}` |
| GET, POST | `/accounts/{id}/subaccounts` | POST `{name, tags}` |
| GET, PATCH, DELETE | `/accounts/{id}/subaccounts/{sub_id}` | PATCH `{name, tags}`; 404 when `sub_id` is not a child of `id` |
| POST | `/accounts/{id}/subaccounts/{sub_id}/detach` | |
| GET, POST | `/accounts/{id}/access-grants` | `{subject_type, subject_id, level, target, subaccount_ids}` |
| DELETE | `/accounts/{id}/access-grants/{grant_id}` | |
| GET | `/accounts/{id}/shares?resource_type=&resource_id=` | A resource can have several shares |
| POST | `/accounts/{id}/shares` | `{resource_type, resource_id, target}`; target is `{type: all}`, `{type: selected, subaccount_ids}` or `{type: tag, key, value}` |
| DELETE | `/accounts/{id}/shares/{share_id}` | |
| GET | `/accounts/{id}/shared-resources/{kind}/{resource_id}` | `{kind, id, name, provider, identifier, description, price, shared_from}`; never credentials |
| GET | `/accounts/{id}/usage/rollup?subaccount_id=&start=&end=` | `{rows:[{subaccount_id, subaccount_name, model, day, requests, cost_usd}]}` |
| GET | `/accounts/{id}/attention/rollup` | `{items:[{subaccount_id, subaccount_name, count}]}` |
| GET, PUT | `/tags/{kind}/{resource_id}` | GET `{tags, governed_keys, version}`; PUT `{tags, version}` |
| GET, PUT | `/access/rules` | GET `{rules, inherited, modes, version}`; PUT `{rules, version}` |
| GET | `/access/rules/export` | `{yaml}` |
| POST | `/access/rules/apply` | `{yaml}` |
| POST | `/access/explain` | `{subject, action, resource}`, returns `{effect, reason, rule_ids}` |
| POST | `/access/modes/preview` | `{action, mode}`, returns `{losing_access, preview_token}` |
| PUT | `/access/modes/{action}` | `{mode, preview_token}` |

Tag and rule writes carry the `version` the client read. The server should
answer 409 when the stored set has a different version, and the clients then
reload instead of overwriting. A client that sends `version: null` has not
read a version (for example against a server that does not return one).

Moving an action to `require_permit` can remove access. The console enables
the save only after it has shown the preview, and sends the preview's
`preview_token` so the server can check that the preview was seen.
