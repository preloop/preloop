import { expect } from '@open-wc/testing';
import { FleetStory, codeLineLabel, workUrl } from './fleet-map-story';
import { sampleFleetMap } from '../test-helpers/fleet-map-graph';

const issue = {
  kind: 'issue',
  key: 'example/widgets#41',
  repo: 'example/widgets',
  reference: '#41',
  title: 'Handle retries',
  url: 'https://example.com/example/widgets/issues/41',
};
const pr = {
  kind: 'pull_request',
  key: 'example/widgets#51',
  repo: 'example/widgets',
  reference: '#51',
  title: 'Fix retries',
  url: 'https://example.com/example/widgets/pull/51',
};
const start = (execution = 'run-1', status = 'RUNNING', subject = issue) => ({
  type: 'status_update',
  flow_id: 'triage',
  execution_id: execution,
  payload: {
    status,
    fleet_context: {
      flow_name: 'Issue triage',
      trigger_kind:
        subject.kind === 'issue' ? 'issue_opened' : 'pull_request_opened',
      subject,
    },
  },
});
const code = (action: string, rest: Record<string, unknown> = {}) => ({
  type: 'code_activity',
  payload: { action, ...rest },
});

describe('Fleet Map observed work story', () => {
  it('projects current flow context, folds concurrent runs and fades 60s after the last terminal run', () => {
    const story = new FleetStory(),
      base = sampleFleetMap();
    story.record('flow_executions', start(), 1000);
    story.record('flow_executions', start('run-2'), 1100);
    let graph = story.graph(base, null, 1200);
    expect(graph.nodes.find((n) => n.id === 'flow:triage')?.status).to.equal(
      '2 running'
    );
    expect(graph.nodes.find((n) => n.type === 'work')?.label).to.contain('#41');
    story.record('flow_executions', start('run-1', 'COMPLETED'), 2000);
    expect(
      story.graph(base, null, 62_100).nodes.find((n) => n.id === 'flow:triage')
        ?.status
    ).to.equal('RUNNING');
    story.record('flow_executions', start('run-2', 'COMPLETED'), 63_000);
    expect(
      story.graph(base, null, 122_999).nodes.some((n) => n.id === 'flow:triage')
    ).to.equal(true);
    expect(
      story.graph(base, null, 123_000).nodes.some((n) => n.id === 'flow:triage')
    ).to.equal(false);
  });
  it('renders an observed issue, attached PR, review verdict and merge without inventing a merger', () => {
    const story = new FleetStory(),
      base = sampleFleetMap();
    story.record(
      'code_activity',
      code('issue_opened', { subject: issue, managed_agent_id: 'claude' }),
      1000
    );
    story.record(
      'code_activity',
      code('opened', {
        subject: pr,
        related_issue: issue,
        flow_id: 'implementation',
      }),
      2000
    );
    const notes = story.record(
      'code_activity',
      code('reviewed', {
        subject: pr,
        review_state: 'changes_requested',
        flow_id: 'reviewer',
      }),
      3000
    );
    expect(notes[0].label).to.contain('changes requested');
    const graph = story.graph(base, null, 3000),
      work = graph.nodes.filter((n) => n.type === 'work');
    const child = work.find((n) => n.work_kind === 'pull_request')!;
    expect(child.parent_id).to.equal(
      work.find((n) => n.work_kind === 'issue')?.id
    );
    expect(child.review_state).to.equal('changes_requested');
    story.record(
      'code_activity',
      code('merged', {
        pr: 'example/widgets#51',
        url: pr.url,
        flow_ids: ['implementation'],
      }),
      4000
    );
    const merged = story.graph(base, null, 5000);
    expect(merged.nodes.find((n) => n.id === child.id)?.status).to.equal(
      'Merged'
    );
    expect(
      merged.edges.some(
        (e) => e.kind === 'caused' && e.source.startsWith('user:')
      )
    ).to.equal(false);
    expect(
      story.graph(base, null, 124_000).nodes.filter((n) => n.type === 'work')
    ).to.have.length(0);
  });
  it('lights real object associations for six seconds and retains their hairlines', () => {
    const story = new FleetStory(),
      base = sampleFleetMap();
    story.record(
      'code_activity',
      code('issue_opened', { subject: issue, managed_agent_id: 'claude' }),
      1000
    );
    expect(story.activeEdges(6999).size).to.equal(1);
    expect(story.activeEdges(7000).size).to.equal(0);
    expect(
      story.graph(base, null, 8000).edges.filter((e) => e.kind === 'caused')
    ).to.have.length(1);
  });
  it('keeps issue and PR namespaces apart and rejects ambiguous number-only subjects', () => {
    const story = new FleetStory(),
      base = sampleFleetMap();
    story.record(
      'code_activity',
      code('issue_opened', { subject: issue }),
      1000
    );
    story.record(
      'code_activity',
      code('opened', { subject: { ...pr, key: issue.key, reference: '#41' } }),
      1000
    );
    story.record(
      'code_activity',
      code('issue_opened', { subject: { kind: 'issue', reference: '#41' } }),
      1000
    );
    expect(
      story.graph(base, null, 1000).nodes.filter((n) => n.type === 'work')
    ).to.have.length(2);
  });
  it('degrades old flow events to a named run with no invented work chip', () => {
    const story = new FleetStory();
    const notes = story.record(
      'flow_executions',
      {
        type: 'execution_started',
        flow_id: 'triage',
        execution_id: 'r',
        payload: {
          status: 'PENDING',
          flow_name: 'Issue triage',
          resolved_input_prompt: 'PRIVATE',
        },
      },
      1000
    );
    expect(notes[0].label).to.equal('Issue triage started');
    expect(
      story
        .graph(sampleFleetMap(), null, 1000)
        .nodes.filter((n) => n.type === 'work')
    ).to.have.length(0);
    expect(JSON.stringify(notes)).not.to.contain('PRIVATE');
  });
  it('deduplicates stable events and never resurrects a completed run from a late start', () => {
    const story = new FleetStory();
    const event = code('opened', {
      subject: pr,
      event_id: 'event-1',
      managed_agent_id: 'codex',
    });
    expect(story.record('code_activity', event, 1000)).to.have.length(1);
    expect(story.record('code_activity', event, 2000)).to.have.length(0);
    story.record('flow_executions', start('r', 'COMPLETED'), 2000);
    expect(story.record('flow_executions', start('r'), 3000)).to.have.length(0);
  });
  it('strips credential URL components and rejects executable links', () => {
    expect(workUrl('javascript:alert(1)')).to.equal('');
    expect(
      workUrl('https://user:secret@example.com/issues/41?token=secret#x')
    ).to.equal('https://example.com/issues/41');
  });

  for (const explicitKey of [true, false])
    it(`enriches an existing work reference with a late URL (${explicitKey ? 'explicit key' : 'repo/reference'})`, () => {
      const story = new FleetStory();
      const subject = { ...issue, key: explicitKey ? issue.key : undefined };
      story.record(
        'code_activity',
        code('issue_opened', { subject: { ...subject, url: undefined } }),
        1000
      );
      const original = story
        .graph(sampleFleetMap(), null, 1000)
        .nodes.find((n) => n.type === 'work')!;
      expect(original.href).to.equal('');
      story.record('code_activity', code('issue_updated', { subject }), 2000);
      const work = story
        .graph(sampleFleetMap(), null, 2000)
        .nodes.filter((n) => n.type === 'work');
      expect(work).to.have.length(1);
      expect(work[0].id).to.equal(original.id);
      expect(work[0].href).to.equal(issue.url);
    });
});

