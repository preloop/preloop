# Plugin manager layout fixtures

Captured on 2026-11-25 from fresh homes with no login or model requests:

- `@anthropic-ai/claude-code` 2.1.332: `claude plugin marketplace add
  anthropics/claude-plugins-official`, `claude plugin install
  code-review@claude-plugins-official`, `claude plugin marketplace add
  obra/superpowers-marketplace`, `claude plugin install
  superpowers@superpowers-marketplace`.
- `@openai/codex` 0.172.0: `codex plugin marketplace add
  obra/superpowers-marketplace`, `codex plugin add
  superpowers@superpowers-marketplace`.

All scripted runs set `PRELOOP_DISABLE_TELEMETRY=true`. Claude also had
nonessential traffic, telemetry, and error reporting disabled.

Claude wrote registry version 2, one array of install records per
`name@marketplace`, absolute cache paths, and enabled flags in `settings.json`.
Codex wrote enabled flags and marketplace sources in `config.toml` and content
in `plugins/cache/superpowers-marketplace/superpowers/7.0.2/`, including
`.codex-plugin/plugin.json`. No `plugins/store/` was created by either agent.

The registry files preserve the captured shape. Home paths use `{{HOME}}`;
repository sources, timestamps and commit hashes are synthetic. Settings retain
only the relevant plugin section; manifests retain name and version. Test
content files are synthetic rather than copies of upstream plugin code.

References:

- [Claude plugin loading and disk layout](https://code.claude.com/docs/en/plugins/loading)
- [Claude manifest reference](https://code.claude.com/docs/en/plugins/manifest-reference)
- [Codex plugin packaging and settings](https://developers.openai.com/plugins/build/plugins)
- [Codex installed-version selection](https://github.com/openai/codex/blob/rust-v0.172.0/codex-rs/core-plugin-common/src/installed.rs)
