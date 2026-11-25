# Sessions list layout screenshots

Captured from the actual Lit view with synthetic API responses on local Vite
servers. Desktop is 1440×1000; phone is 390×844. The before revision is
`c27befa7`. No upstream providers or deployed environments are used.

From `frontend`, run the capture script against each local revision:

```sh
PRELOOP_DISABLE_TELEMETRY=true node scripts/capture-sessions-layout.mjs http://127.0.0.1:5198 before
PRELOOP_DISABLE_TELEMETRY=true node scripts/capture-sessions-layout.mjs http://127.0.0.1:5197 after
```

| View | Before | After |
| --- | --- | --- |
| Desktop | [Before](before-desktop.png) | [After](after-desktop.png) |
| Phone detail | [Before](before-phone.png) | [After](after-phone.png) |

Additional after captures show the [phone list](after-phone-list.png),
[desktop list footer](after-desktop-list-footer.png),
[dark desktop](after-desktop-dark.png) and [dark phone](after-phone-dark.png).
