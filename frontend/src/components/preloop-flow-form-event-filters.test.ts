import {
  expect,
  fixture,
  fixtureCleanup,
  html,
  oneEvent,
} from '@open-wc/testing';
import sinon, { SinonSandbox } from 'sinon';
import './preloop-flow-form.ts';
import type { PreloopFlowForm } from './preloop-flow-form';

/**
 * Tests for the restored Event filters (optional) section in the
 * Create/Edit Flow form component (preloop-flow-form.ts).
 *
 * The source-substring block below is a coarse guard that every filter control
 * is still wired to its trigger_config key. The behaviour block that follows it
 * is what actually verifies the gating, the value bindings and the submitted
 * payload.
 */

let source: string;

before(async () => {
  const res = await fetch(
    new URL('./preloop-flow-form.ts', import.meta.url).href
  );
  expect(res.ok).to.be.true;
  source = await res.text();
});

describe('PreloopFlowForm event filters section', () => {
  it('declares filtersExpanded state property', () => {
    expect(source).to.include('filtersExpanded');
  });

  it('contains renderEventFilters method', () => {
    expect(source).to.include('renderEventFilters()');
  });

  it('renders Add filters button when collapsed', () => {
    expect(source).to.include('Add filters');
  });

  it('renders Hide filters button when expanded', () => {
    expect(source).to.include('Hide filters');
  });

  it('renders Event filters (optional) heading', () => {
    expect(source).to.include('Event filters (optional)');
  });

  it('renders the Created by filter input', () => {
    expect(source).to.include('Created by (username)');
    expect(source).to.include('trigger_config.author');
  });

  it('renders assignee filter input', () => {
    expect(source).to.include('Assigned to (username)');
    expect(source).to.include('trigger_config.assignee');
  });

  it('renders reviewer filter gated on MR/PR events', () => {
    expect(source).to.include('trigger_config.reviewer');
    expect(source).to.include('Reviewer (username)');
    expect(source).to.include('Requested reviewer (username)');
  });

  it('renders labels filter as comma-separated input', () => {
    expect(source).to.include('Labels (comma-separated)');
    expect(source).to.include('trigger_config.labels');
  });

  it('renders milestone filter for non-Jira trackers', () => {
    expect(source).to.include('trigger_config.milestone');
  });

  it('renders priority select for Jira trackers', () => {
    expect(source).to.include('trigger_config.priority');
    expect(source).to.include('Any priority');
  });

  it('renders issue type filter for Jira trackers', () => {
    expect(source).to.include('trigger_config.issue_type');
  });

  it('renders the moved-to-status filter for Jira trackers', () => {
    expect(source).to.include('trigger_config.status_to');
    expect(source).to.include('Moved to status');
  });

  it('renders merged and draft checkbox filters for PR/MR events', () => {
    expect(source).to.include('trigger_config.merged');
    expect(source).to.include('trigger_config.draft');
    expect(source).to.include('Only when marked as ready (not draft)');
  });

  it('renders GitLab-specific detailed_merge_status and state filters', () => {
    expect(source).to.include('trigger_config.detailed_merge_status');
    expect(source).to.include('Only when approved');
    expect(source).to.include('Merge status');
  });

  it('renders GitHub-specific state and mergeable_state filters', () => {
    expect(source).to.include('Pull request state');
    expect(source).to.include('trigger_config.mergeable_state');
    expect(source).to.include('Mergeable state');
  });

  it('renders Bitbucket-specific state filter with its own vocabulary', () => {
    expect(source).to.include('Filter by Bitbucket pull request state');
    expect(source).to.include('Declined');
    expect(source).to.include('Superseded');
    expect(source).to.include('changeRequestNoun(tracker.tracker_type)');
  });

  it('renders filter semantics help alert', () => {
    expect(source).to.include('How filters work:');
    expect(source).to.include('ALL conditions must');
  });

  it('includes trigger_config in the submit payload via conditional spread', () => {
    // The bundler may compile `!== undefined` to `!== void 0`
    const hasTriggerConfigGuard =
      source.includes('trigger_config !== undefined') ||
      source.includes('trigger_config !== void 0');
    expect(hasTriggerConfigGuard).to.be.true;
    expect(source).to.include('trigger_config: this.flow.trigger_config');
  });

  it('calls renderEventFilters gated on trigger_event_source', () => {
    expect(source).to.include(
      'this.flow.trigger_event_source ? this.renderEventFilters()'
    );
  });
});

