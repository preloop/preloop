# Cursor hooks: live conversation tracking in Cost analytics

This page moved to the harness-agnostic guide:

[Usage hooks](usage-hooks.md)

The Cursor section there is the same wiring as before: put
`preloop usage hook` in `hooks.json` for `sessionStart`, `sessionEnd`,
`subagentStart`, `subagentStop`, `stop`, and `preCompact`. Generic
harness events and Codex CLI session imports are documented on that
page as well.
