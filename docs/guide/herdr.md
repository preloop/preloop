# herdr terminal workspaces

[herdr](https://herdr.dev) is a supported terminal workspace manager. Preloop can
show where an agent session lives and whether its pane is working, idle, blocked
at a native dialog, done, or closed. The adapter runs inside the existing local
runner service and uses its authenticated inventory channel.

## Enable observation

Install herdr and enroll a [local runner](runners/your-machine.md), then opt in on that
device:

```sh
preloop herdr enable --observe
preloop herdr status
preloop sessions list --wide
preloop sessions list --json
```

Observation is off by default. The runner checks snapshots on its normal
heartbeat, approximately every 15 seconds, and reports changed observations.
Protocol 22 is supported. An unknown protocol produces
`herdr_protocol_unsupported` once and disables observation until a supported
snapshot is available. A missing herdr executable causes no runner errors or
log noise.

The console session list and session page show one compact place and state chip.
Use the workspace filter to narrow the session list. Preloop's own active turn
and pending approval signals take precedence over terminal state. A blocked pane
with no Preloop approval is waiting at a dialog in the terminal; resolve that
dialog locally. A closed pane stops appearing live after the next successful
reconciliation.

To bring a session's pane to the front on this device:

```sh
preloop sessions focus <session-id>
```

The runner's device inventory also lists agent panes without a matching Preloop session.
These unmanaged entries show harness, directory, and place. They have no
transcript, and observing them does not create a session or enroll an agent.

## Prompt permission

Observation does not grant permission to type into panes. The separate local
switch reserves that permission for the Talk adapter:

```sh
preloop herdr enable --prompt
preloop herdr disable --prompt
```

The server cannot enable this switch. Prompt delivery also requires an available
Talk route, session authorization, and the device's fresh identity and state
checks. A switch alone does not make a session reachable.

Pane prompts must target the same native harness session in a fresh snapshot.
Only idle or done agents are eligible. Working agents receive ordinary operator
notes; blocked, replaced, missing, and shell panes refuse prompts. Native
permission and question dialogs must be handled locally. The adapter never
answers those dialogs or uses `send-keys`.

## Privacy and disabling

Only session identity, harness, directory, pane state, and terminal location
metadata are reported. Titles and labels are bounded display strings. Preloop
does not read or upload terminal output. Session location metadata follows the
session's retention; governed replies remain in the normal session record.

Disable observation independently, or disable both permissions. The next runner
report removes terminal locations without marking those sessions ended:

```sh
preloop herdr disable --observe
preloop herdr disable
```

See [Agent Control architecture](../architecture/agent-control.md) for the
delivery and security boundary.
