# warehouse-sim: fixture MCP server for artifact tests

A small, synthetic MCP server for automated tests and manual runs of the
artifact work (#1081, #1082, #1103, #1106). It is a
test fixture: it is excluded from the production image (`.dockerignore`) and
everything in it is fictional.

## Run

```bash
# from the repository root
python -m scripts.fixtures.warehouse_sim                      # stdio
python -m scripts.fixtures.warehouse_sim --http 127.0.0.1:8765
# streamable HTTP endpoint: http://127.0.0.1:8765/mcp
pytest scripts/fixtures/warehouse_sim/tests
```

Binding `127.0.0.1` turns on the MCP SDK's DNS rebinding protection, which
only accepts `Host: 127.0.0.1` / `localhost`. If Preloop runs in Docker and
reaches the fixture as `host.docker.internal`, bind `0.0.0.0:8765` instead.

## Tools

All results are MCP `ContentBlock` values (spec 2026-07-28). No tool has a
side effect; ids are derived from the arguments, so they are stable.

| Tool | Result |
|---|---|
| `get_transcript(site, shift)` | `TextContent` summary line, then `EmbeddedResource {resource: {uri: "warehouse-sim://transcripts/<site>/<shift>.vtt", mimeType: "text/vtt", text}}` |
| `list_workflows(site)` | `TextContent` JSON `{"workflows": [{workflow_id, site, format, path, sha256}]}` |
| `propose_workflow_change(site, workflow_id, bpmn_diff, justification)` | `TextContent` JSON with a `change_id` (`chg-...`), `status: "proposed"` |
| `create_task(site, title, body)` | `TextContent` JSON with a `task_id` (`task-...`) |
| `get_audio(site, shift)` | `AudioContent {data, mimeType: "audio/wav"}`: a 1.5 s tone, about 24 KB, generated on the fly (not speech) |
| `transcribe_audio(audio_ref)` | The same two blocks as `get_transcript` for that clip |

`audio_ref` accepts the clip URI `warehouse-sim://audio/<site>/<shift>.wav`,
`<site>/<shift>`, the base64 `data` returned by `get_audio`, or the sha256 of
the clip bytes.

`justification` on `propose_workflow_change` is optional. With Preloop in
front, the firewall takes the `justification` argument for itself and does
not forward it to the upstream server, so the fixture must not fail without
it (`justification_received` in the result shows what arrived).

## Data

Sites `nord` and `sued`, shifts `early`, `late`, `night`:

| Site/shift | Language | Topic |
|---|---|---|
| `nord/early` | de | Shift handover |
| `nord/late` | en | Damaged pallet report |
| `nord/night` | de | Forklift near miss |
| `sued/early` | de | Picking-route complaint |
| `sued/late` | en | Inventory recount |
| `sued/night` | en | Contact details: a person name, an email, two phone numbers |

Sample BPMN files (`picking-route`, `goods-receipt`) per site are under
`bpmn/`. They are illustrations, not engine configuration.

### Synthetic personal data

* Name: `Erika Mustermann`, the German placeholder name used on specimen ID
  documents.
* Email: `erika.mustermann@example.com` (`example.com` is reserved, RFC 2606).
* US phone: `+1 555 010 0199`, in the NANP `555-0100` to `555-0199` block
  reserved for fiction.
* German phone: `+49 7131 1234567`, chosen by the issue to show a German
  shape. Germany has no reserved fiction range comparable to `555-01xx`
  that we could verify, so this number is a documented fixture value, not a
  guaranteed unassigned one. Do not dial it.

`tests/test_synthetic_data.py` fails if any other email domain or
international (`+...`) number appears anywhere in the fixture, or if any
other phone-shaped run of seven or more digits (national formats such as
`07131 1234567` or `(555) 010-0199` included) appears in the transcripts or
BPMN files.

### What the current PII detector sees

The regex detectors in `backend/preloop/services/model_content_detectors.py`
(`PII_EMAIL_RE`, `PII_PHONE_RE`, lines 17-20, read 2026-10-01) give:

| Value | Result |
|---|---|
| `erika.mustermann@example.com` | email, detected |
| `+1 555 010 0199` | phone, full match |
| `+49 7131 1234567` | phone, but only the substring `7131 1234567` matches. The regex is US-shaped (3-3-4 digits); it finds a 3-3-4 run inside the German number and leaves the `+49 ` prefix outside the span. Detection fires, a span-based redaction would be incomplete. |
| `Erika Mustermann` | not detected (no name detector) |

Only `sued/night` trips the detector; the other five transcripts do not.
`tests/test_synthetic_data.py` pins this so the README fails loudly when the
detector changes.
