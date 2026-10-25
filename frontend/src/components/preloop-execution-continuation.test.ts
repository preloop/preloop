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
  trigger_event_details: {
    source: 'github',
    tracker_id: 'tracker-1',
    payload: { repository: { id: 42 } },
  },
  result: {
    pr_url: 'https://github.com/team/repo/pull/7',
    pr_source_branch: 'fix/issue',
  },
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

  it('takes no space when the run is not eligible (#1692)', async () => {
    const element = await fixture<PreloopExecutionContinuation>(
      html`<preloop-execution-continuation
        .execution=${{ id: 'exec-2', status: 'RUNNING' }}
      ></preloop-execution-continuation>`
    );
    await element.updateComplete;
    expect(element.hasAttribute('data-empty')).to.equal(true);
    expect(getComputedStyle(element).display).to.equal('none');
    const eligible = await mount();
    await eligible.updateComplete;
    expect(eligible.hasAttribute('data-empty')).to.equal(false);
  });

  it('keeps the recovery preview accessible on an already registered publisher', async () => {
    const element = await mount();
    element.execution = {
      ...execution,
      result: {
        ...execution.result,
        continuation: { thread_id: 'native-thread' },
      },
    };
    await element.updateComplete;
    expect(control(element, 'preview')).to.exist;
  });

  it('offers explicit native recovery for a disabled existing thread', async () => {
    preview = {
      ...preview,
      existing_thread_id: 'native-thread',
      existing_thread_state: 'stopped',
      native_resume_available: true,
      native_reactivation_available: true,
      allowed_recovery_modes: ['native_resume'],
    };
    const element = await mount();
    await open(element);
    expect(control(element, 'confirm').disabled).to.equal(false);
    expect(control(element, 'confirm').textContent).to.contain(
      'Recover PR follow-up'
    );
    await confirm(element);
    expect(posts()).to.have.length(1);
    const body = JSON.parse(posts()[0].args[1].body);
    expect(body.recovery_mode).to.equal('native_resume');
    expect(body.expected_head_sha).to.equal('a'.repeat(40));
  });

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
    preview.native_resume_expires_at = '2026-09-08T12:00:00Z';
    const element = await mount();
    await open(element);
    expect(element.shadowRoot!.textContent!.replace(/\s+/g, ' ')).to.include(
      'saved checkpoint until 2026-09-08 12:00:00 UTC'
    );
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

  for (const status of [
    'FAILED',
    'TIMED_OUT',
    'STOPPED',
    'CANCELLED',
    'ABORTED',
  ]) {
    it(`offers explicit follow-up for a ${status} publishing execution`, async () => {
      const element = await mount();
      element.execution = { ...execution, status };
      await element.updateComplete;
      await open(element);
      expect(control(element, 'confirm').disabled).to.equal(true);
      await acknowledge(element);
      await confirm(element);
      expect(posts()).to.have.length(1);
    });
  }

  const selectPublication = async (element: PreloopExecutionContinuation) => {
    for (const [name, value] of [
      ['pr-url', preview.pr_url],
      ['branch', preview.branch],
    ]) {
      const input = control(element, name);
      input.value = value;
      input.dispatchEvent(new Event('sl-input', { bubbles: true }));
      await element.updateComplete;
    }
    control(element, 'reload').click();
    await waitUntil(() => !(element as any).loading);
    await element.updateComplete;
  };

  it('requires a selected PR and branch before previewing an unrecorded publication', async () => {
    const element = await mount();
    element.execution = { ...execution, status: 'FAILED', result: {} };
    await element.updateComplete;
    await open(element);
    expect(fetchStub.callCount).to.equal(0);
    expect(control(element, 'reload').disabled).to.equal(true);
    expect(control(element, 'confirm').disabled).to.equal(true);
    await selectPublication(element);
    const url = new URL(
      String(fetchStub.lastCall.args[0]),
      window.location.origin
    );
    expect(url.searchParams.get('pr_url')).to.equal(preview.pr_url);
    expect(url.searchParams.get('branch')).to.equal(preview.branch);
    expect(posts()).to.have.length(0);
    await acknowledge(element);
    await confirm(element);
    expect(JSON.parse(posts()[0].args[1].body)).to.deep.equal({
      recovery_mode: 'published_branch_handoff',
      expected_head_sha: preview.head_sha,
      acknowledge_fresh_conversation: true,
      pr_url: preview.pr_url,
      branch: preview.branch,
    });
  });

  it('invalidates the preview and acknowledgment when the selected publication changes', async () => {
    const element = await mount();
    element.execution = { ...execution, status: 'FAILED', result: {} };
    await element.updateComplete;
    await open(element);
    await selectPublication(element);
    await acknowledge(element);
    const input = control(element, 'branch');
    input.value = 'other-branch';
    input.dispatchEvent(new Event('sl-input', { bubbles: true }));
    await element.updateComplete;
    expect((element as any).preview).to.equal(null);
    expect((element as any).acknowledged).to.equal(false);
    expect(control(element, 'confirm').disabled).to.equal(true);
    await (element as any).adopt();
    expect(posts()).to.have.length(0);
  });

  it('never treats an unrecorded selected PR as a native session continuation', async () => {
    preview.native_resume_available = true;
    preview.allowed_recovery_modes = [
      'native_resume',
      'published_branch_handoff',
    ];
    const element = await mount();
    element.execution = { ...execution, status: 'FAILED', result: {} };
    await element.updateComplete;
    await open(element);
    await selectPublication(element);
    expect(control(element, 'ack')).to.exist;
    await acknowledge(element);
    await confirm(element);
    expect(JSON.parse(posts()[0].args[1].body).recovery_mode).to.equal(
      'published_branch_handoff'
    );
  });

  it('asks for the missing branch when only the PR URL was recorded', async () => {
    const element = await fixture<PreloopExecutionContinuation>(
      html`<preloop-execution-continuation
        .execution=${{ ...execution, result: { pr_url: execution.result.pr_url } }}
      ></preloop-execution-continuation>`
    );
    await open(element);
    expect(fetchStub.callCount).to.equal(0);
    expect(control(element, 'pr-url').value).to.equal(execution.result.pr_url);
    expect(control(element, 'branch').value).to.equal('');
    expect(control(element, 'reload').disabled).to.equal(true);
  });

  it('discards preview responses after the element is detached and reattached', async () => {
    let release!: (response: Response) => void;
    fetchStub.callsFake(
      () =>
        new Promise<Response>((resolve) => {
          release = resolve;
        })
    );
    const element = await mount();
    control(element, 'preview').click();
    await element.updateComplete;
    const container = element.parentElement!;
    element.remove();
    container.append(element);
    release(new Response(JSON.stringify(preview)));
    await new Promise((resolve) => setTimeout(resolve, 0));
    await element.updateComplete;
    expect((element as any).preview).to.equal(null);
    expect((element as any).open).to.equal(false);
    expect(posts()).to.have.length(0);
  });

  it('hides unrecorded PR recovery for non-tracker executions', async () => {
    const element = await mount();
    element.execution = {
      ...execution,
      status: 'FAILED',
      result: {},
      trigger_event_details: { source: 'schedule' },
    };
    await element.updateComplete;
    expect(element.shadowRoot!.querySelector('[data-continuation-preview]')).to
      .not.exist;
    expect(fetchStub.callCount).to.equal(0);
  });

  for (const change of [{ status: 'RUNNING' }, { status: 'COMPLETED' }]) {
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
