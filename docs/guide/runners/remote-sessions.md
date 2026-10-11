# Remote sessions on a personal runner

Editions: OSS, Cloud, Enterprise. Unless stated otherwise, everything on this page ships in OSS.

A remote session starts one of your locally installed agent harnesses (for
example GitHub Copilot CLI) on a machine where your Preloop runner is
connected, from the web console or a phone browser. The session needs a
directory to work in. This page documents the workspace policy: which
directories a session may open, how a fresh checkout of a tracker repository
is made, what is refused, and why.

The session lifecycle itself (starting a session from the console, turns,
approvals, stopping, the host-side notice) is described in its own sections
of this guide as those parts land.

## Workspaces

A session start names a workspace of one of two kinds. The control plane never
names a path on your machine.

| Kind | What the console sends | What the runner does |
| --- | --- | --- |
| Authorized directory | `{"kind": "authorized_directory", "id": "dir_9f2c"}` (optionally a relative `path` below it) | Opens the directory you listed with `preloop runner dirs add`, after proving the request stays inside it |
| Tracker checkout | `{"kind": "tracker_checkout", "tracker_id": "...", "repository": "owner/name", "ref": "main"}` | Clones the repository into a fresh session directory with a short-lived credential, deletes it when the session ends |

### Authorized directories

Only directories you list on the host can be opened. Manage the list with:

```sh
preloop runner dirs add ~/src/ims --label "IMS" --mode write
preloop runner dirs add ~/src/docs --mode read_only --harness copilot_cli
preloop runner dirs list
preloop runner dirs remove dir_9f2c        # by id or by path
```

`add` resolves the path (symlinks included) and stores the real path with a
generated id in `~/.preloop/runner.json` under `authorized_directories`:

```json
{
  "authorized_directories": [
    {"id": "dir_9f2c", "path": "/home/jane/src/ims", "label": "IMS", "mode": "write", "harnesses": "all"},
    {"id": "dir_aa11", "path": "/home/jane/src/docs", "label": "docs", "mode": "read_only", "harnesses": ["copilot_cli"]}
  ]
}
```

- `mode` is `write` or `read_only`. The mode travels with the resolved
  workspace so the session host can run the harness in a read-only
  configuration; a harness that cannot run read-only is refused for a
  `read_only` directory.
- `harnesses` is `"all"` or a list of harness ids (`copilot_cli`, `cursor_cli`,
  `claude_code`, ...). A session with another harness is refused for that
  directory.
- The control plane sees `id`, `label`, `mode` and `harnesses` only. They are
  sent with the runner's registration and every heartbeat, so a change on the
  host appears in the console within a heartbeat (15 seconds) without a
  restart. Paths never leave the host.

Refused at `dirs add` and again every time an entry is used:

| Entry | Why |
| --- | --- |
| `~`, `$HOME`, `~/x`, globs (`*`, `?`, `[`) | The path must be explicit. Nothing is expanded. |
| `/`, `C:\`, `\\server\share` | A filesystem root would authorize everything. |
| Your home directory, or a directory that contains it | Authorizing the whole home directory authorizes every credential store and configuration file in it. Add project directories below it instead. |
| `\\?\...`, `\\.\...`, `\\server\C$\...` | Device namespace paths and UNC administrative shares. |
| A path that is not a directory, or that no longer resolves to the stored real path | The directory was removed, moved, or replaced by a symlink or junction since it was authorized. `dirs list` shows the entry as `not usable` with the reason; remove it and add the current path. |

### Containment

When a session asks for an authorized directory (optionally with a relative
`path` below it), the runner:

1. re-validates the entry (the checks above), and confirms its stored path is
   still its own real path;
2. refuses a relative `path` that is absolute, carries a drive letter or
   backslash, or contains `.` or `..` segments;
3. inspects every component below the directory as named: a reparse point of
   unknown type is refused;
4. resolves the target with realpath and requires the result to be the
   directory or below it. A symlink (or a Windows junction) inside the
   directory that points outside it is refused with
   `workspace_not_authorized`, even though it was reachable from inside.

On Windows the comparison is case-insensitive and 8.3 short names are
expanded; drive-letter and UNC paths are canonicalised. On Linux and macOS
the comparison is exact.

### Dirty directories

A directory that is a git work tree with uncommitted changes (including
untracked files that are not ignored) is refused with `workspace_dirty`,
so a session cannot silently build on, or destroy, work you have not
committed. The owner of the runner can start the session anyway with
`allow_dirty`; the control plane only sets it for the owner. A directory
outside any repository has nothing to protect and is never refused for this
reason. If `.git` exists but git is not installed for the runner user, the
check fails closed.

### Tracker checkouts

A tracker checkout clones a repository of one of the account's trackers into
`~/.preloop/host-workspaces/sessions/<session id>/<repository name>` and runs
the harness there. The directory is created fresh for each session (an
existing directory with the same id is never reused) and deleted when the
session ends or when the runner starts (no session survives a runner
restart).

- `ref` selects a branch or tag (`git clone --single-branch --branch`);
  absent means the provider's default branch.
- The runner derives the clone URL from the provider and `owner/name`
  (`https://github.com/...`, `https://bitbucket.org/...`). The control plane
  cannot supply a URL, so a credential can only ever be presented to that
  provider, and redirects are refused.

