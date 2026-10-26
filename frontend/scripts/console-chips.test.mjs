// Console chips (DESIGN.md "Chips" and "Colour, semantically"): primary is
// for actions and links only, so no console sl-badge is primary. Shoelace's
// sl-badge defaults to `variant="primary"`, so a badge without a variant is
// primary too. The people pages, runners, Gateway traffic and model detail
// were moved onto the tinted pill; the allowance below lists the badges that
// predate this check, per file, and may only shrink.
import assert from 'node:assert/strict';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const src = path.join(root, 'src');

/** Primary badges that predate this check. Lower a count when you fix one. */
const PRIMARY_ALLOWANCE = {
  'components/flow-governance-card.ts': 1,
  'components/preloop-gateway-event.ts': 2,
  'components/session-optimization-panel.ts': 1,
  'components/session-replay-panel.ts': 3,
  'components/tool-card.ts': 1,
  'utils/verdict.ts': 1,
  'views/authed/agent-detail-view.ts': 2,
  'views/authed/console-shell.ts': 2,
  'views/authed/hierarchy/shared-badge.ts': 1,
  'views/authed/issues-dependencies-view.ts': 2,
  'views/authed/policies-view.ts': 1,
  'views/authed/runtime-sessions-view.ts': 3,
  'views/authed/settings/webhooks-view.ts': 1,
};

/** Computed variants cannot be proven safe by this text scanner. Existing
 * expressions are explicitly counted here, including audit outcomes, policy
 * rule actions and issue statuses. New expressions require review; lower the
 * count when replacing one with a literal non-primary variant. */
const UNVERIFIED_ALLOWANCE = {
  'components/answer-form.ts': 1,
  'components/execution-report-panel.ts': 1,
  'components/fleet-map.ts': 1,
  'components/flow-governance-card.ts': 1,
  'components/inventory-card.ts': 3,
  'components/issue-detail-view.ts': 2,
  'components/mcp-server-card.ts': 1,
  'components/preloop-execution-tree.ts': 1,
  'components/preloop-gateway-event.ts': 2,
  'components/session-approval-card.ts': 1,
  'components/session-chat-view.ts': 1,
  'components/session-embedding-settings.ts': 1,
  'components/session-list-panel.ts': 2,
  'components/session-optimization-panel.ts': 2,
  'components/session-replay-panel.ts': 3,
  'components/session-request-timeline.ts': 1,
  'components/session-summary-panel.ts': 1,
  'components/similar-sessions-panel.ts': 1,
  'components/single-issue-detail-view.ts': 2,
  'components/tool-card.ts': 1,
  // OAuth connection state is limited to success, warning, and neutral.
  'components/tools-editor-component.ts': 1,
  'utils/agent-display.ts': 1,
  'utils/tracker-status.ts': 2,
  'views/authed/agent-detail-view.ts': 2,
  'views/authed/agent-talk-view.ts': 1,
  'views/authed/agents-view.ts': 4,
  'views/authed/api-usage-view.ts': 1,
  'views/authed/approval-view.ts': 3,
  'views/authed/approvals-view.ts': 3,
  'views/authed/attention-view.ts': 1,
  'views/authed/audit-view.ts': 2,
  'views/authed/cost-view.ts': 1,
  'views/authed/dashboard-control-plane-view.ts': 2,
  'views/authed/delivery-report-view.ts': 1,
  'views/authed/delivery-reports-view.ts': 1,
  'views/authed/device-fleet-view.ts': 2,
  'views/authed/flow-execution-view.ts': 2,
  'views/authed/flow-executions-view.ts': 1,
  'views/authed/flow-view.ts': 3,
  'views/authed/flows-view.ts': 3,
  'views/authed/governance-view.ts': 3,
  'views/authed/issues-compliance-view.ts': 3,
  'views/authed/issues-dependencies-view.ts': 1,
  'views/authed/issues-view.ts': 2,
  'views/authed/notification-preferences-view.ts': 1,
  'views/authed/policies-view.ts': 1,
  'views/authed/runners-view.ts': 2,
  'views/authed/runtime-sessions-view.ts': 2,
  'views/authed/settings/ai-model-detail-view.ts': 3,
  'views/authed/settings/ai-models-view.ts': 2,
  'views/authed/settings/api-keys-view.ts': 1,
  'views/authed/settings/chat-connections-view.ts': 1,
  'views/authed/settings/invitation-management-view.ts': 1,
  'views/authed/settings/user-detail-view.ts': 4,
  'views/authed/settings/user-management-view.ts': 1,
  'views/authed/settings/webhooks-view.ts': 2,
  'views/authed/tracker-detail-view.ts': 2,
  'views/authed/tracker-issue-view.ts': 1,
};

