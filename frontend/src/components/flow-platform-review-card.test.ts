import { expect, fixture, fixtureCleanup, html } from '@open-wc/testing';
import sinon, { SinonSandbox, SinonStub } from 'sinon';
import './flow-platform-review-card';
import type { FlowPlatformReviewCard } from './flow-platform-review-card';

const SCHEMA = {
  type: 'object',
  required: ['verdict'],
  properties: { verdict: { type: 'string', enum: ['pass', 'fail'] } },
};

const FLOW = {
  id: 'flow-1',
  name: 'Reviewer',
  result_schema: SCHEMA,
  validate_result: true,
  git_clone_config: {
    enabled: true,
    publication_mode: 'read_only',
    repositories: [{ tracker_id: 't1', identifier: 'acme/api' }],
  },
};

const TRACKERS = [
  { id: 't1', tracker_type: 'github', auth_type: 'github_app' },
];

describe('flow-platform-review-card', () => {
  let sandbox: SinonSandbox;
  let fetchStub: SinonStub;
  let trackers: unknown = TRACKERS;
  let putBodies: any[];

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
    sandbox = sinon.createSandbox();
    putBodies = [];
    trackers = TRACKERS;
    fetchStub = sandbox
      .stub(window, 'fetch')
      .callsFake(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes('/trackers')) {
          if (trackers === 'error') return new Response('', { status: 500 });
          return new Response(JSON.stringify(trackers));
        }
        if (url.includes('/flows/flow-1') && init?.method === 'PUT') {
          const body = JSON.parse(String(init.body));
          putBodies.push(body);
          if (
            body.git_clone_config?.platform_review?.status?.context ===
            'srv/refuse'
          ) {
            return new Response(
              JSON.stringify({ detail: 'platform_review is refused' }),
              { status: 422 }
            );
          }
          return new Response(JSON.stringify({ ...FLOW, ...body }));
        }
        return new Response('[]');
      });
  });

  afterEach(() => {
    fixtureCleanup();
    sandbox.restore();
    localStorage.clear();
  });

  const mount = async (
    flow: any = FLOW,
    editable = true
  ): Promise<FlowPlatformReviewCard> => {
    const element = await fixture<FlowPlatformReviewCard>(
      html`<flow-platform-review-card
        .flow=${flow}
        .editable=${editable}
      ></flow-platform-review-card>`
    );
    for (let i = 0; i < 50 && (element as any).loadingTrackers; i++) {
      await new Promise((resolve) => setTimeout(resolve, 5));
    }
    await element.updateComplete;
    return element;
  };

  const q = (element: Element, selector: string): any =>
    element.shadowRoot!.querySelector(selector);

  it('lists the prerequisites as checks with no change pending', async () => {
    const element = await mount();
    const checks = element.shadowRoot!.querySelectorAll('[data-check]');
    expect(checks.length).to.equal(6);
    expect(q(element, '[data-check="github_app"]').dataset.ok).to.equal('true');
    expect(q(element, '[data-check="app_permissions"]').textContent).to.contain(
      'Commit statuses: Read and write'
    );
    expect(q(element, 'sl-badge').textContent.trim()).to.equal('Off');
    expect(q(element, '[data-action="save"]').disabled).to.equal(true);
    expect(element.shadowRoot!.textContent!.replace(/\s+/g, ' ')).to.contain(
      'needs no new CI identities'
    );
  });

  it('shows a loading state, then an unchecked connection on error', async () => {
    trackers = 'error';
    const element = await mount();
    expect(q(element, '[data-check="github_app"]').dataset.ok).to.equal('null');
  });

  it('enables posting with a verdict mapping and saves only that block', async () => {
    const element = await mount();
    const toggle = q(element, '[data-field="enabled"]');
    toggle.checked = true;
    toggle.dispatchEvent(new Event('sl-change'));
    await element.updateComplete;
    const statusOn = q(element, '[data-field="status_on"]');
    statusOn.checked = true;
    statusOn.dispatchEvent(new Event('sl-change'));
    await element.updateComplete;
    const fail = q(element, '[data-verdict="fail"]');
    expect(fail.value).to.equal('failure');
    expect(q(element, '[data-verdict="pass"]').value).to.equal('success');
    fail.value = 'error';
    fail.dispatchEvent(new Event('sl-change'));
    await element.updateComplete;

    let saved: any = null;
    element.addEventListener('flow-updated', (e: Event) => {
      saved = (e as CustomEvent).detail.flow;
    });
    q(element, '[data-action="save"]').click();
    await new Promise((resolve) => setTimeout(resolve, 20));
    await element.updateComplete;

    expect(putBodies).to.have.length(1);
    expect(Object.keys(putBodies[0])).to.deep.equal(['git_clone_config']);
    const config = putBodies[0].git_clone_config;
    expect(config.repositories).to.deep.equal(
      FLOW.git_clone_config.repositories
    );
    expect(config.platform_review).to.deep.equal({
      enabled: true,
      status: {
        context: 'preloop/review',
        verdict_states: { pass: 'success', fail: 'error' },
      },
    });
    expect(saved).to.not.equal(null);
    expect(q(element, '[data-saved]')).to.exist;
    expect(q(element, 'sl-badge').textContent.trim()).to.equal('On');
  });

  it('blocks a save the server would refuse and says why', async () => {
    const element = await mount({ ...FLOW, validate_result: false });
    const toggle = q(element, '[data-field="enabled"]');
    toggle.checked = true;
    toggle.dispatchEvent(new Event('sl-change'));
    await element.updateComplete;
    expect(q(element, '[data-blocker]').textContent).to.contain(
      'Validate result is off'
    );
    expect(q(element, '[data-action="save"]').disabled).to.equal(true);
  });

  it('shows the server refusal as an error', async () => {
    const element = await mount();
    const toggle = q(element, '[data-field="enabled"]');
    toggle.checked = true;
    toggle.dispatchEvent(new Event('sl-change'));
    await element.updateComplete;
    const statusOn = q(element, '[data-field="status_on"]');
    statusOn.checked = true;
    statusOn.dispatchEvent(new Event('sl-change'));
    await element.updateComplete;
    const context = q(element, '[data-field="status_context"]');
    context.value = 'srv/refuse';
    context.dispatchEvent(new Event('sl-input'));
    await element.updateComplete;
    q(element, '[data-action="save"]').click();
    await new Promise((resolve) => setTimeout(resolve, 20));
    await element.updateComplete;
    expect(q(element, '[data-error]').textContent).to.contain(
      'platform_review is refused'
    );
    expect(q(element, 'sl-badge').textContent.trim()).to.equal('Off');
  });

  it('refuses a status name GitHub would not accept', async () => {
    const element = await mount();
    const toggle = q(element, '[data-field="enabled"]');
    toggle.checked = true;
    toggle.dispatchEvent(new Event('sl-change'));
    await element.updateComplete;
    const statusOn = q(element, '[data-field="status_on"]');
    statusOn.checked = true;
    statusOn.dispatchEvent(new Event('sl-change'));
    await element.updateComplete;
    const context = q(element, '[data-field="status_context"]');
    context.value = '!bad';
    context.dispatchEvent(new Event('sl-input'));
    await element.updateComplete;
    expect(q(element, '[data-blocker]').textContent).to.contain('status name');
    expect(q(element, '[data-action="save"]').disabled).to.equal(true);
    expect(fetchStub.calledWithMatch(sinon.match(/flows\/flow-1/))).to.equal(
      false
    );
  });

  it('is read-only without edit permission', async () => {
    const element = await mount(
      {
        ...FLOW,
        git_clone_config: {
          ...FLOW.git_clone_config,
          platform_review: {
            enabled: true,
            status: { verdict_states: { pass: 'success', fail: 'failure' } },
          },
        },
      },
      false
    );
    expect(q(element, '[data-field="enabled"]')).to.equal(null);
    expect(q(element, '[data-action="save"]')).to.equal(null);
    expect(q(element, '[data-mapping]').textContent).to.contain('failure');
    expect(q(element, 'sl-badge').textContent.trim()).to.equal('On');
  });

  it('says there is nothing to map when the schema has no verdicts', async () => {
    const element = await mount({
      ...FLOW,
      result_schema: { type: 'object' },
      git_clone_config: {
        ...FLOW.git_clone_config,
        platform_review: {
          enabled: true,
          status: { verdict_states: { pass: 'success' } },
        },
      },
    });
    expect(q(element, '[data-empty="verdicts"]')).to.exist;
    expect(q(element, '[data-check="verdict_enum"]').dataset.ok).to.equal(
      'false'
    );
  });
});
