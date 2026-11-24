# Microsoft Teams manifest schema

`MicrosoftTeams.schema.json` was fetched on 2026-11-24 from Microsoft's published
schema: https://developer.microsoft.com/json-schemas/teams/v1.30/MicrosoftTeams.schema.json

The package test validates the generated manifest against this file (only a final newline is added by pre-commit).
The schema contains ECMA Unicode regexes in unused extension definitions; Python's
`jsonschema` meta-schema checker cannot compile those expressions. The test uses
the declared schema validator directly to validate the actual bot manifest.
Admin-center acceptance and real-tenant behaviour remain unverified.
