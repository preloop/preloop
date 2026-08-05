# OpenClaw Plugin Compatibility Report

Generated: deterministic
Status: PASS

## Summary

| Metric                     | Value |
| -------------------------- | ----- |
| Fixtures                   | 1     |
| High-priority fixtures     | 1     |
| Hard breakages             | 0     |
| Warnings                   | 0     |
| Compatibility suggestions  | 0     |
| Issue findings             | 0     |
| Open issue findings        | 0     |
| Runtime-covered findings   | 0     |
| Runtime-partial findings   | 0     |
| P0 issues                  | 0     |
| P1 issues                  | 0     |
| Open P0 issues             | 0     |
| Open P1 issues             | 0     |
| Live issues                | 0     |
| Live P0 issues             | 0     |
| Compat gaps                | 0     |
| Deprecation warnings       | 0     |
| Inspector gaps             | 0     |
| Open inspector gaps        | 0     |
| Runtime coverage artifacts | 0     |
| Upstream metadata          | 0     |
| Contract probes            | 0     |
| Decision rows              | 0     |

## Triage Overview

| Class               | Count | P0 | Meaning                                                                                                                                                  |
| ------------------- | ----- | -- | -------------------------------------------------------------------------------------------------------------------------------------------------------- |
| live-issue          | 0     | 0  | Potential runtime breakage in the target OpenClaw/plugin pair. P0 only when it is not a deprecated compat seam.                                          |
| compat-gap          | 0     | -  | Compatibility behavior is needed but missing from the target OpenClaw compat registry.                                                                   |
| deprecation-warning | 0     | -  | Plugin uses a supported but deprecated compatibility seam; keep it wired while migration exists.                                                         |
| inspector-gap       | 0     | -  | Plugin Inspector needs stronger capture/probe evidence before making contract judgments. Runtime-covered rows are proof-backed and not open report work. |
| upstream-metadata   | 0     | -  | Plugin package or manifest metadata should improve upstream; not a target OpenClaw live break by itself.                                                 |
| fixture-regression  | 0     | -  | Fixture no longer exposes an expected seam; investigate fixture pin or scanner drift.                                                                    |

## P0 Live Issues

_none_

## Other Live Issues

_none_

## Compat Gaps

_none_

## Deprecation Warnings

_none_

## Inspector Proof Gaps

_none_

## Runtime-Covered Inspector Gaps

_none_

## Upstream Metadata Issues

_none_

## Hard Breakages

_none_

## Target OpenClaw Compat Records

| Metric                    | Value                                |
| ------------------------- | ------------------------------------ |
| Configured path           | npm:openclaw@2026.7.1-2              |
| Status                    | ok                                   |
| Requested version         | latest                               |
| Resolved version          | 2026.7.1-2                           |
| Range eligibility version | 2026.7.1                             |
| Source                    | npm:openclaw                         |
| NPM dist-tag              | latest                               |
| Prepared cache            | miss                                 |
| Compat registry           | -                                    |
| Compat records            | 0                                    |
| Compat status counts      | -                                    |
| Record ids                | -                                    |
| Hook registry             | dist/hook-types-DQ9eTy2x.d.ts        |
| Hook names                | 40                                   |
| API builder               | dist/types-DaHgOqFX.d.ts             |
| API registrars            | 55                                   |
| Captured registration     | dist/types-DaHgOqFX.d.ts             |
| Captured registrars       | 55                                   |
| Package metadata          | package.json                         |
| Plugin SDK exports        | 324                                  |
| Manifest types            | dist/manifest-registry-CIZD-9A4.d.ts |
| Manifest fields           | 66                                   |
| Manifest contract fields  | 22                                   |

## Warnings

_none_

## Suggestions To OpenClaw Compat Layer

_none_

## Issue Findings

_none_

## Contract Probe Backlog

_none_

## Fixture Seam Inventory

| Fixture         | Priority | Seams          | Hooks | Registrations | Manifest contracts |
| --------------- | -------- | -------------- | ----- | ------------- | ------------------ |
| openclaw-plugin | high     | plugin-runtime | -     | -             | -                  |

## Decision Matrix

_none_

## Raw Logs

| Fixture         | Code                    | Level | Message                                                                          | Evidence                                                 | Compat record |
| --------------- | ----------------------- | ----- | -------------------------------------------------------------------------------- | -------------------------------------------------------- | ------------- |
| openclaw-plugin | seam-inventory          | log   | observed 0 hooks, 0 registrations, and 0 manifest contracts                      | -                                                        | -             |
| openclaw-plugin | hook-names-present      | log   | all observed hooks exist in the target OpenClaw hook registry                    | -                                                        | -             |
| openclaw-plugin | api-registrars-present  | log   | all observed api.register* calls exist in the target OpenClaw plugin API builder | -                                                        | -             |
| openclaw-plugin | manifest-fields-checked | log   | plugin manifest fields were compared with target OpenClaw manifest types         | openclaw.plugin.json                                     | -             |
| openclaw-plugin | package-metadata        | log   | selected package metadata for plugin contract checks                             | package.json, @preloop-ai/openclaw-plugin, version:0.2.1 | -             |
