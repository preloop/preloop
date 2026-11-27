# Approval readiness and fleet labels

These are actual local Lit views rendered with synthetic API fixtures. No
staging, production, notification destination, or model provider was contacted.
The shell is omitted to focus on the changed views. Pending-route captures the
shared component used by native approval detail. The confidence form is unsaved.

Reproduce with a local Vite server on port 5187, then from `frontend`:

```sh
PRELOOP_DISABLE_TELEMETRY=true node scripts/capture-approval-readiness.mjs
```

- [Native mode and readiness](native-mode.png)
- [Native mode at phone width](native-mode-phone.png)
- [AI threshold](ai-threshold.png)
- [Tool and observer names](tools-seen.png)
- [Current device enrollment](device-enrollment.png)
- [Device reporter evidence](device-evidence.png)
- [Pending request routing and deadline](pending-route.png)
