# Agent format fixtures

Version-pinned snapshots of the on-disk formats (logs, session files) that the
CLI's upstream-model resolvers parse. Each fixture is a **verbatim sample**
captured from a real agent installation, named `<format>@<agent-version>.<ext>`.

Purpose: the resolvers infer an agent's recent upstream model from files the
agents do not treat as stable interfaces. When an agent changes its format
(e.g. OpenCode 1.18 switched LLM log lines from `service=llm` to
`message=stream`), inference silently returns nothing and onboarding degrades
to MCP-proxy-only without an error. These fixtures turn that silent drift into
a CI failure.

When adding support for a new agent version's format:
1. Capture a sanitized sample (redact keys/tokens/paths) into this tree.
2. Add a case to `agents_format_fixtures_test.go` asserting the resolver
   extracts the expected provider/model.
3. Keep the old version's fixture — resolvers must stay backward compatible.
