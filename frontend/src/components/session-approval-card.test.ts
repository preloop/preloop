import { expect, fixture, html, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';
import './session-approval-card';
import type { SessionApprovalCard } from './session-approval-card';
import type { AnswerForm } from './answer-form';
import type { ApprovalRequest } from '../types';
import { resetConfirmDialogForTests } from './confirm-dialog';

const request = (
  overrides: Partial<ApprovalRequest> = {}
): ApprovalRequest => ({
  id: 'approval-example',
  account_id: 'account-example',
  tool_configuration_id: 'tool-example',
  approval_workflow_id: 'workflow-example',
  execution_id: null,
  runtime_session_id: 'session-example',
  tool_name: 'terminal',
  tool_args: { command: 'pwd' },
  agent_reasoning: null,
  status: 'pending',
  requested_at: '2026-10-02T10:00:00Z',
  resolved_at: null,
  expires_at: null,
  approver_comment: null,
  ...overrides,
});
const json = (value: unknown, status = 200) =>
  new Response(JSON.stringify(value), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
describe('inline session decisions', () => {
  let fetchStub: sinon.SinonStub;
  beforeEach(() => {
    localStorage.setItem('accessToken', 'synthetic-token');
    fetchStub = sinon.stub(window, 'fetch');
    resetConfirmDialogForTests();
  });
  afterEach(() => {
    sinon.restore();
    localStorage.removeItem('accessToken');
    resetConfirmDialogForTests();
  });
  async function mount(r = request()) {
    return fixture<SessionApprovalCard>(
      html`<session-approval-card
        .request=${r}
        .canDecide=${true}
      ></session-approval-card>`
    );
  }
  it('posts once and reports an authoritative quorum vote until external resolution', async () => {
    let finish!: (response: Response) => void;
    fetchStub.returns(new Promise((resolve) => (finish = resolve)));
    const element = await mount();
    element.addEventListener(
      'session-approval-updated',
      (event) =>
        (element.request = (event as CustomEvent<ApprovalRequest>).detail)
    );
    const button =
      element.shadowRoot!.querySelector<HTMLElement>('.actions sl-button')!;
    button.click();
    button.click();
    await element.updateComplete;
    expect(fetchStub.callCount).to.equal(1);
    finish(json(request()));
    await waitUntil(() =>
      element.shadowRoot!.textContent!.includes('Your vote was recorded')
    );
    element.request = request({
      status: 'approved',
      resolved_at: '2026-10-02T10:03:00Z',
    });
    await element.updateComplete;
    expect(element.shadowRoot!.textContent).not.to.include(
      'remaining approvers'
    );
    expect(element.shadowRoot!.textContent).to.include('Approved');
  });
  it('keeps approve and deny separate and denial never posts approval', async () => {
    fetchStub.resolves(json(request({ status: 'declined' })));
    const element = await mount();
    const buttons =
      element.shadowRoot!.querySelectorAll<HTMLElement>('.actions sl-button');
    expect(buttons).to.have.length(2);
    expect(buttons[0].contains(buttons[1])).to.equal(false);
    buttons[1].click();
    await waitUntil(() =>
      Boolean(
        document
          .querySelector('confirm-dialog')
          ?.shadowRoot?.querySelector('sl-dialog')
      )
    );
    const dialog = document
      .querySelector('confirm-dialog')!
      .shadowRoot!.querySelector('sl-dialog')!;
    const deny = dialog.querySelector<HTMLElement>(
      'sl-button[variant="danger"]'
    )!;
    deny.click();
    await waitUntil(() => fetchStub.called);
    expect(
      String(
        fetchStub.getCalls().find((call) => call.args[1]?.method === 'POST')!
          .args[0]
      ).endsWith('/decline')
    ).to.equal(true);
    expect(
      fetchStub.getCalls().some((c) => String(c.args[0]).endsWith('/approve'))
    ).to.equal(false);
  });
  it('validates required inline form fields before submitting the structured answer', async () => {
    fetchStub.resolves(json(request({ status: 'approved' })));
    const element = await mount(
      request({
        question_schema: {
          type: 'object',
          properties: { note: { type: 'string', minLength: 3 } },
          required: ['note'],
        },
      })
    );
    const button =
      element.shadowRoot!.querySelector<HTMLElement>('.actions sl-button')!;
    button.click();
    await element.updateComplete;
    expect(fetchStub.called).to.equal(false);
    const form = element.shadowRoot!.querySelector<AnswerForm>('answer-form')!;
    await form.updateComplete;
    const input = form.shadowRoot!.querySelector('sl-input') as HTMLElement & {
      value: string;
    };
    input.value = 'Example answer';
    input.dispatchEvent(new CustomEvent('sl-input', { bubbles: true }));
    await form.updateComplete;
    button.click();
    await waitUntil(() => fetchStub.called);
    expect(JSON.parse(fetchStub.firstCall.args[1].body).answer).to.deep.equal({
      note: 'Example answer',
    });
  });
  it('uses question answer controls and sends the answer without navigating', async () => {
    fetchStub.resolves(json(request({ status: 'approved' })));
    const element = await mount(
      request({
        is_question: true,
        question: 'Choose an example',
        question_options: ['Option A', 'Option B'],
      })
    );
    expect(element.shadowRoot!.querySelector('.actions')).to.equal(null);
    const panel = element.shadowRoot!.querySelector('question-answer-panel')!;
    panel.dispatchEvent(
      new CustomEvent('question-answer', {
        detail: { selectedOption: 'Option A' },
        bubbles: true,
        composed: true,
      })
    );
    await waitUntil(() =>
      fetchStub.getCalls().some((call) => call.args[1]?.method === 'POST')
    );
    const posted = fetchStub
      .getCalls()
      .find((call) => call.args[1]?.method === 'POST')!;
    expect(JSON.parse(posted.args[1].body).selected_option).to.equal(
      'Option A'
    );
  });
  it('refreshes forbidden server decisions and disables ineligible actions', async () => {
    fetchStub
      .onFirstCall()
      .resolves(json({ detail: 'Permission denied' }, 403));
    fetchStub.onSecondCall().resolves(json(request()));
    const element = await mount();
    element
      .shadowRoot!.querySelector<HTMLElement>('.actions sl-button')!
      .click();
    await waitUntil(() =>
      element.shadowRoot!.textContent!.includes('Permission denied')
    );
    await element.updateComplete;
    expect(element.shadowRoot!.querySelector('.actions')).to.equal(null);
    expect(fetchStub.callCount).to.equal(2);
  });
  it('never offers expired or resolved actions and preserves bypass attribution', async () => {
    const expired = await mount(
      request({ expires_at: '2020-01-01T00:00:00Z' })
    );
    expect(expired.shadowRoot!.querySelector('.actions')).to.equal(null);
    expired.request = request({
      status: 'approved',
      was_bypassed: true,
      resolved_at: '2026-10-02T10:00:01Z',
    });
    await expired.updateComplete;
    expect(expired.shadowRoot!.textContent).to.include('Bypass');
    expect(fetchStub.called).to.equal(false);
  });
  it('does not submit a confirmation after the session card is disposed', async () => {
    fetchStub.resolves(json(request({ status: 'declined' })));
    const element = await mount();
    element
      .shadowRoot!.querySelectorAll<HTMLElement>('.actions sl-button')[1]
      .click();
    await waitUntil(() =>
      Boolean(
        document
          .querySelector('confirm-dialog')
          ?.shadowRoot?.querySelector('sl-dialog')
      )
    );
    element.remove();
    document
      .querySelector('confirm-dialog')!
      .shadowRoot!.querySelector<HTMLElement>('sl-button[variant="danger"]')!
      .click();
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(
      fetchStub.getCalls().filter((call) => call.args[1]?.method === 'POST')
    ).to.have.length(0);
  });
});
