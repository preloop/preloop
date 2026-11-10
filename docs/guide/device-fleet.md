# Device fleet

Editions: Cloud, Enterprise. The page appears when the account has the fleet
capability; the alert rules are served by the enterprise fleet service.

Device fleet (**Settings > Device fleet**, `/console/settings/device-fleet`)
lists the workstations Preloop has evidence about: runner heartbeats, collector
reports from [agent discovery reporting](agent-discovery-reporting.md), governed
traffic and, if you upload one, the expected roster. Members with
`view_device_fleet` see the devices; members with `manage_device_fleet` also see
the **Settings** and **Roster** tabs. A member without manage permission who
opens `?tab=settings` lands on the device list, and the settings endpoint
answers 403.

## What the page shows

- **Devices**: one row per device with independent states for the runner,
  collection, inventory and roster. Each state has its own freshness; none is
  combined into a score.
- **Device page**: the people, runners and agents tied to one device, its open
  findings and the source observations behind them.
- **Map**: the same evidence on the [Fleet Map](fleet-map.md).
- **Roster**: an uploaded CSV of expected devices, so a device that never
  reports appears as "Expected, never seen".
- **Settings**: the alert rules below.

## Alert rules

Every rule is off until an administrator turns it on. Saving replaces the
whole settings document through `PUT /api/v1/fleet/settings`; the server
validates every rule before storing any. A server that predates the three
evidence rules returns only the first two, and the page shows only those.

Each row shows one sentence on what the rule cannot show. The evidence rules
(agent quiet, control drift, stale inventory) take a report freshness per
reporter source. Sources are picked from the account's device credentials
(`GET /api/v1/fleet/devices`); a rule is never applied to a source without a
stated freshness.

### Traffic without a live runner

| Opens | Resolves | Never opens |
|---|---|---|
| Governed agent traffic continued for the traffic window while no runner of that user was online past the offline grace | A runner of the user is online again, or the traffic stops | The rule is off; the user has no governed traffic |

Settings: traffic window (minutes), offline grace (minutes).

### Discovered agents not onboarded

| Opens | Resolves | Never opens |
|---|---|---|
| A collector reported an agent that stayed not onboarded for the configured number of days | The agent is onboarded or no longer reported | The rule is off; the device has no collector |

Settings: not onboarded for (days).

### Enrolled agent quiet

| Opens | Resolves | Never opens |
|---|---|---|
| An enrolled agent produced no governed traffic for the inactivity days, past the enrollment grace, while a fresh report from a configured source still lists it | Governed traffic is seen again, or the agent is no longer reported | The rule is off; no configured source has a fresh report; the subject has an unexpired exception |

Settings: quiet for (days), enrollment grace (minutes), report freshness per
source. The server rejects an enabled quiet rule without all three, and the
page shows its message. Quiet means no governed traffic was seen. It does not
show whether the agent was removed, idle or used outside Preloop.

### Expected control no longer observed

| Opens | Resolves | Never opens |
|---|---|---|
| A fresh report from a configured source no longer shows an expected control, such as the managed Claude Code hook | The control is reported again | The rule is off; the report is missing or stale (the state is unknown); no expectation covers the device |

Expectations are set by the server, not typed on the page; the row shows how
many exist. Every expectation's source needs a freshness before the rule can
be enabled.

### Stale runner inventory

| Opens | Resolves | Never opens |
|---|---|---|
| No inventory report arrived within the inventory freshness (24 hours by default) | A new inventory report arrives | The rule is off |

Settings: inventory fresh for (minutes).

## Delivery channels

Each rule has its own channels:

- **Attention**: a finding on the Attention page. It does not send a message.
- **Email**, **push** and **webhook**: sent to eligible owners and admins,
  honoring their notification preferences. Webhooks use the account's
  [outbound webhooks](webhooks.md).

## What this page can and cannot prove

Observations are self-reported, not attestation. Devices without Preloop are
invisible unless they are on the roster.

- Runner heartbeats, collector reports and governed traffic describe what
  Preloop last observed. Sources have independent freshness windows; stale or
  missing reports do not establish a machine's current state.
- The roster supplies an expected inventory. "Expected, never seen" means no
  matching observation is available, not that a device is offline. Hostname
  similarities are hints, not verified device identity.
- Nothing on the page proves that all traffic went through Preloop or that all
  agents on a device are governed.

Related: [Agent discovery reporting](agent-discovery-reporting.md),
[Endpoint coverage and threat model](endpoint-coverage.md),
[Staged MDM deployment recipes](../operations/mdm-recipes.md).