describe('Fleet Map current publication and repair metadata', () => {
  it('attaches the controlled publication PR URL to its recorded trigger issue', () => {
    const story = new FleetStory();
    story.record('flow_executions', start('implementation'), 1000);
    const notes = story.record(
      'flow_executions',
      {
        ...start('implementation', 'COMPLETED'),
        payload: {
          ...start().payload,
          status: 'COMPLETED',
          result: { pr_url: pr.url, output: 'PRIVATE OUTPUT' },
        },
      },
      2000
    );
    const work = story
      .graph(sampleFleetMap(), null, 2000)
      .nodes.filter((n) => n.type === 'work');
    const published = work.find((n) => n.work_kind === 'pull_request')!;
    expect(published.parent_id).to.equal(
      work.find((n) => n.work_kind === 'issue')?.id
    );
    expect(notes.some((n) => n.label === 'Published PR #51')).to.equal(true);
    expect(JSON.stringify(work) + JSON.stringify(notes)).not.to.contain(
      'PRIVATE OUTPUT'
    );
  });
  it('labels structured review verdicts as reported and uses the actual repair PR URL', () => {
    const story = new FleetStory();
    story.record('flow_executions', start('review', 'RUNNING', pr), 1000);
    const event = {
      ...start('review', 'COMPLETED', pr),
      payload: {
        ...start('review', 'COMPLETED', pr).payload,
        result: {
          review_posted: true,
          review_action: 'request_changes',
          findings: [{ body: 'PRIVATE' }],
        },
      },
    };
    const notes = story.record('flow_executions', event, 2000);
    expect(
      notes.some((n) =>
        n.label.includes('Reported review #51  changes requested')
      )
    ).to.equal(true);
    expect(
      story
        .graph(sampleFleetMap(), null, 2000)
        .nodes.find((n) => n.work_kind === 'pull_request')?.review_state
    ).to.equal('Reported changes requested');
    story.record(
      'flow_executions',
      {
        type: 'execution_started',
        flow_id: 'implementation',
        execution_id: 'repair',
        payload: {
          status: 'RUNNING',
          fleet_context: {
            flow_name: 'Implementation',
            trigger_kind: 'implementation_feedback',
            resume_pr_url: pr.url,
            resume_from_execution_id: 'review',
          },
        },
      },
      3000
    );
    const graph = story.graph(sampleFleetMap(), null, 3000);
    expect(
      graph.edges.some(
        (e) =>
          e.kind === 'caused' &&
          e.target === 'flow:implementation' &&
          e.source.startsWith('work:')
      )
    ).to.equal(true);
    expect(story.flowForExecution('repair')).to.equal('implementation');
    expect(JSON.stringify(notes)).not.to.contain('PRIVATE');
  });
});

