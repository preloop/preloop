import { expect } from '@open-wc/testing';
import type { ApprovalRequest } from '../types';
import {
  approvalActions,
  isDecidableRequest,
  requestNeedsForm,
} from './approval-actions';
import { actionIds, intersectActions } from './registry';

const NOW = Date.parse('2026-09-07T12:00:00Z');

function makeRequest(
  overrides: Partial<ApprovalRequest> = {}
): ApprovalRequest {
  return {
    id: 'req-1',
    account_id: 'acc-1',
    tool_configuration_id: 'tool-1',
    approval_workflow_id: 'wf-1',
    execution_id: null,
    tool_name: 'github.create_pr',
    tool_args: {},
    agent_reasoning: null,
    status: 'pending',
    requested_at: '2026-09-07T11:59:00Z',
    resolved_at: null,
    expires_at: '2026-09-07T12:05:00Z',
    approver_comment: null,
    ...overrides,
  } as ApprovalRequest;
}

const ctx = {
  now: NOW,
  includeDetails: true,
  onApprove: () => {},
  onDeny: () => {},
};

describe('approvalActions', () => {
  it('offers Approve and Deny on a pending, unexpired request', () => {
    expect(actionIds(approvalActions(makeRequest(), ctx))).to.deep.equal([
      'approve',
      'deny',
      'details',
    ]);
  });

  it('offers no decision once the request expired', () => {
    expect(
      actionIds(
        approvalActions(
          makeRequest({ expires_at: '2026-09-07T11:00:00Z' }),
          ctx
        )
      )
    ).to.deep.equal(['details']);
  });

  it('offers no decision on a request already decided', () => {
    for (const status of ['approved', 'declined', 'expired', 'cancelled']) {
      expect(
        actionIds(
          approvalActions(
            makeRequest({ status: status as ApprovalRequest['status'] }),
            ctx
          )
        ),
        status
      ).to.deep.equal(['details']);
    }
  });

  it('leaves a question to its answer panel', () => {
    expect(
      actionIds(approvalActions(makeRequest({ is_question: true }), ctx))
    ).to.deep.equal(['details']);
  });

  it('names the link for what the row is for', () => {
    const waiting = approvalActions(makeRequest(), ctx);
    expect(waiting.find((a) => a.id === 'details')!.label).to.equal('Details');
    const settled = approvalActions(makeRequest({ status: 'approved' }), ctx);
    expect(settled.find((a) => a.id === 'details')!.label).to.equal('View');
  });

  it('treats a pending request with no expiry as decidable', () => {
    expect(isDecidableRequest(makeRequest({ expires_at: null }), NOW)).to.equal(
      true
    );
  });

  describe('a request that carries an answer form', () => {
    const withForm = makeRequest({
      question_schema: {
        type: 'object',
        properties: { waived: { type: 'array' } },
        required: ['waived'],
      },
    });

    it('is recognised from the schema, and from the server flag alone', () => {
      expect(requestNeedsForm(withForm)).to.be.true;
      expect(requestNeedsForm(makeRequest({ has_answer_form: true }))).to.be
        .true;
      expect(requestNeedsForm(makeRequest())).to.be.false;
    });

    it('offers no Approve button, because a row cannot fill a form', () => {
      expect(actionIds(approvalActions(withForm, ctx))).to.deep.equal([
        'deny',
        'details',
      ]);
    });

    it('says what the link is for', () => {
      expect(
        approvalActions(withForm, ctx).find((a) => a.id === 'details')!.label
      ).to.equal('Open to answer');
    });
  });

  it('offers a waiting and a decided request only the link', () => {
    const waiting = approvalActions(makeRequest({ id: 'a' }), ctx);
    const decided = approvalActions(
      makeRequest({ id: 'b', status: 'approved' }),
      ctx
    );
    expect(actionIds(intersectActions([waiting, decided]))).to.deep.equal([
      'details',
    ]);
  });
});
