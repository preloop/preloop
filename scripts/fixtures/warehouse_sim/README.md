# warehouse-sim: fixture MCP server for artifact demos and tests

A small, synthetic MCP server for the transcript-to-workflow demo and for
automated tests of the artifact work (#1081, #1082, #1103, #1106). It is a
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

## Seeding Preloop

`seed.py` prints the REST calls for two accounts, "Lager Nord" (site `nord`)
and "Lager Sued" (site `sued`). It does not write the database or call the
API:

```bash
python -m scripts.fixtures.warehouse_sim.seed --mcp-url http://host.docker.internal:8765/mcp
python -m scripts.fixtures.warehouse_sim.seed --json    # same plan as JSON
```

Per account it registers the server, scans it, creates a "Site lead"
approval workflow, and sets:

* `get_transcript`, `list_workflows`, `get_audio`: a `tool_access_rules`
  argument rule `args.site != '<own site>'` with action `deny`. The API
  derives the condition type from the expression; this one is stored as
  `simple`.
* `propose_workflow_change`: `justification_mode: "required"`, the same
  own-site deny rule, then `require_approval` with the "Site lead" workflow.
* `create_task`, `transcribe_audio`: enabled, no rules.

## 15-minute demo script

Based on memo D.2 (product memo 2026-10-01, agent artifacts). Steps marked
**checked** were run against a local stack on `main` at `aa4cbcafb` with this
fixture and the `seed.py` plan. Steps marked **existing** use features on
`main` that were not exercised with this fixture. Steps marked
**after #NNNN** need that issue merged; say so when you show it.

1. **checked** Two accounts, "Lager Nord" and "Lager Sued", each with its own
   warehouse-sim registration and rules (Tools page shows the deny and
   approval badges). Nothing is shared; hierarchy is not shown.
2. **checked** Start the agent on "Lager Nord, early shift handover". It calls
   `get_transcript(site=nord, shift=early)` through the Preloop MCP endpoint.
3. **after #1081** The agent deposits the transcript as a `transcript`
   artifact with labels `site=nord`,
   `consent_basis=works-agreement-2026-03` (seeded value).
   **after #1083** It appears inline in the session timeline.
4. **checked** The agent tries `get_transcript(site=sued)`: denied with
   "Lager Nord may only read site nord".
5. **checked** The agent proposes the picking-route change from `sued/early`
   (in the Sued account): an approval request appears under Audit,
   Approvals with the matched rule; approve it in the console.
6. **after #1081** Summary and task list deposited as `document` artifacts.
   **after #1082** Search "damaged pallet" finds the transcript chunk and
   the summary.
7. **existing** PII: a model request carrying `sued/night` is flagged by the
   existing regex content policy (email and phone). Present it as
   detection, not redaction; names are not detected.
8. **existing** Legal hold on the session, evidence export, sha256 per file.
9. **existing** Kill switch on Lager Nord: the next model call is refused.
10. **after #1103** `get_audio` then `transcribe_audio` via the audio
    preset, depositing the transcript.

Claims not to make: hierarchy, redaction, AI Act or works-council
compliance, OpenSandbox-native integration, audio storage at scale.

### Known gaps seen while checking the script (main at `aa4cbcafb`)

* The firewall flattens non-text blocks when it proxies a result:
  `EmbeddedResource` arrives at the agent as Python repr text and
  `AudioContent` is relabelled `ImageContent` before being flattened
  (`backend/preloop/services/mcp_client_pool.py:327-345`,
  `backend/preloop/services/dynamic_fastmcp.py:1225-1229`). The fixture
  returns the correct shapes; #1082 and #1103 depend on the proxy keeping them.
* With `justification_mode: "required"`, the approval request for
  `propose_workflow_change` showed `agent_reasoning: null` although the
  client sent a justification.
