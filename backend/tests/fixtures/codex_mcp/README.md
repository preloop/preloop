# Hosted Codex MCP wire captures

Both files contain the complete `tools` array received by a local mock Responses
endpoint from the real Linux amd64 Codex binary. MCP served only two synthetic
tools with empty argument schemas. No customer data or credentials are present.

| Capture | Image digest | Runtime CLI |
| --- | --- | --- |
| `191dc7e2-0.153.4-tools.json` | `sha256:191dc7e2ef84b7a05c2b3d6403df59c9b2e51e890b0853eae1935ccc4417dd78` | `codex-cli 0.153.4` |
| `c6f75840-0.153.4-tools.json` | `sha256:c6f75840defd44b6a6b187302acf165e245b378844098c8b57442bceee5f5b96` | `codex-cli 0.153.4` |

Repository: `ghcr.io/preloop/preloop-codex-universal`. Each image was started
with `--platform linux/amd64` and `PRELOOP_DISABLE_TELEMETRY=true`, its runtime
selection script and entrypoint, then `npm install -g @openai/codex@0.153.4`,
matching the rc.1 hosted launcher. The images supply toolchains; the backend
independently pins the CLI. Neither image supplied a preinstalled Codex binary.
The two captured arrays are identical.

Configuration came from `CodexAgent._build_codex_auth_config`, including the
custom-model catalog and its HTTP `[mcp_servers.preloop]` entry. Only endpoint
URLs and synthetic model/authentication values were substituted. The command
was `codex exec --json --skip-git-repo-check --yolo --model local-test-model`.

Both routers reached the local MCP server only when the Responses item carried
`{"type":"function_call","namespace":"mcp__preloop","name":"get_pull_request"}`.
Flat names `mcp__preloop__get_pull_request`, `mcp__preloop.get_pull_request`,
`preloop.get_pull_request`, and `get_pull_request` were rejected. Replaying the
gateway's actual SSE with a fake chat-completions upstream reproduced dotted
call failures before the fix and successful MCP calls for all four aliases
after it, in both images. The flat-declaration unit tests are compatibility
cases; they are not claimed as captures from these runtimes.
