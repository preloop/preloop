# Console record truth

Real Lit components on local Vite with synthetic API responses and disabled
telemetry. No deployed environment or model provider was used. Before is main
at `8c17f51c`; after is this change. Desktop is 1440 by 1000, phone 390 by 844.
The activity strip has no live socket backend and reports Reconnecting.

From `frontend`, with Vite listening on local port 5197:

```sh
PRELOOP_DISABLE_TELEMETRY=true node scripts/capture-console-poc.mjs before
PRELOOP_DISABLE_TELEMETRY=true node scripts/capture-console-poc.mjs after
```

Run each against its corresponding source revision. The fixtures are identical.

| Item | Before | After |
| --- | --- | --- |
| Approval causes | [Before](before-approvals.png) | [After](after-approvals.png) |
| Project coverage | [Before](before-extensions.png) | [After](after-extensions.png) |
| Child session | [Before](before-sessions.png) | [After](after-sessions.png) |
| Child on phone | [Before](before-sessions-phone.png) | [After](after-sessions-phone.png) |