const GITHUB_TRACKER = {
  id: 'tracker-github',
  name: 'GitHub',
  tracker_type: 'github',
};
const GITLAB_TRACKER = {
  id: 'tracker-gitlab',
  name: 'GitLab',
  tracker_type: 'gitlab',
};
const BITBUCKET_TRACKER = {
  id: 'tracker-bitbucket',
  name: 'Bitbucket',
  tracker_type: 'bitbucket',
};
const JIRA_TRACKER = {
  id: 'tracker-jira',
  name: 'Jira',
  tracker_type: 'jira',
};
const ORGANIZATION = {
  id: 'org-1',
  name: 'Acme',
  identifier: 'acme',
  tracker_id: GITHUB_TRACKER.id,
};
const PROJECT = {
  id: 'project-1',
  name: 'api',
  identifier: 'api',
  organization_id: ORGANIZATION.id,
};

/** A saved tracker flow that already carries event filters. */
function trackerFlow(trigger_config?: Record<string, unknown>) {
  return {
    id: 'flow-1',
    name: 'PR Reviewer',
    prompt_template: 'review',
    agent_type: 'codex',
    trigger_event_source: GITHUB_TRACKER.id,
    trigger_event_types: ['pull_request_opened'],
    trigger_organization_id: ORGANIZATION.id,
    ...(trigger_config ? { trigger_config } : {}),
  };
}

