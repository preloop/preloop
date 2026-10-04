# Contributing to Preloop

We welcome contributions from the community.

## Code Style

- Python code should pass `ruff check .` and `ruff format .`.
- Frontend code should pass `npm run format:check` from `frontend/`.
- Keep changes focused and update docs when behavior or setup changes.

## Built-in tools

Every agent pays for a built-in tool in prompt budget and attack surface, so prefer
extending an existing tool with optional parameters over adding a new name.

If a change does add a built-in tool, the pull request body must state the
`default_enabled` decision explicitly and why: `true` means every account with a
matching tracker gets it without asking, `false` means it stays hidden until an
account, an agent or a flow allow-list selects it. Record the same value in
`backend/preloop/tools/builtin_defs.py` and in the REST catalogue. A pull request
that adds a tool without that statement is not ready for review.

## Testing

All new features and bug fixes should include tests when practical.

- Backend: `pytest`
- Frontend: `cd frontend && npm run test`

## Guide pages

Pages under `docs/guide/` document shipped behaviour. A findings page or a
design note records observations or a proposed design, and has to say so
before any other body text. Start the file with:

```yaml
---
status: non-normative
---
```

The first line after the title is:

> **Status: findings / design note. Not shipped behaviour.** This page records observations or a proposed design. Nothing here is a product capability unless a linked release note says so.

These pages stay in `docs/guide/` with that banner. Moving them under
`docs/design/` or `docs/findings/` is a separate change.

## Documentation site

docs.preloop.ai is built from this repository: `mkdocs.yml` at the root,
pages under `docs/`. Preview and check a change with:

```bash
pip install --require-hashes -r requirements/docs.txt
mkdocs serve             # http://127.0.0.1:8000
mkdocs build --strict    # what CI runs
```

Every page has an `Editions:` line directly under its title. The default is
"Unless stated otherwise, everything on this page ships in OSS". Wrap a
paragraph that applies only to Cloud and Enterprise in a
`!!! cloud "Cloud and Enterprise"` admonition. On a non-normative page the
status banner comes first and the `Editions:` line follows it.

## Submitting Changes

1. Fork the repository and create a feature branch.
2. Make your changes and run the relevant checks locally.
3. Open a GitHub pull request with a clear description of the change.

Pull requests are reviewed by a core contributor before merge.

Preloop also reviews its own pull requests with the Pull Request Reviewer
flow. For pull requests from forks, that automated review does not start
until a maintainer has read the change and added the `preloop-review`
label; a maintainer may also skip it with `preloop-skip-review`. Do not
wait for the bot before asking for a human review.
