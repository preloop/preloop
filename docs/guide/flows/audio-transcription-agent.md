# Audio transcription agent

The `audio-transcription-agent` preset (`backend/presets/020-audio-transcription-agent.yaml`)
fetches one recording, has it transcribed, and saves the result to the
session as artifacts:

- a `transcript` (WebVTT preferred) labelled `site`, `shift` and
  `consent_basis`;
- a summary as a `document` with the same labels, whose parent is the
  transcript;
- the raw audio, with the transcript as parent, only when the account has
  [audio storage](../artifacts.md#audio-is-off-by-default) turned on. With it
  off, the deposit is refused with `artifact_audio_storage_disabled` and the
  run still succeeds.

Preloop does not transcribe and does not record. Both happen in an MCP server
the operator registers under **Tools**.

## Audio server contract

The preset expects a server named `audio-mcp` (the template variable
`audio_mcp_server`) with two tools:

| Tool | Returns |
|---|---|
| `get_audio(site, shift)` | MCP `AudioContent` `{type: "audio", data, mimeType}` |
| `transcribe_audio(audio_ref)` | `TextContent` and/or an `EmbeddedResource` with `mimeType: text/vtt` (preferred) or `text/plain` |

Preloop does not substitute variables inside tool allowlists. Either register
your server as `audio-mcp`, or replace `audio-mcp` in the copied flow's
servers and tools with your server's name. Tool names are changed the same
way. "Recording from a device" means a server whose `get_audio` captures the
audio; none ships with Preloop. The warehouse-sim fixture
(`scripts/fixtures/warehouse_sim`) implements this contract for tests.

Known limitation: tool results that pass through the Preloop gateway reach
the agent as one text block that spells out the original `AudioContent` or
`EmbeddedResource` fields (#1151). The prompt tells the agent to rebuild the
block from those fields, unchanged, before depositing it.

## Running it

The preset is off and on demand. Copy it into the account, enable it, then
start it from the console with a payload, or from another agent with the
builtin `run_flow` tool:

```text
run_flow(flow="audio-transcription-agent",
         payload={"site": "nord", "shift": "late",
                  "audio_ref": "nord/late",
                  "consent_basis": "works-agreement-2026-03"})
```

A commented schedule variant is in the YAML.

## Consent

`consent_basis` is recorded as a label exactly as the caller supplies it.
Preloop does not verify consent. When it is missing, the agent deposits
nothing and asks with `ask_user`; the question appears in **Attention** until
someone answers.

## Result

The agent writes `/workspace/result.json` with `"status": "success" | "error"`,
the transcript and summary artifact ids and `audio_stored`.