#### The clone credential

The control plane mints a credential immediately before it sends the start
message and sends it inside that message only:

| Tracker | Credential | Lifetime |
| --- | --- | --- |
| GitHub, App-authenticated | Installation access token restricted to the one repository with `contents: read` (GitHub adds `metadata: read`) | 60 minutes (GitHub's maximum) |
| Bitbucket Cloud, managed connection (Enterprise) | The OAuth access token of the managed grant; rotated first if it has less than five minutes left | Up to 2 hours (fixed by Bitbucket); the runner discards it right after the clone |

Refused with a named error before any credential exists:

| Tracker | Error | Why |
| --- | --- | --- |
| GitHub with a personal access token | `checkout_requires_app_or_oauth` | A PAT cannot be narrowed to one repository or made short-lived before it reaches a laptop. |
| Bitbucket with an API token, a repository or workspace access token, an app password, or a pasted OAuth token | `checkout_requires_oauth` | Same reason: it is a long-lived secret that cannot be issued per session. |
| GitLab, Jira or any other tracker type | `checkout_provider_unsupported` | GitLab checkouts are planned (#1489). |
| `repository` not of the form `owner/name` | `checkout_repository_invalid` | |
| The provider refused, or the GitHub App signing configuration is missing on the control plane | `checkout_credential_unavailable` | |

On the runner the credential is used through an askpass helper and nothing
else:

- git runs with `GIT_ASKPASS` pointing at the Preloop binary; git asks it for
  the username (answered from an environment variable, `x-access-token` for
  GitHub, `x-token-auth` for Bitbucket) and for the password, which the
  helper reads from a pipe the runner created and the git process tree
  inherited (fd 3 on Linux and macOS, an inherited handle on Windows). The
  token is never in a process environment, on the command line, in the
  remote URL or in `.git/config`.
- Global and system git configuration are ignored for the clone, and
  `credential.helper` is cleared, so a credential helper (keychain, Windows
  credential manager, `store`), a `url.<base>.insteadOf` rewrite, a proxy or
  a certificate override on the host can neither see nor redirect the
  credential. `GIT_SSL_CAINFO` and `GIT_SSL_CAPATH` from the runner's
  environment are kept so a host behind an inspecting proxy can still verify
  the provider certificate.
- Only `https` is allowed and redirects are refused.
- After the clone the runner clears the token from its memory and the pipe.
  The harness then runs in the checkout without any credential: it cannot
  push, and `git remote -v` shows a plain URL.
- The control plane never stores the credential: it is stripped from
  persisted lease payloads at every depth and from the session record, and
  the audit event `runner_session.checkout_credential_minted` records the
  provider, repository and expiry only.

A failed clone is reported as `checkout_failed` with the last git line (with
the token redacted if a provider ever echoed it), and the session directory
is removed.

### Deferred to 0.18.0

GitLab checkouts (#1489), a per-project checkout cache with TTL and size cap
and write-scoped tokens for pushing from a session (#1478). Flows keep using
the existing host-exec publication for pushes.
