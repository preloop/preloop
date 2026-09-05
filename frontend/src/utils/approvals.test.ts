import { expect } from '@open-wc/testing';

import {
  APPROVAL_REQUESTS_PAGE_LIMIT,
  approvalStatusLabel,
  formatNextWaitingLabel,
  isExpiringSoon,
  isUnexpiredPendingRequest,
  millisUntilExpiry,
  normalizeApprovalRequest,
  partitionApprovalRequests,
} from './approvals';
import type { ApprovalRequest } from '../types';

const NOW = Date.parse('2026-09-05T12:00:00Z');

function request(overrides: Partial<ApprovalRequest> = {}): ApprovalRequest {
  return {
    id: 'ar-1',
    account_id: 'acc-1',
    tool_configuration_id: 'tc-1',
    approval_workflow_id: 'aw-1',
    execution_id: null,
    tool_name: 'Bash',
    tool_args: {},
    agent_reasoning: null,
    status: 'pending',
    requested_at: '2026-09-05T11:55:00Z',
    resolved_at: null,
    expires_at: '2026-09-05T12:05:00Z',
    approver_comment: null,
    ...overrides,
  } as ApprovalRequest;
}

describe('approval normalisation', () => {
  it('treats a pending request with an expiry ahead as live', () => {
    expect(isUnexpiredPendingRequest(request(), NOW)).to.be.true;
    expect(normalizeApprovalRequest(request(), NOW).status).to.equal('pending');
  });

  it('treats a pending request past its expiry as timed out', () => {
    const stale = request({ expires_at: '2026-07-13T15:04:10Z' });
    expect(isUnexpiredPendingRequest(stale, NOW)).to.be.false;
    const normalized = normalizeApprovalRequest(stale, NOW);
    expect(normalized.status).to.equal('expired');
    expect(normalized.resolved_at).to.equal('2026-07-13T15:04:10Z');
  });

  it('keeps a pending request without an expiry live', () => {
    const open = request({ expires_at: null });
    expect(isUnexpiredPendingRequest(open, NOW)).to.be.true;
    expect(normalizeApprovalRequest(open, NOW).status).to.equal('pending');
  });

  it('leaves an already resolved request untouched', () => {
    const approved = request({
      status: 'approved',
      resolved_at: '2026-09-05T11:58:00Z',
    });
    expect(normalizeApprovalRequest(approved, NOW)).to.equal(approved);
    expect(isUnexpiredPendingRequest(approved, NOW)).to.be.false;
  });

  it('keeps an existing resolved_at when the expiry passed', () => {
    const stale = request({
      expires_at: '2026-07-13T15:04:10Z',
      resolved_at: '2026-07-13T15:04:11Z',
    });
    expect(normalizeApprovalRequest(stale, NOW).resolved_at).to.equal(
      '2026-07-13T15:04:11Z'
    );
  });
});

describe('approval expiry', () => {
  it('reports the time left, and nothing when there is no expiry', () => {
    expect(millisUntilExpiry(request(), NOW)).to.equal(5 * 60 * 1000);
    expect(millisUntilExpiry(request({ expires_at: null }), NOW)).to.be.null;
  });

  it('flags an expiry inside five minutes and not one beyond it', () => {
    expect(isExpiringSoon(request({ expires_at: '2026-09-05T12:04:00Z' }), NOW))
      .to.be.true;
    expect(isExpiringSoon(request({ expires_at: '2026-09-05T12:41:00Z' }), NOW))
      .to.be.false;
    expect(isExpiringSoon(request({ expires_at: '2026-09-05T11:59:00Z' }), NOW))
      .to.be.false;
  });
});

describe('partitionApprovalRequests', () => {
  it('splits waiting from history and sorts waiting by expiry', () => {
    const later = request({ id: 'later', expires_at: '2026-09-05T12:40:00Z' });
    const soonest = request({
      id: 'soonest',
      expires_at: '2026-09-05T12:02:00Z',
    });
    const noExpiry = request({ id: 'no-expiry', expires_at: null });
    const stale = request({ id: 'stale', expires_at: '2026-07-13T15:04:10Z' });
    const approved = request({ id: 'approved', status: 'approved' });

    const { waiting, history } = partitionApprovalRequests(
      [later, approved, soonest, stale, noExpiry],
      NOW
    );

    expect(waiting.map((r) => r.id)).to.deep.equal([
      'soonest',
      'later',
      'no-expiry',
    ]);
    expect(history.map((r) => r.id)).to.deep.equal(['approved', 'stale']);
  });
});

describe('approvalStatusLabel', () => {
  it('reads declined as denied and expired as timed out, in sentence case', () => {
    expect(approvalStatusLabel('pending')).to.equal('Pending');
    expect(approvalStatusLabel('approved')).to.equal('Approved');
    expect(approvalStatusLabel('declined')).to.equal('Denied');
    expect(approvalStatusLabel('expired')).to.equal('Timed out');
    expect(approvalStatusLabel('cancelled')).to.equal('Cancelled');
  });
});

describe('formatNextWaitingLabel', () => {
  it('states the filtered count when the pending page is not full', () => {
    expect(formatNextWaitingLabel(7, 8)).to.equal('Next waiting (7)');
  });

  it('says 100+ when the pending page is full, not a count that looks total', () => {
    expect(formatNextWaitingLabel(99, APPROVAL_REQUESTS_PAGE_LIMIT)).to.equal(
      'Next waiting (100+)'
    );
    expect(formatNextWaitingLabel(100, APPROVAL_REQUESTS_PAGE_LIMIT)).to.equal(
      'Next waiting (100+)'
    );
  });
});