describe('fleet-map work line labels', () => {
  it('says the action that created the work, never a kind', () => {
    const issue = { work_kind: 'issue' as const, review_state: null };
    const pr = { work_kind: 'pull_request' as const, review_state: null };
    expect(codeLineLabel('issue_opened', '#41', {}, issue)).to.equal(
      'opened issue #41'
    );
    expect(
      codeLineLabel(
        'issue_labeled',
        '#41',
        { labels_added: ['bug', 'p2'] },
        issue
      )
    ).to.equal('labelled bug, p2');
    expect(codeLineLabel('opened', '#51', {}, pr)).to.equal('opened PR #51');
    expect(
      codeLineLabel(
        'reviewed',
        '#51',
        { review_state: 'changes_requested' },
        pr
      )
    ).to.equal('review: changes requested');
    expect(codeLineLabel('pull_request_updated', '#51', {}, pr)).to.equal(
      'updated PR #51'
    );
    expect(codeLineLabel('merged', '#51', {}, pr)).to.equal('merged PR #51');
    expect(codeLineLabel('something_else', '#51', {}, pr)).to.equal('');
  });
});

describe('fleet-map window history seeding', () => {
  const merged = (pr: string, url: string | null) => ({
    id: `merge:${pr}`,
    at: new Date(Date.now() - 60_000).toISOString(),
    action: 'merged' as const,
    node_id: 'flow:implementation',
    pr,
    url,
  });
  const base = {
    active_within_minutes: 60,
    since: new Date(Date.now() - 3_600_000).toISOString(),
    truncated: false,
    counts: {},
    flows: [],
  };
  const works = (story: FleetStory) =>
    story
      .graph({ ...sampleFleetMap() }, 60)
      .nodes.filter((n) => n.type === 'work');

  it('seeds merged PRs from forge merges on a server without the work list', () => {
    const story = new FleetStory();
    expect(
      story.seedWork({ ...base, events: [merged('acme/api#51', null)] })
    ).to.equal(true);
    const nodes = works(story);
    expect(nodes.map((n) => [n.work_kind, n.status])).to.deep.equal([
      ['pull_request', 'Merged'],
    ]);
  });

  it('does not add a merge event PR twice when the work list has it', () => {
    const story = new FleetStory();
    story.seedWork({
      ...base,
      events: [merged('#51', null)],
      work: [
        {
          key: 'acme/api#51',
          kind: 'pull_request',
          reference: '#51',
          repo: 'acme/api',
          title: 'Retry',
          url: null,
          state: 'merged',
          last_action: 'merged',
          last_at: new Date(Date.now() - 60_000).toISOString(),
          review_state: null,
          labels: [],
          related_issue: null,
          node_ids: [],
        },
      ],
    });
    expect(works(story)).to.have.length(1);
  });

  it('maps recorded PR states to capitalized work node statuses', () => {
    const story = new FleetStory();
    const states = ['open', 'closed', 'merged', null] as const;
    story.seedWork({
      ...base,
      events: [],
      work: states.map((state, index) => ({
        key: `example/widgets#${index + 1}`,
        kind: 'pull_request' as const,
        reference: `#${index + 1}`,
        repo: 'example/widgets',
        title: null,
        url: null,
        state,
        last_action: 'opened' as const,
        last_at: new Date(Date.now() - 60_000).toISOString(),
        review_state: null,
        labels: [],
        related_issue: null,
        node_ids: [],
      })),
    });

    expect(
      works(story).map((node) => [node.resource_id, node.status])
    ).to.deep.equal([
      ['example/widgets#1', 'Open'],
      ['example/widgets#2', 'Closed'],
      ['example/widgets#3', 'Merged'],
      ['example/widgets#4', null],
    ]);
  });
});
