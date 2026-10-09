# Claude Desktop manual checklist (founder-run)

The scripted harness (`verify.sh`, `desktop-direct.sh`) cannot drive the real
Claude Desktop app. This checklist covers what only a person at a Mac or
Windows machine with admin rights can confirm. The harness itself never
writes Desktop's managed configuration; every change below is made by you,
by hand, and undone in the Rollback section.

Record results in the table at the end and paste it into the PR or issue.

Sources: Desktop configuration reference
(https://claude.com/docs/third-party/claude-desktop/configuration), Desktop
LLM gateway page (https://claude.com/docs/third-party/claude-desktop/gateway),
Claude apps gateway (https://code.claude.com/docs/en/claude-apps-gateway).

## Before you start

- [ ] A Preloop you can reach from the Mac (the harness stack on
      `http://127.0.0.1:18900`, or a local dev Preloop). Not staging, not prod.
- [ ] A Preloop API key for route D (`desktop-direct.sh` prints the harness
      one, `pl-harness-direct-key-0000000`), or one minted with
      `preloop auth gateway-credential --client claude-desktop` once #1410 lands.
- [ ] Quit Claude Desktop completely (configuration is read at launch).
- [ ] Note the Desktop version (About Claude): ____________

Where the keys go (pick one per test, never both):

| Platform | Managed (needs admin) | Local (no admin, in-app config window) |
| - | - | - |
| macOS | `/Library/Managed Preferences/<user>/com.anthropic.claudefordesktop.plist` (via a configuration profile) | `~/Library/Application Support/Claude-3p/configLibrary/` |
| Windows | `HKCU\SOFTWARE\Policies\Claude` (REG_SZ values) | `%LOCALAPPDATA%\Claude-3p\configLibrary\` |

A managed source wins over local values. Prefer the local in-app
configuration window for this test when it is enough; it is the easiest to
roll back.

## Route D: Desktop straight to Preloop (`inferenceProvider: gateway`)

1. [ ] Set these keys (JSON form; in a plist or registry use string values,
       and `inferenceCustomHeaders` as a JSON string):

   ```json
   {
     "inferenceProvider": "gateway",
     "inferenceGatewayBaseUrl": "http://127.0.0.1:18900/anthropic",
     "inferenceGatewayApiKey": "<preloop api key>",
     "inferenceGatewayAuthScheme": "x-api-key",
     "inferenceCustomHeaders": { "X-Preloop-Client": "claude-desktop" },
     "chatTabEnabled": true
   }
   ```

   Base URL form: `desktop-direct.sh` showed Preloop serves
   `<preloop>/anthropic` (Desktop appends `/v1/messages`); `<preloop>/anthropic/v1`
   gives `.../v1/v1/messages`, which 404s. If Desktop rejects `http://` for a
   non-loopback host, use the loopback address above or an https Preloop.
2. [ ] Relaunch Desktop. The model picker lists Preloop's models (from
       `GET /anthropic/v1/models`). Models shown: ____________
3. [ ] **Chat** tab: send "say hi". Reply arrives.
4. [ ] **Cowork** tab: start a task "list the files in this folder". Reply arrives.
5. [ ] **Code** tab: open a scratch folder, ask "what is in this folder?". Reply arrives.
6. [ ] In the Preloop console (or `GET /api/v1/account/gateway-usage/search`),
       the three requests are on your key with `meta_data.client = claude_desktop`
       and `meta_data.gateway_source = direct`.
7. [ ] Budget denial: create a budget policy on the key
       (`subject_type: api_key`, `hard_limit_usd: 0.000001`), send one more
       Chat message. Desktop shows an error (copy the exact text): ____________
       Delete the policy afterwards.
8. [ ] Which base URL form worked: `/anthropic` / `/anthropic/v1` / both

## Route G: Desktop through the Claude apps gateway (`bootstrapUrl`)

Run the harness stack first (`./verify.sh` leaves it up). The gateway listens
on `http://localhost:8080` and its policy already carries `desktop: {}` and
`chatTabEnabled` can be set below.

1. [ ] Set `bootstrapUrl` to `http://localhost:8080/user/bootstrap` (and
       `chatTabEnabled: true`). Remove every route D key first.
2. [ ] Relaunch Desktop. It opens the gateway sign-in; sign in at Dex as
       `alice@example.com` / `password`.
3. [ ] **Chat**, **Cowork**, **Code**: one request each, each answers
       `PRELOOP_STUB_OK` (the stub model behind Preloop).
4. [ ] `curl -s http://127.0.0.1:19000/_harness/requests | jq` lists the
       requests; note the `user_agent` value Desktop's traffic carries
       (this decides whether Preloop can tell Desktop from the CLI on route G): ____________
5. [ ] Usage rows carry `gateway_source = claude_apps_gateway` and
       `gateway_subject_email = alice@example.com`.
6. [ ] Budget denial: sign out, sign in as `bob@example.com`, send one
       message, then add a `gateway_subject` budget for bob's subject
       (`verify.sh` step 3 shows the API call) and send another. Desktop shows
       the 429 `billing_error` message (copy it): ____________

## Rollback (do this even if a step failed)

- [ ] Local config: in Desktop's configuration window switch back to the
      previous configuration, or delete the `<id>.json` you added under
      `configLibrary/` and restore `_meta.json`.
- [ ] Managed config: remove the configuration profile (macOS System
      Settings > Privacy & Security > Profiles) or delete the
      `HKCU\SOFTWARE\Policies\Claude` values you added.
- [ ] Quit and relaunch Desktop. It signs in to Claude.ai as before.
- [ ] `docker compose down -v` in this directory.

## Results

| Route | Tab | Allowed request | Budget denial text | Base URL form | Notes |
| - | - | - | - | - | - |
| D | Chat | | | | |
| D | Cowork | | | | |
| D | Code | | | | |
| G | Chat | | | n/a | |
| G | Cowork | | | n/a | |
| G | Code | | | n/a | |
