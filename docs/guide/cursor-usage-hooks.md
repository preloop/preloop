# Cursor hooks: live conversation tracking in Cost analytics

This page moved to the harness-agnostic guide:

[Usage hooks](usage-hooks.md)

`preloop agents onboard Cursor` now installs the `preloop usage hook`
entries in `hooks.json` for `sessionStart`, `sessionEnd`, `subagentStart`,
`subagentStop`, `stop`, `preCompact` and `beforeSubmitPrompt`. Each
conversation is stored as a runtime session with a transcript-derived
token and cost estimate; transcript text is shipped only with
`--store-transcript`. Generic harness events and Codex CLI session imports
are documented on that page as well.
