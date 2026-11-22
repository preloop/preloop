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

  const chooseMode = async (
    element: FlowPlatformReviewCard,
    mode: 'off' | 'automatic' | 'manual'
  ): Promise<void> => {
    const group = q(element, '[data-field="mode"]');
    group.value = mode;
    group.dispatchEvent(new Event('sl-change'));
    await element.updateComplete;
  };

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
    await chooseMode(element, 'automatic');
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
    expect(q(element, 'sl-badge').textContent.trim()).to.equal('Automatic');
  });

  it('recovers from a concurrent change: keeps the draft, retries on the latest revision', async () => {
    const ifMatch: (string | null)[] = [];
    const latest = {
      ...FLOW,
      revision: 5,
      git_clone_config: {
        ...FLOW.git_clone_config,
        repositories: [
          ...FLOW.git_clone_config.repositories,
          { tracker_id: 't1', identifier: 'acme/web' },
        ],
      },
    };
    fetchStub.callsFake(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes('/trackers'))
          return new Response(JSON.stringify(TRACKERS));
        if (url.includes('/flows/flow-1') && init?.method === 'PUT') {
          ifMatch.push(new Headers(init.headers).get('If-Match'));
          putBodies.push(JSON.parse(String(init.body)));
          if (ifMatch.length === 1) {
            return new Response(
              JSON.stringify({ detail: { current_revision: 5 } }),
              { status: 412 }
            );
          }
          return new Response(
            JSON.stringify({ ...latest, ...putBodies[1], revision: 6 })
          );
        }
        if (url.includes('/flows/flow-1'))
          return new Response(JSON.stringify(latest));
        return new Response('[]');
      }
    );
    const element = await mount({ ...FLOW, revision: 4 });
    await chooseMode(element, 'automatic');
    const statusOn = q(element, '[data-field="status_on"]');
    statusOn.checked = true;
    statusOn.dispatchEvent(new Event('sl-change'));
    await element.updateComplete;

    q(element, '[data-action="save"]').click();
    await new Promise((resolve) => setTimeout(resolve, 30));
    await element.updateComplete;
    expect(ifMatch).to.deep.equal(['"4"']);
    expect(q(element, '[data-error]').textContent).to.contain('Save again');
    // The draft survived the refresh to the latest flow.
    expect(q(element, '[data-field="mode"]').value).to.equal('automatic');

    q(element, '[data-action="save"]').click();
    await new Promise((resolve) => setTimeout(resolve, 30));
    await element.updateComplete;
    expect(ifMatch).to.deep.equal(['"4"', '"5"']);
    // The retry is built on the latest config, so the other change is kept.
    expect(putBodies[1].git_clone_config.repositories).to.have.length(2);
    expect(putBodies[1].git_clone_config.platform_review.enabled).to.equal(
      true
    );
    expect(q(element, '[data-saved]')).to.exist;
  });

  it('offers off, automatic and manual where the switch was', async () => {
    const element = await mount();
    const group = q(element, '[data-field="mode"]');
    expect(group.tagName.toLowerCase()).to.equal('sl-radio-group');
    expect(
      [...group.querySelectorAll('sl-radio')].map((r: any) => r.value)
    ).to.deep.equal(['off', 'automatic', 'manual']);
    expect(group.value).to.equal('off');
  });

  it('saves manual mode and shows it', async () => {
    const element = await mount();
    await chooseMode(element, 'manual');
    expect(q(element, '[data-field="mode"]').helpText).to.contain(
      'publish endpoint'
    );
    const statusOn = q(element, '[data-field="status_on"]');
    statusOn.checked = true;
    statusOn.dispatchEvent(new Event('sl-change'));
    await element.updateComplete;
    q(element, '[data-action="save"]').click();
    await new Promise((resolve) => setTimeout(resolve, 20));
    await element.updateComplete;
    expect(putBodies[0].git_clone_config.platform_review).to.deep.equal({
      enabled: true,
      mode: 'manual',
      status: {
        context: 'preloop/review',
        verdict_states: { pass: 'success', fail: 'failure' },
      },
    });
    expect(q(element, 'sl-badge').textContent.trim()).to.equal('Manual');
  });

  it('keeps a stored manual mode when another setting changes', async () => {
    const element = await mount({
      ...FLOW,
      git_clone_config: {
        ...FLOW.git_clone_config,
        platform_review: {
          enabled: true,
          mode: 'manual',
          status: { verdict_states: { pass: 'success', fail: 'failure' } },
        },
      },
    });
    expect(q(element, '[data-field="mode"]').value).to.equal('manual');
    expect(q(element, '[data-action="save"]').disabled).to.equal(true);
    const fail = q(element, '[data-verdict="fail"]');
    fail.value = 'error';
    fail.dispatchEvent(new Event('sl-change'));
    await element.updateComplete;
    q(element, '[data-action="save"]').click();
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(putBodies[0].git_clone_config.platform_review.mode).to.equal(
      'manual'
    );
  });

  it('turns posting off from manual and treats a stored automatic mode as unchanged', async () => {
    const stored = {
      ...FLOW,
      git_clone_config: {
        ...FLOW.git_clone_config,
        platform_review: {
          enabled: true,
          mode: 'automatic',
          status: { verdict_states: { pass: 'success', fail: 'failure' } },
        },
      },
    };
    const element = await mount(stored);
    expect(q(element, '[data-field="mode"]').value).to.equal('automatic');
    // The server echoes mode: automatic; that is the same as no mode.
    expect(q(element, '[data-action="save"]').disabled).to.equal(true);
    await chooseMode(element, 'off');
    q(element, '[data-action="save"]').click();
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(putBodies[0].git_clone_config.platform_review.enabled).to.equal(
      false
    );
    expect(putBodies[0].git_clone_config.platform_review).to.not.have.property(
      'mode'
    );
  });

  it('blocks a save the server would refuse and says why', async () => {
    const element = await mount({ ...FLOW, validate_result: false });
    await chooseMode(element, 'automatic');
    expect(q(element, '[data-blocker]').textContent).to.contain(
      'Validate result is off'
    );
    expect(q(element, '[data-action="save"]').disabled).to.equal(true);
  });

  it('shows the server refusal as an error', async () => {
    const element = await mount();
    await chooseMode(element, 'automatic');
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
    await chooseMode(element, 'automatic');
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
    expect(q(element, '[data-field="mode"]')).to.equal(null);
    expect(q(element, '[data-mode]').textContent).to.contain(
      'Posted when each run finishes'
    );
    expect(q(element, '[data-action="save"]')).to.equal(null);
    expect(q(element, '[data-mapping]').textContent).to.contain('failure');
    expect(q(element, 'sl-badge').textContent.trim()).to.equal('Automatic');
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

  it('says a pull request event starts the run when the trigger types allow it', async () => {
    const element = await mount({
      ...FLOW,
      trigger_event_types: ['pull_request_opened', 'pull_request_updated'],
    });
    expect(q(element, '[data-event-review]').textContent).to.contain(
      'No CI identity is required'
    );
    const plain = await mount(FLOW);
    expect(q(plain, '[data-event-review]')).to.equal(null);
  });

  it('states how each declared finding field is shown and warns when none is text', async () => {
    const shown = await mount({
      ...FLOW,
      result_schema: {
        type: 'object',
        properties: {
          verdict: { type: 'string', enum: ['pass', 'fail'] },
          findings: {
            type: 'array',
            items: {
              type: 'object',
              properties: {
                severity: { type: 'string' },
                problem: { type: 'string' },
                evidence: { type: 'string' },
                location: { type: 'string' },
              },
            },
          },
        },
      },
    });
    const text = shown.shadowRoot!.textContent!.replace(/\s+/g, ' ');
    expect(text).to.contain('problem text');
    expect(text).to.contain('evidence labelled block');
    expect(text).to.contain('location location');
    expect(q(shown, '[data-finding-text]')).to.equal(null);

    const missing = await mount({
      ...FLOW,
      result_schema: {
        type: 'object',
        properties: {
          verdict: { type: 'string', enum: ['pass', 'fail'] },
          findings: {
            type: 'array',
            items: {
              type: 'object',
              properties: { severity: { type: 'string' }, meta: {} },
            },
          },
        },
      },
    });
    expect(q(missing, '[data-finding-text]').textContent).to.contain(
      'no text field'
    );
    expect(q(missing, '[data-action="save"]')).to.exist;
  });

  it('reads finding fields wrapped in allOf and does not warn without an item schema', async () => {
    const wrapped = await mount({
      ...FLOW,
      result_schema: {
        type: 'object',
        properties: {
          verdict: { type: 'string', enum: ['pass', 'fail'] },
          findings: {
            type: 'array',
            items: {
              allOf: [
                {
                  type: 'object',
                  properties: { problem: { type: 'string' } },
                },
              ],
            },
          },
        },
      },
    });
    expect(wrapped.shadowRoot!.textContent).to.contain('problem text');
    expect(q(wrapped, '[data-finding-text]')).to.equal(null);

    const bare = await mount({
      ...FLOW,
      result_schema: {
        type: 'object',
        properties: {
          verdict: { type: 'string', enum: ['pass', 'fail'] },
          findings: { type: 'array' },
        },
      },
    });
    expect(q(bare, '[data-finding-text]')).to.equal(null);
    expect(bare.shadowRoot!.textContent).to.contain('does not declare');
  });

  it('offers the three evidence levels and saves the one that tightens posting', async () => {
    const element = await mount();
    await chooseMode(element, 'automatic');
    const select = q(element, '[data-field="evidence"]');
    const values = [...select.querySelectorAll('sl-option')].map(
      (option: Element) => option.getAttribute('value')
    );
    expect(values).to.deep.equal(['any', 'captured', 'complete']);
    expect(select.helpText).to.contain(
      "whether or not the run's tool evidence"
    );
    select.value = 'captured';
    select.dispatchEvent(new Event('sl-change'));
    await element.updateComplete;
    expect(q(element, '[data-field="evidence"]').helpText).to.contain(
      'masked credentials'
    );
    q(element, '[data-action="save"]').click();
    await new Promise((resolve) => setTimeout(resolve, 20));
    await element.updateComplete;
    expect(putBodies[0].git_clone_config.platform_review.evidence).to.equal(
      'captured'
    );
    expect(putBodies[0].git_clone_config.platform_review).to.not.have.property(
      'status'
    );
  });

  it('keeps a stored project list across a save and shows it read-only', async () => {
    const scoped = {
      ...FLOW,
      trigger_project_ids: ['p1', 'p2', 'p3'],
      git_clone_config: {
        ...FLOW.git_clone_config,
        repositories: [
          { tracker_id: 't1', project_id: 'p1', identifier: 'acme/api' },
          { tracker_id: 't1', project_id: 'p2', identifier: 'acme/web' },
          { tracker_id: 't1', project_id: 'p3', identifier: 'acme/docs' },
        ],
        platform_review: { enabled: true, project_ids: ['p2'] },
      },
    };
    const element = await mount(scoped);
    const note = q(element, '[data-posting-projects]');
    expect(note.textContent).to.contain('Posts for 1 of 3 repositories');
    expect(note.textContent).to.contain('acme/web');
    expect(note.textContent).to.not.contain('acme/api');
    const select = q(element, '[data-field="evidence"]');
    select.value = 'captured';
    select.dispatchEvent(new Event('sl-change'));
    await element.updateComplete;
    q(element, '[data-action="save"]').click();
    await new Promise((resolve) => setTimeout(resolve, 20));
    await element.updateComplete;
    const saved = putBodies[0].git_clone_config.platform_review;
    expect(saved.project_ids).to.deep.equal(['p2']);
    expect(saved.evidence).to.equal('captured');
  });

  it('drops the project list when posting is turned off', async () => {
    const element = await mount({
      ...FLOW,
      git_clone_config: {
        ...FLOW.git_clone_config,
        platform_review: { enabled: true, project_ids: ['p1'] },
      },
    });
    await chooseMode(element, 'off');
    q(element, '[data-action="save"]').click();
    await new Promise((resolve) => setTimeout(resolve, 20));
    await element.updateComplete;
    const saved = putBodies[0].git_clone_config.platform_review;
    expect(saved.enabled).to.equal(false);
    expect(saved).to.not.have.property('project_ids');
  });

  it('does not mention other repositories when the list covers them all', async () => {
    const element = await mount({
      ...FLOW,
      trigger_project_ids: ['p1'],
      git_clone_config: {
        ...FLOW.git_clone_config,
        repositories: [
          { tracker_id: 't1', project_id: 'p1', identifier: 'acme/api' },
        ],
        platform_review: { enabled: true, project_ids: ['p1'] },
      },
    });
    const note = q(element, '[data-posting-projects]');
    expect(note.textContent).to.contain('Posts for 1 of 1 repository');
    expect(note.textContent).to.not.contain('Other repositories');
  });
  describe('posting projects editor', () => {
    const repos = (n: number) =>
      Array.from({ length: n }, (_, i) => ({
        tracker_id: 't1',
        project_id: `p${i + 1}`,
        identifier: `acme/repo-${i + 1}`,
      }));
    const scopedFlow = (block: any, n = 3, extra: any = {}) => ({
      ...FLOW,
      trigger_project_ids: repos(n).map((r) => r.project_id),
      ...extra,
      git_clone_config: {
        ...FLOW.git_clone_config,
        repositories: repos(n),
        ...(block ? { platform_review: block } : {}),
      },
    });
    const ON = { enabled: true };
    const save = async (element: FlowPlatformReviewCard): Promise<any> => {
      q(element, '[data-action="save"]').click();
      await new Promise((resolve) => setTimeout(resolve, 20));
      await element.updateComplete;
      return putBodies[putBodies.length - 1].git_clone_config.platform_review;
    };
    const chooseScope = async (
      element: FlowPlatformReviewCard,
      scope: 'all' | 'selected'
    ): Promise<void> => {
      const group = q(element, '[data-field="project_scope"]');
      group.value = scope;
      group.dispatchEvent(new Event('sl-change'));
      await element.updateComplete;
    };
    const tick = async (
      element: FlowPlatformReviewCard,
      id: string,
      checked: boolean
    ): Promise<void> => {
      const box = q(element, `[data-project="${id}"]`);
      box.checked = checked;
      box.dispatchEvent(new Event('sl-change'));
      await element.updateComplete;
    };
    const ticked = (element: FlowPlatformReviewCard): string[] =>
      [...element.shadowRoot!.querySelectorAll('[data-project]')]
        .filter((box: any) => box.checked)
        .map((box: any) => box.dataset.project);

    it('"every repository" stores no project_ids', async () => {
      const element = await mount(scopedFlow(null));
      await chooseMode(element, 'automatic');
      expect(q(element, '[data-field="project_scope"]').value).to.equal('all');
      expect(q(element, '[data-project="p1"]')).to.equal(null);
      const saved = await save(element);
      expect(saved).to.deep.equal({ enabled: true });
    });

    it('"only selected" with two ticked stores those two', async () => {
      const element = await mount(scopedFlow(ON));
      await chooseScope(element, 'selected');
      await tick(element, 'p2', false);
      expect(ticked(element)).to.deep.equal(['p1', 'p3']);
      expect(q(element, '[data-project="p1"]').textContent).to.contain(
        'acme/repo-1'
      );
      expect(q(element, '[data-posting-projects]').textContent).to.contain(
        'Posts for 2 of 3 repositories'
      );
      const saved = await save(element);
      expect(saved.project_ids).to.deep.equal(['p1', 'p3']);
    });

    it('none ticked stores [] and says nothing will be posted', async () => {
      const element = await mount(scopedFlow(ON));
      await chooseScope(element, 'selected');
      for (const id of ['p1', 'p2', 'p3']) await tick(element, id, false);
      const none = q(element, '[data-posting-none]');
      const noneText = none.textContent.replace(/\s+/g, ' ');
      expect(noneText).to.contain('Nothing will be posted');
      expect(noneText).to.contain('no pull request event will start a review');
      expect(q(element, '[data-posting-projects]').textContent).to.contain(
        'Posts for 0 of 3 repositories'
      );
      const saved = await save(element);
      expect(saved.project_ids).to.deep.equal([]);
    });

    it('preselects a stored list and reset restores it', async () => {
      const element = await mount(scopedFlow({ ...ON, project_ids: ['p2'] }));
      expect(q(element, '[data-field="project_scope"]').value).to.equal(
        'selected'
      );
      expect(ticked(element)).to.deep.equal(['p2']);
      await tick(element, 'p3', true);
      expect(q(element, '[data-action="save"]').disabled).to.equal(false);
      q(element, '[data-action="reset"]').click();
      await element.updateComplete;
      expect(ticked(element)).to.deep.equal(['p2']);
      expect(q(element, '[data-action="save"]').disabled).to.equal(true);
    });

    it('switching back to every repository removes the key', async () => {
      const element = await mount(scopedFlow({ ...ON, project_ids: ['p2'] }));
      await chooseScope(element, 'all');
      const saved = await save(element);
      expect(saved).to.not.have.property('project_ids');
      expect(saved.enabled).to.equal(true);
    });

    it('turning posting off with a stored list sends no list and says so', async () => {
      const element = await mount(scopedFlow({ ...ON, project_ids: ['p2'] }));
      expect(q(element, '[data-posting-projects-cleared]')).to.equal(null);
      await chooseMode(element, 'off');
      expect(
        q(element, '[data-posting-projects-cleared]').textContent
      ).to.contain('clears the list');
      expect(q(element, '[data-field="project_scope"]')).to.equal(null);
      const saved = await save(element);
      expect(saved).to.deep.equal({ enabled: false });
    });

    it('is disabled for a user who may not edit the flow', async () => {
      const element = await mount(
        scopedFlow({ ...ON, project_ids: ['p2'] }),
        false
      );
      const radios = [
        ...q(element, '[data-field="project_scope"]').querySelectorAll(
          'sl-radio'
        ),
      ];
      expect(radios.length).to.equal(2);
      expect(radios.every((radio: any) => radio.disabled)).to.equal(true);
      const boxes = [...element.shadowRoot!.querySelectorAll('[data-project]')];
      expect(boxes.length).to.equal(3);
      expect(boxes.every((box: any) => box.disabled)).to.equal(true);
    });

    it('shows no editor for a flow with one project', async () => {
      const element = await mount(scopedFlow(ON, 1));
      expect(q(element, '[data-field="project_scope"]')).to.equal(null);
      expect(q(element, '[data-field="project_ids"]')).to.equal(null);
    });

    it('shows the event sentence only with pull request event triggers', async () => {
      const plain = await mount(scopedFlow({ ...ON, project_ids: ['p1'] }));
      const help = q(plain, '[data-posting-projects-help]').textContent;
      expect(help).to.contain('Preloop posts only for the selected');
      expect(help).to.contain('added to the flow later is not selected');
      expect(help).to.not.contain('pull request events');
      const events = await mount(
        scopedFlow({ ...ON, project_ids: ['p1'] }, 3, {
          trigger_event_types: ['pull_request_opened'],
        })
      );
      expect(q(events, '[data-posting-projects-help]').textContent).to.contain(
        'Pull request events start reviews only for the selected repositories'
      );
    });

    it('flags a stored id that is not on the flow and does not send it', async () => {
      const element = await mount(
        scopedFlow({ ...ON, project_ids: ['p2', 'gone'] })
      );
      expect(q(element, '[data-stale-projects]').textContent).to.contain(
        'gone'
      );
      expect(q(element, '[data-stale-projects]').textContent).to.contain(
        'Not on this flow'
      );
      expect(ticked(element)).to.deep.equal(['p2']);
      // Not dirty on load: nothing to discard until something changes.
      expect(q(element, '[data-action="reset"]').disabled).to.equal(true);
      await tick(element, 'p3', true);
      q(element, '[data-action="reset"]').click();
      await element.updateComplete;
      expect(q(element, '[data-action="reset"]').disabled).to.equal(true);
      await tick(element, 'p1', true);
      expect(q(element, '[data-blocker]')).to.equal(null);
      const saved = await save(element);
      expect(saved.project_ids).to.deep.equal(['p1', 'p2']);
    });

    it('offers no editor on a flow open to every project', async () => {
      const element = await mount(
        scopedFlow(ON, 3, { trigger_project_ids: null })
      );
      expect(q(element, '[data-field="project_scope"]')).to.equal(null);
    });

    it('shows the editor on a one-project flow with a stored empty list', async () => {
      const element = await mount(scopedFlow({ ...ON, project_ids: [] }, 1));
      expect(q(element, '[data-field="project_scope"]').value).to.equal(
        'selected'
      );
      expect(q(element, '[data-posting-none]')).to.exist;
      await tick(element, 'p1', true);
      const saved = await save(element);
      expect(saved.project_ids).to.deep.equal(['p1']);
    });

    it('handles 45 repositories with a filter and a bounded list', async () => {
      const element = await mount(
        scopedFlow({ ...ON, project_ids: ['p3', 'p40'] }, 45)
      );
      const list = q(element, '[data-field="project_ids"]');
      expect(getComputedStyle(list).overflowY).to.equal('auto');
      expect(list.clientHeight).to.be.lessThan(list.scrollHeight);
      const filter = q(element, '[data-field="project_filter"]');
      filter.value = 'repo-4';
      filter.dispatchEvent(new Event('sl-input'));
      await element.updateComplete;
      const shown = [
        ...element.shadowRoot!.querySelectorAll('[data-project]'),
      ].map((box: any) => box.dataset.project);
      expect(shown).to.deep.equal([
        'p4',
        'p40',
        'p41',
        'p42',
        'p43',
        'p44',
        'p45',
      ]);
      await tick(element, 'p41', true);
      filter.value = '';
      filter.dispatchEvent(new Event('sl-input'));
      await element.updateComplete;
      const saved = await save(element);
      expect(saved.project_ids).to.deep.equal(['p3', 'p40', 'p41']);
    });
  });
});