/** Screens moved onto the tinted pill: every badge on them is a pill. */
const PILL_ONLY = [
  'views/authed/settings/user-management-view.ts',
  'views/authed/settings/team-management-view.ts',
  'views/authed/settings/invitation-management-view.ts',
  'views/authed/api-usage-view.ts',
  'views/authed/runners-view.ts',
  'views/authed/settings/ai-model-detail-view.ts',
];

function sourceFiles(dir) {
  const out = [];
  for (const name of readdirSync(dir)) {
    const full = path.join(dir, name);
    if (statSync(full).isDirectory()) out.push(...sourceFiles(full));
    else if (name.endsWith('.ts') && !name.endsWith('.test.ts')) out.push(full);
  }
  return out;
}

function badges(file) {
  return readFileSync(file, 'utf8').match(/<sl-badge\b[^>]*>/gs) || [];
}

export function isPrimaryBadge(tag) {
  if (!/\bvariant\s*=/.test(tag)) return true;
  return /primary/.test(tag);
}

export function isUnverifiedBadge(tag) {
  return /\bvariant\s*=\s*["']?\$\{/.test(tag);
}

export function isPill(tag) {
  return /\spill\b/.test(tag) || /\btag-chip\b|\bchip-tag\b/.test(tag);
}

test('a badge with no variant or a primary variant counts as primary', () => {
  assert.equal(isPrimaryBadge('<sl-badge class="chip" pill>'), true);
  assert.equal(isPrimaryBadge('<sl-badge variant="primary">'), true);
  assert.equal(
    isPrimaryBadge("<sl-badge variant=${x ? 'primary' : 'neutral'}>"),
    true
  );
  assert.equal(isPrimaryBadge('<sl-badge variant="neutral" pill>'), false);
  assert.equal(isPill('<sl-badge variant="neutral">'), false);
  assert.equal(isPill('<sl-badge class="chip" pill variant="neutral">'), true);
});

test('no console sl-badge is primary beyond the shrinking allowance', () => {
  const over = [];
  for (const dir of ['views', 'components', 'utils']) {
    for (const file of sourceFiles(path.join(src, dir))) {
      const rel = path.relative(src, file).split(path.sep).join('/');
      const count = badges(file).filter(isPrimaryBadge).length;
      const allowed = PRIMARY_ALLOWANCE[rel] || 0;
      if (count > allowed)
        over.push(`${rel}: ${count} primary (allowed ${allowed})`);
    }
  }
  assert.deepEqual(over, []);
});

test('the allowance only lists files that still need it', () => {
  const stale = [];
  for (const [rel, allowed] of Object.entries(PRIMARY_ALLOWANCE)) {
    const count = badges(path.join(src, rel)).filter(isPrimaryBadge).length;
    if (count < allowed)
      stale.push(`${rel}: ${count} primary, allowance ${allowed}`);
  }
  assert.deepEqual(stale, []);
});

test('screens on the tinted pill use no square badges', () => {
  const square = [];
  for (const rel of PILL_ONLY) {
    for (const tag of badges(path.join(src, rel))) {
      if (!isPill(tag)) square.push(`${rel}: ${tag.replace(/\s+/g, ' ')}`);
    }
  }
  assert.deepEqual(square, []);
});

test('computed variants are unverified even without the word primary', () => {
  for (const expression of [
    'badge.variant',
    "ruleActionMeta('notify').variant",
    "getStatusVariant('open')",
    'statusVariant',
  ]) {
    assert.equal(
      isUnverifiedBadge('<sl-badge variant=${' + expression + '}>'),
      true
    );
    assert.equal(
      isUnverifiedBadge('<sl-badge variant="${' + expression + '}">'),
      true
    );
  }
  assert.equal(isUnverifiedBadge('<sl-badge variant="neutral">'), false);
});

test('computed badge variants require an explicit counted allowance', () => {
  const actual = {};
  for (const dir of ['views', 'components', 'utils']) {
    for (const file of sourceFiles(path.join(src, dir))) {
      const count = badges(file).filter(isUnverifiedBadge).length;
      if (count)
        actual[path.relative(src, file).split(path.sep).join('/')] = count;
    }
  }
  // Exact counts reject both new unreviewed expressions and stale allowances.
  assert.deepEqual(actual, UNVERIFIED_ALLOWANCE);
});
