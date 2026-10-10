# VS Code Copilot Chat: manual BYOK

Editions: OSS, Cloud, Enterprise. Unless stated otherwise, everything on this page ships in OSS.

`preloop agents onboard "VSCode / Copilot"` writes the MCP firewall entry in `~/.vscode/mcp.json`. Model traffic is a separate manual step. Copilot Chat can send chat, Agent mode, and utility tasks through the Preloop gateway when you add a Custom Endpoint whose key is the onboarded agent's credential.

This page is that click path, a sanitized `chatLanguageModels.json`, and a six-step check for a person sitting in VS Code. The gateway regression test `backend/tests/endpoints/test_openai_gateway_issue_787.py` replays the request shapes with a fake upstream. It does not open VS Code.

GitHub-hosted Copilot models, inline completions, semantic search, and embeddings stay on GitHub. The GitHub-hosted Copilot coding agent and Copilot 365 are out of scope.

## Plan entitlement

Checked 2026-10-10. The pages below do not publish a Free, Pro, and Pro+ matrix.

- [AI language models in VS Code](https://code.visualstudio.com/docs/agent-customization/language-models) says BYOK models work without signing in to GitHub and without a Copilot plan. The same page's FAQ says a Copilot Business or Enterprise administrator must enable the **Bring Your Own Language Model Key in VS Code** policy before those seats can add their own keys, "just like individual plan users." Earlier on the page, the same policy is one an administrator can disable.
- [Use your own language model key in VS Code](https://code.visualstudio.com/blogs/2026/06/18/byok-vscode) (2026-06-18) says the same: BYOK works without a GitHub account and without a Copilot plan, and Copilot Business and Enterprise administrators control BYOK through Copilot policy settings.
- [Bring your own key for GitHub Copilot](https://docs.github.com/en/copilot/concepts/models/bring-your-own-key) lists VS Code under local BYOK and describes that mechanism as suitable for users without a Copilot subscription. For Copilot Business and Copilot Enterprise, an enterprise or organization policy can disable local BYOK in IDEs.
- [Changing the AI model for GitHub Copilot Chat](https://docs.github.com/en/copilot/how-tos/use-ai-models/change-the-chat-model) says a Copilot Business or Copilot Enterprise plan must have **Bring Your Own Language Model Key in Select IDEs** enabled to use third-party models in a supported IDE.

A licensed Business or Enterprise seat was not used for this page. That check is on the founder checklist below.

## Click path

`github.copilot.chat.customOAIModels` is deprecated. Use the Custom Endpoint provider (`vendor` `customendpoint`).

1. Run **Chat: Manage Language Models** from the Command Palette, or open the chat model picker and choose **Manage Language Models**.
2. Select **Add Models**, then **Custom Endpoint**.
3. Enter a group name, a display name, and the API key. The key is the Preloop agent credential from `preloop agents onboard "VSCode / Copilot"`.
4. Choose the API type: **Chat Completions**, **Responses**, or **Messages**.
5. VS Code opens `chatLanguageModels.json`. Set `toolCalling` to `true` and set `url` to the full gateway path, then save.

`toolCalling: true` is required for Agent mode. A model with tool calling off is hidden from the Agent model picker.

The `url` is the full endpoint. A URL with no API path gets `/v1/<api>` appended, so use the full path:

| API | `apiType` | `url` |
| --- | --- | --- |
| Chat Completions | `chat-completions` | `https://YOUR_PRELOOP_URL/openai/v1/chat/completions` |
| Responses | `responses` | `https://YOUR_PRELOOP_URL/openai/v1/responses` |
| Messages | `messages` | `https://YOUR_PRELOOP_URL/anthropic/v1/messages` |

Chat Completions requests are OpenAI Chat Completions with `tools` and `stream: true`. Responses requests use the Responses `input` and tool shape, also streamed. The gateway regression test replays both against an agent credential and checks that the usage row stores tokens and cost for that agent.

For Agent Host sessions, enable `chat.agentHost.byokModels.enabled`. The VS Code page marks that setting experimental.

## chatLanguageModels.json

Store the credential with an input variable. The file below has no secret.

`id` is the model alias configured on the Preloop AI model. One alias can appear on more than one row when you want both Chat Completions and Responses.

```json
[
  {
    "name": "Preloop",
    "vendor": "customendpoint",
    "apiKey": "${input:preloopAgentKey}",
    "models": [
      {
        "id": "openai/example-model",
        "name": "Gateway chat",
        "url": "https://gateway.example.com/openai/v1/chat/completions",
        "apiType": "chat-completions",
        "toolCalling": true,
        "vision": false,
        "maxInputTokens": 128000,
        "maxOutputTokens": 16000
      },
      {
        "id": "openai/example-model",
        "name": "Gateway responses",
        "url": "https://gateway.example.com/openai/v1/responses",
        "apiType": "responses",
        "toolCalling": true,
        "vision": false,
        "maxInputTokens": 128000,
        "maxOutputTokens": 16000
      },
      {
        "id": "anthropic/example-model",
        "name": "Gateway messages",
        "url": "https://gateway.example.com/anthropic/v1/messages",
        "apiType": "messages",
        "toolCalling": true,
        "vision": false,
        "maxInputTokens": 200000,
        "maxOutputTokens": 16000
      }
    ]
  }
]
```

## Founder checklist

Owner: founder. These steps need an interactive VS Code session. They were not run when this page was added.

1. Run `preloop agents onboard "VSCode / Copilot"` and copy the agent credential into the `preloopAgentKey` input. Leave the credential out of `chatLanguageModels.json`.
2. Run **Chat: Manage Language Models**, then **Add Models**, then **Custom Endpoint**.
3. Save `chatLanguageModels.json` with `vendor` `customendpoint`, `toolCalling` `true`, and the three full URLs in the table above.
4. Open the Chat model picker and select the gateway model. If it is missing from Agent mode, `toolCalling` is not `true` or the file was not saved. Restart VS Code if the model is still absent after a save.
5. Send one chat prompt, then one Agent mode turn that calls a tool.
6. In the Preloop console, open **Cost** and **Models**. The **VSCode / Copilot** agent shows token counts and a cost for those two turns. GitHub-hosted model picks stay off those gateway rows.

On that same session, confirm whether Copilot Chat loads the `~/.vscode/mcp.json` file the CLI writes. Tool governance and model metering are separate: a chat that reaches the gateway can still be using a different MCP file.

Also, on a Copilot Business seat and a Copilot Enterprise seat, confirm the admin policy named on the pages above is what actually shows **Custom Endpoint**. That per-plan check is unverified here.

## Related

- [Cursor, Claude Desktop and other MCP clients](other-mcp-clients.md)
- [Copilot coverage](../copilot.md)
- [CLI reference: support levels](../cli.md#support-levels)
