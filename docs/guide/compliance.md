# Compliance

**Settings > Compliance** puts what is out of policy, and who decided what, on
one page.

Rows describe what software on each machine reported. They are not attested,
and an empty table means nothing is out of policy on the devices that
reported, not that every device is in policy.

## What the page shows

- **Summary.** Devices reporting, stale and never reported; agents onboarded
  and discovered but not onboarded; extensions approved, unapproved and not
  decided; open findings by reason.
- **Out of policy now.** One row per device and the people associated with it
  (or *Unassigned*), with the agents, extensions and reasons involved, when
  the oldest condition was first observed, the device's last report, and a
  **Decide** link to the finding on the [Device fleet](device-fleet.md) page.
  Rows are sorted oldest first and filter by person, device, agent and reason.
- **Recent decisions.** Each approve, unapprove, bulk decision, pin and unpin,
  with who made it, the state it replaced and when. History starts when this
  page shipped; earlier decisions show only their current state on the
  Extensions dialog.
- **Export CSV.** The current register, with an `# Compliance register as of`
  line that records when it was taken.

## Editions

The summary of extensions and the decision history are part of every edition.
People, devices and open findings come from the device fleet, which is part of
Preloop Cloud Team and Preloop Enterprise; without it, the page shows the
extensions reported to this account and says so.

## API

| Route | Permission | Returns |
|---|---|---|
| `GET /api/v1/agents/discovery-extensions/state-counts` | `view_agents` | Present extension identities by account decision |
| `GET /api/v1/agents/extension-decisions?limit=` | `view_agents` | Recent decisions, newest first (at most 500) |
