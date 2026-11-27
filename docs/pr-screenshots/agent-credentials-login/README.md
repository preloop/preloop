# Agent credentials and login guidance

Screenshots show the actual Lit views rendered locally with synthetic API
responses. The credential in the creation screenshot is an inert example.
No deployed instance or model provider was contacted.

| Surface | Before | After |
| --- | --- | --- |
| Agent detail | [Before](agent-before.png) | [Credentials card](agent-credentials-after.png) |
| Browser approval | [Before](authorize-before.png) | [After](authorize-after.png) |
| Browser one-time code | [Before](login-code-before.png) | [After](login-code-after.png) |

Additional states: [create](agent-credentials-create.png),
[one-time key](agent-credentials-key.png),
[revoke confirmation](agent-credentials-revoke.png), and
[390px mobile view](agent-credentials-mobile.png).

The terminal transcripts exercise the actual CLI binaries against a local mock
OAuth server with synthetic identities and tokens. Each run completes login.
Ports and OAuth state are normalized; pasted codes are redacted.

| Flow | Before | After |
| --- | --- | --- |
| Local browser callback | [Before](login-loopback-before.txt) | [After](login-loopback-after.txt) |
| Headless copy/paste | [Before](login-headless-before.txt) | [After](login-headless-after.txt) |

This version uses OAuth authorization codes, with a local callback or a
headless copy/paste step. It does not use device-code polling. The login
protocol, state validation, code expiry, token exchange, and storage are
unchanged.