describe('PreloopFlowForm event filters behaviour', () => {
  let sandbox: SinonSandbox;

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
    sandbox = sinon.createSandbox();
    sandbox.stub(window, 'fetch').callsFake(async (url: any) => {
      const target = String(url);
      if (target.includes('/api/v1/trackers')) {
        return new Response(
          JSON.stringify([
            GITHUB_TRACKER,
            GITLAB_TRACKER,
            BITBUCKET_TRACKER,
            JIRA_TRACKER,
          ])
        );
      }
      if (target.includes('/api/v1/organizations')) {
        return new Response(JSON.stringify({ items: [ORGANIZATION] }));
      }
      if (target.includes('/api/v1/projects')) {
        return new Response(JSON.stringify([PROJECT]));
      }
      if (target.includes('/api/v1/agents')) {
        return new Response(JSON.stringify({ items: [] }));
      }
      return new Response(JSON.stringify([]));
    });
  });

  afterEach(() => {
    fixtureCleanup();
    sandbox.restore();
    localStorage.clear();
  });

  const mount = async (flow: Record<string, unknown>) => {
    const element = await fixture<PreloopFlowForm>(
      html`<preloop-flow-form .flow=${flow}></preloop-flow-form>`
    );
    // connectedCallback loads trackers/orgs/projects before the form renders.
    while ((element as any)._loadingReferenceData) {
      await new Promise((resolve) => setTimeout(resolve, 10));
    }
    await element.updateComplete;
    return element;
  };

  /** Submit the form and return the payload carried by flow-submit. */
  const submit = async (element: PreloopFlowForm) => {
    const submitted = oneEvent(element, 'flow-submit');
    void (element as any).handleFormSubmit(new Event('submit'));
    const event = await submitted;
    return event.detail.flow;
  };

  const filterInput = (element: PreloopFlowForm, label: string) =>
    element.shadowRoot!.querySelector(`sl-input[label="${label}"]`) as any;

  it('renders saved filters and submits them unchanged', async () => {
    const element = await mount(
      trackerFlow({ author: 'octocat', labels: ['bug'] })
    );

    expect(filterInput(element, 'Created by (username)').value).to.equal(
      'octocat'
    );
    expect(filterInput(element, 'Labels (comma-separated)').value).to.equal(
      'bug'
    );

    const payload = await submit(element);
    expect(payload.trigger_config).to.deep.equal({
      author: 'octocat',
      labels: ['bug'],
    });
  });

  it('records typed filters on the flow', async () => {
    const element = await mount(trackerFlow());
    (element as any).filtersExpanded = true;
    await element.updateComplete;

    const author = filterInput(element, 'Created by (username)');
    author.value = 'alice';
    author.dispatchEvent(new CustomEvent('sl-input'));

    const labels = filterInput(element, 'Labels (comma-separated)');
    labels.value = ' bug, critical ';
    labels.dispatchEvent(new CustomEvent('sl-input'));

    const payload = await submit(element);
    expect(payload.trigger_config).to.deep.equal({
      author: 'alice',
      labels: ['bug', 'critical'],
    });
  });

  it('round-trips the all-of labels filter', async () => {
    const element = await mount(
      trackerFlow({ labels: ['agent-ready'], labels_all: ['complexity:low'] })
    );
    (element as any).filtersExpanded = true;
    await element.updateComplete;

    const all = filterInput(
      element,
      'Issue must also carry all of these labels'
    );
    expect(all.value).to.equal('complexity:low');
    all.value = ' complexity:medium, risk:low ';
    all.dispatchEvent(new CustomEvent('sl-input'));

    let payload = await submit(element);
    expect(payload.trigger_config).to.deep.equal({
      labels: ['agent-ready'],
      labels_all: ['complexity:medium', 'risk:low'],
    });

    all.value = '';
    all.dispatchEvent(new CustomEvent('sl-input'));
    payload = await submit(element);
    expect(payload.trigger_config).to.deep.equal({ labels: ['agent-ready'] });
  });

  it('does not create trigger_config while rendering a flow without filters', async () => {
    const element = await mount(trackerFlow());

    expect(element.flow.trigger_config).to.equal(undefined);
    const payload = await submit(element);
    expect(payload).to.not.have.property('trigger_config');
  });

  it('clears filters when the trigger type moves away from a tracker', async () => {
    const element = await mount(trackerFlow({ author: 'octocat' }));

    (element as any).handleTriggerTypeChange('webhook');
    await element.updateComplete;

    // Explicit null so the backend clears the filters saved on the flow.
    expect(element.flow.trigger_config).to.equal(null);
    const payload = await submit(element);
    expect(payload.trigger_event_source).to.equal('webhook');
    expect(payload.trigger_config).to.equal(null);
  });

  it('clears filters when a different tracker is selected', async () => {
    const element = await mount(
      trackerFlow({ state: 'open', mergeable_state: 'clean' })
    );

    await (element as any).handleTrackerChange({
      target: { value: GITLAB_TRACKER.id },
    });
    await element.updateComplete;

    expect(element.flow.trigger_config).to.equal(null);
    // A tracker change clears the events, and a tracker trigger needs one.
    element.flow.trigger_event_types = ['merge_request_opened'];
    const payload = await submit(element);
    expect(payload.trigger_event_source).to.equal(GITLAB_TRACKER.id);
    expect(payload.trigger_config).to.equal(null);
  });

  it('shows Bitbucket vocabulary and pull request states for a Bitbucket tracker', async () => {
    const element = await mount({
      ...trackerFlow({ state: 'declined' }),
      trigger_event_source: BITBUCKET_TRACKER.id,
      trigger_organization_id: undefined,
    });
    (element as any).filtersExpanded = true;
    await element.updateComplete;

    const reviewer = filterInput(element, 'Reviewer (username)');
    expect(reviewer).to.not.equal(null);
    expect(reviewer.getAttribute('help-text')).to.include('request changes');
    expect(filterInput(element, 'Requested reviewer (username)')).to.equal(
      null
    );

    const state = element.shadowRoot!.querySelector(
      'sl-select[label="Pull request state"]'
    ) as any;
    expect(state).to.not.equal(null);
    expect(state.value).to.equal('declined');
    const options = Array.from(state.querySelectorAll('sl-option')).map(
      (option: any) => option.value
    );
    expect(options).to.deep.equal([
      '',
      'open',
      'merged',
      'declined',
      'superseded',
    ]);
    expect(
      element.shadowRoot!.querySelector('sl-select[label="Mergeable state"]')
    ).to.equal(null);
    expect(element.shadowRoot!.textContent!.replace(/\s+/g, ' ')).to.include(
      'Only when the pull request is merged'
    );

    const payload = await submit(element);
    expect(payload.trigger_config).to.deep.equal({ state: 'declined' });
  });

  it('keeps filters when the same tracker is re-selected', async () => {
    const element = await mount(trackerFlow({ author: 'octocat' }));

    await (element as any).handleTrackerChange({
      target: { value: GITHUB_TRACKER.id },
    });
    await element.updateComplete;

    expect(element.flow.trigger_config).to.deep.equal({ author: 'octocat' });
  });

  it('records the Jira moved-to-status filter as status_to', async () => {
    const element = await mount({
      ...trackerFlow(),
      trigger_event_source: JIRA_TRACKER.id,
      trigger_event_types: ['issue_status_changed'],
    });
    (element as any).filtersExpanded = true;
    await element.updateComplete;

    const status = filterInput(element, 'Moved to status');
    expect(status).to.exist;
    status.value = ' Ready for Dev ';
    status.dispatchEvent(new CustomEvent('sl-input'));

    const payload = await submit(element);
    expect(payload.trigger_config).to.deep.equal({
      status_to: 'Ready for Dev',
    });
  });

  it('does not offer the moved-to-status filter for code hosts', async () => {
    const element = await mount(trackerFlow());
    (element as any).filtersExpanded = true;
    await element.updateComplete;

    expect(filterInput(element, 'Moved to status')).to.equal(null);
  });
});
