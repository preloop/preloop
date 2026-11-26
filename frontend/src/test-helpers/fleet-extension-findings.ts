import type { FleetFinding } from '../device-fleet-api';

/**
 * Synthetic API records verified against the Enterprise fleet evidence
 * producer's evaluate_extensions and finding_summary serializers (2026-11-26).
 * These are persisted fleet findings, not the CLI's local discovery findings:
 * the evaluator preserves extension_id, emits extension_kind as kind, and
 * records its verdict as reason. The summary passes that evidence through.
 */
export const fleetExtensionFindings: FleetFinding[] = [
  {
    extension_id: 'review-helper',
    kind: 'skill',
    reason: 'unapproved',
    approval_state: 'unapproved',
    effective_state: 'unapproved',
    version: null,
    version_constraint: null,
  },
  {
    extension_id: 'release-check',
    kind: 'hook',
    reason: 'content_changed',
    approval_state: 'approved',
    effective_state: 'content_changed',
    version: null,
    version_constraint: null,
    pinned_hashes: ['sha256:' + 'a'.repeat(64)],
  },
  {
    extension_id: 'build-tools',
    kind: 'plugin',
    reason: 'version_outside_constraint',
    approval_state: 'approved',
    effective_state: 'approved',
    version: '2.0.0',
    version_constraint: '1.4.*',
  },
].map((evidence, index) => ({
  id: `00000000-0000-4000-8000-00000000000${index + 1}`,
  rule: 'unapproved_extension',
  device_key: 'fp:' + 'a'.repeat(64),
  user_id: null,
  state: 'open',
  opened_at: '2026-11-26T10:00:00Z',
  last_evaluated_at: '2026-11-26T10:01:00Z',
  resolved_at: null,
  evidence: {
    agent_kind: 'claude_code',
    profile_ref: 'default',
    source_kind: 'local',
    content_hash: 'sha256:' + 'b'.repeat(64),
    content_hash_status: 'ok',
    scope: 'user',
    project_ref_hash: null,
    first_seen_at: '2026-11-26T10:00:00Z',
    last_seen_at: '2026-11-26T10:01:00Z',
    attention_enabled: true,
    ...evidence,
  },
  limitation:
    'Inventory proves files are present in a known location, not that the agent loaded or executed them.',
  title: 'An installed agent extension needs approval.',
  detail:
    'Inventory proves files are present in a known location, not that the agent loaded or executed them.',
  href: '/console/settings/device-fleet',
  severity: 'warning',
  source: 'device_fleet',
}));
