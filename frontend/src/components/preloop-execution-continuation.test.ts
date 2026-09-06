import {
  expect,
  fixture,
  fixtureCleanup,
  html,
  waitUntil,
} from '@open-wc/testing';
import sinon from 'sinon';
import './preloop-execution-continuation';
import type { PreloopExecutionContinuation } from './preloop-execution-continuation';
import type { FlowContinuationPreview } from '../api';

const execution = {
  id: 'exec-1',
  flow_id: 'flow-1',
  status: 'SUCCEEDED',
  result: { pr_url: 'https://github.com/team/repo/pull/7' },
};
const initialPreview: FlowContinuationPreview = {
  execution_id: 'exec-1',
  flow_id: 'flow-1',
  pr_url: execution.result.pr_url,
  branch: 'fix/issue',
  head_sha: 'a'.repeat(40),
  feedback_enabled: true,
  feedback_readable: true,
  feedback_blocked_reason: null,
  artifact_upload_enabled: true,
  native_resume_available: false,
  existing_thread_id: null,
  existing_thread_state: null,
  allowed_recovery_modes: ['published_branch_handoff'],
  warnings: [],
};

describe('Execution PR follow-up adoption', () => {
  let fetchStub: sinon.SinonStub;
  let preview: FlowContinuationPreview;
  let postResponse: () => Promise<Response>;
  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-token');
    preview = structuredClone(initialPreview);
    postResponse = async () =>
      new Response(
        JSON.stringify({
          thread_id: 'thread-1',
          state: 'waiting',
          pr_url: preview.pr_url,
          recovery_mode: 'published_branch_handoff',
        })
      );
    fetchStub = sinon.stub(window, 'fetch').callsFake(async (_input, init) => {
      return init?.method === 'POST'
        ? postResponse()
        : new Response(JSON.stringify(preview));
    });
  });
  afterEach(() => {
    fixtureCleanup();
    sinon.restore();
    localStorage.clear();
  });
  const mount = () =>
    fixture<PreloopExecutionContinuation>(
      html`<preloop-execution-continuation
        .execution=${structuredClone(execution)}
      ></preloop-execution-continuation>`
    );
  const control = (
    element: PreloopExecutionContinuation,
    name: string
  ): any => {
    const value = element.shadowRoot!.querySelector(
      `[data-continuation-${name}]`
    );
    expect(value, name).to.exist;
    return value;
  };
  const open = async (element: PreloopExecutionContinuation) => {
    control(element, 'preview').click();
    await waitUntil(() => !(element as any).loading);
    await element.updateComplete;
  };
  const acknowledge = async (element: PreloopExecutionContinuation) => {
    const checkbox = control(element, 'ack');
    checkbox.checked = true;
    checkbox.dispatchEvent(new Event('sl-change', { bubbles: true }));
    await element.updateComplete;
  };
  const requests = () =>
    fetchStub
      .getCalls()
      .filter((call) => String(call.args[0]).endsWith('/continuation'));
  const posts = () =>
    fetchStub.getCalls().filter((call) => call.args[1]?.method === 'POST');
  const confirm = async (element: PreloopExecutionContinuation) => {
    control(element, 'confirm').click();
    await waitUntil(
      () => !(element as any).saving && !(element as any).loading
    );
    await element.updateComplete;
  };

  it('does not fetch until explicitly opened and never adopts during preview', async () => {
    const element = await mount();
    expect(requests().length).to.equal(0);
    await open(element);
    expect(requests().length).to.equal(1);
    expect(requests()[0].args[0]).to.equal(
      '/api/v1/flows/executions/exec-1/continuation'
    );
    expect(posts()).to.have.length(0);
    expect(element.shadowRoot!.querySelector('a')!.href).to.equal(
      preview.pr_url
    );
    expect(control(element, 'ack').checked).to.equal(false);
    expect(control(element, 'confirm').disabled).to.equal(true);
  });

  it('requires explicit fresh-conversation acknowledgment and submits the reviewed head', async () => {
    const element = await mount();
    await open(element);
    await (element as any).adopt();
    expect(posts()).to.have.length(0);
    await acknowledge(element);
    await confirm(element);
    expect(posts()).to.have.length(1);
    expect(JSON.parse(posts()[0].args[1].body)).to.deep.equal({
      recovery_mode: 'published_branch_handoff',
      expected_head_sha: 'a'.repeat(40),
      acknowledge_fresh_conversation: true,
    });
    expect(element.shadowRoot!.textContent).to.include(
      'PR follow-up is configured'
    );
  });

  it('uses native resume only when offered and keeps fresh-conversation acknowledgment false', async () => {
    preview.native_resume_available = true;
    preview.allowed_recovery_modes = [
      'native_resume',
      'published_branch_handoff',
    ];
    const element = await mount();
    await open(element);
    expect(element.shadowRoot!.querySelector('[data-continuation-ack]')).to.not
      .exist;
    await confirm(element);
    expect(JSON.parse(posts()[0].args[1].body)).to.deep.equal({
      recovery_mode: 'native_resume',
      expected_head_sha: 'a'.repeat(40),
      acknowledge_fresh_conversation: false,
    });
  });

  for (const field of [
    'feedback_enabled',
    'feedback_readable',
    'artifact_upload_enabled',
    'allowed_recovery_modes',
    'existing_thread_id',
  ] as const) {
    it(`does not allow adoption when ${field} is unavailable`, async () => {
      if (field === 'allowed_recovery_modes') preview[field] = [];
      else if (field === 'existing_thread_id')
        preview[field] = 'existing-thread';
      else preview[field] = false;
      const element = await mount();
      await open(element);
      expect(control(element, 'confirm').disabled).to.equal(true);
      await (element as any).adopt();
      expect(posts()).to.have.length(0);
      if (field === 'feedback_enabled')
        expect(
          element.shadowRoot!.querySelector(
            'a[href="/console/flows/flow-1?edit=true"]'
          )
        ).to.exist;
    });
  }

  it('refreshes a conflicting head and clears acknowledgment without resubmitting', async () => {
    postResponse = async () => {
      preview.head_sha = 'b'.repeat(40);
      return new Response(JSON.stringify({ detail: 'Head changed' }), {
        status: 409,
      });
    };
    const element = await mount();
    await open(element);
    await acknowledge(element);
    await confirm(element);
    expect(posts()).to.have.length(1);
    expect(requests().length).to.equal(3);
    expect(control(element, 'ack').checked).to.equal(false);
    expect(control(element, 'confirm').disabled).to.equal(true);
    expect(element.shadowRoot!.textContent).to.include('b'.repeat(40));
    expect(control(element, 'error').textContent).to.include('confirm again');
  });

  it('checks adoption state after an uncertain POST failure without another POST', async () => {
    postResponse = async () => {
      preview.existing_thread_id = 'accepted-thread';
      preview.existing_thread_state = 'waiting';
      throw new TypeError('Network interrupted');
    };
    const element = await mount();
    await open(element);
    await acknowledge(element);
    await confirm(element);
    expect(posts()).to.have.length(1);
    expect(requests().length).to.equal(3);
    expect(element.shadowRoot!.textContent).to.include(
      'already has follow-up configured'
    );
    expect(control(element, 'confirm').disabled).to.equal(true);
  });

  it('ignores an old preview response after navigating to another execution', async () => {
    let release: (response: Response) => void = () => {};
    fetchStub.callsFake(
      () =>
        new Promise<Response>((resolve) => {
          release = resolve;
        })
    );
    const element = await mount();
    control(element, 'preview').click();
    await element.updateComplete;
    element.execution = { ...execution, id: 'exec-2' };
    await element.updateComplete;
    release(new Response(JSON.stringify(initialPreview)));
    await new Promise((resolve) => setTimeout(resolve, 0));
    await element.updateComplete;
    expect((element as any).preview).to.equal(null);
    expect((element as any).open).to.equal(false);
    expect(posts()).to.have.length(0);
  });

  for (const change of [
    { status: 'RUNNING' },
    { status: 'FAILED' },
    { result: {} },
    {
      result: { ...execution.result, continuation: { thread_id: 'thread-1' } },
    },
  ]) {
    it(`hides the action for ineligible execution ${JSON.stringify(change)}`, async () => {
      const element = await mount();
      element.execution = { ...execution, ...change };
      await element.updateComplete;
      expect(element.shadowRoot!.querySelector('[data-continuation-preview]'))
        .to.not.exist;
      expect(requests().length).to.equal(0);
    });
  }
});
