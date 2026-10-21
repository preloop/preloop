import { expect } from '@open-wc/testing';
import sinon from 'sinon';
import {
  containers,
  getFleetCodeActivity,
  getFleetMap,
  getFleetUsage,
  laneTitle,
  layoutGraph,
  scopeToUsers,
  usersParam,
  type FleetMapGraph,
  type FleetMapNode,
} from './fleet-map-api';
import { sampleFleetMap } from './test-helpers/fleet-map-graph';
import { nodeMark, FLOW_ICON_SRC } from './utils/fleet-map-icons';
import { ARRANGEMENT_LABEL, parseArrangement } from './utils/fleet-map-words';
import { windowLabel } from './utils/fleet-map-activity';

const flow = (id: string, label: string): FleetMapNode => ({
  id,
  type: 'flow',
  resource_id: id.split(':')[1],
  label,
  detail: 'Flow',
  href: '',
  status: null,
  owner_ids: [],
  provider_ids: [],
  gaps: [],
  finding_count: 0,
  actions: [],
});

const withFlows = (): FleetMapGraph => {
  const graph = sampleFleetMap();
  return {
    ...graph,
    nodes: [...graph.nodes, flow('flow:a', 'Triage'), flow('flow:b', 'Review')],
  };
};

describe('Fleet Map pass 4: users scope (item 5)', () => {
  it('sends users= with resource ids on all three reads', async () => {
    localStorage.setItem('accessToken', 'test-token');
    const fetch = sinon.stub(window, 'fetch').callsFake(
      async () =>
        new Response(
          JSON.stringify({
            nodes: [],
            edges: [],
            clusters: [],
            counts: {},
            flows: [],
            events: [],
          })
        )
    );
    try {
      await getFleetMap(undefined, 60, false, ['user:a', 'b']);
      await getFleetCodeActivity(undefined, 60, ['user:a']);
      await getFleetUsage(undefined, null, ['user:a']);
      const urls = fetch.getCalls().map((call) => String(call.args[0]));
      expect(urls[0]).to.contain('active_within_minutes=60');
      expect(urls[0]).to.contain('users=a%2Cb');
      expect(urls[1]).to.contain('users=a');
      expect(urls[2]).to.equal('/api/v1/fleet/usage?users=a');
    } finally {
      fetch.restore();
      localStorage.removeItem('accessToken');
    }
  });

  it('omits users= for everyone and dedupes ids', () => {
    expect(usersParam([])).to.equal('');
    expect(usersParam(['user:a', 'a', 'user:b'])).to.equal('a,b');
  });

  it('returns the graph unchanged for an empty selection', () => {
    const graph = sampleFleetMap();
    expect(scopeToUsers(graph, new Set())).to.equal(graph);
  });

  it('keeps work the people or their agents touched, and drops flows', () => {
    const graph = withFlows();
    const work: FleetMapNode = {
      ...flow('work:pr:1', 'PR 1'),
      type: 'work',
      detail: null,
    };
    const other: FleetMapNode = { ...work, id: 'work:pr:2', label: 'PR 2' };
    const scoped = scopeToUsers(
      {
        ...graph,
        nodes: [...graph.nodes, work, other],
        edges: [
          ...graph.edges,
          {
            id: 't',
            source: 'agent:claude',
            target: 'work:pr:1',
            kind: 'touched',
            provenance: 'observed',
            impact_source: 'work:pr:1',
            impact_target: 'agent:claude',
            gaps: [],
          },
        ],
      },
      new Set(['user:maya'])
    );
    const ids = scoped.nodes.map((node) => node.id);
    expect(ids).to.include('work:pr:1');
    expect(ids).not.to.include('work:pr:2');
    expect(ids).not.to.include('flow:a');
    expect(ids).to.include.members([
      'device:mbp',
      'agent:claude',
      'agent:codex',
      'model:sonnet',
      'model:gpt',
      'provider:anthropic',
    ]);
    expect(ids).not.to.include('user:sam');
  });

  it('keeps work listed in code activity node_ids for a scoped agent', () => {
    const graph = sampleFleetMap();
    const work: FleetMapNode = {
      ...flow('work:issue:9', 'Issue 9'),
      type: 'work',
    };
    const scoped = scopeToUsers(
      { ...graph, nodes: [...graph.nodes, work] },
      new Set(['user:sam']),
      [{ id: 'work:issue:9', node_ids: ['agent:cursor'] }]
    );
    expect(scoped.nodes.map((n) => n.id)).to.include('work:issue:9');
  });
});

describe('Fleet Map pass 4: containment (item 6)', () => {
  it('puts models inside the provider named by provider_id', () => {
    const graph = sampleFleetMap();
    graph.nodes = graph.nodes.map((node) =>
      node.id === 'model:gpt'
        ? {
            ...node,
            provider_ids: ['provider:anthropic', 'provider:openai'],
            provider_id: 'provider:openai',
          }
        : node
    );
    expect(containers(graph).get('model:gpt')).to.equal('provider:openai');
  });

  it('leaves models free when providers are hidden', () => {
    const graph = sampleFleetMap();
    const parent = containers({
      nodes: graph.nodes.filter((node) => node.type !== 'provider'),
      edges: graph.edges,
    });
    expect(parent.has('model:gpt')).to.equal(false);
    const layout = layoutGraph({
      nodes: graph.nodes.filter((node) => node.type !== 'provider'),
      edges: graph.edges,
    });
    expect(layout.columns.map((c) => c.type)).to.include('model');
  });
});

describe('Fleet Map pass 4: lanes (item 8)', () => {
  it('puts flows under the agents in one lane with a sub-header', () => {
    const layout = layoutGraph(withFlows());
    const lane = layout.columns.find((c) => c.type === 'agent')!;
    expect(layout.columns.some((c) => c.type === 'flow')).to.equal(false);
    expect(laneTitle(lane)).to.equal('Agents & Flows');
    expect(lane.groups!.map((g) => [g.type, g.count])).to.deep.equal([
      ['agent', 3],
      ['flow', 2],
    ]);
    const lowestAgent = Math.max(
      ...['agent:claude', 'agent:codex', 'agent:cursor'].map(
        (id) => layout.positions.get(id)!.y + layout.positions.get(id)!.h
      )
    );
    const sub = lane.groups![1].y;
    expect(sub).to.be.at.least(lowestAgent);
    for (const id of ['flow:a', 'flow:b']) {
      const p = layout.positions.get(id)!;
      expect(p.x).to.equal(lane.x);
      expect(p.y).to.be.at.least(sub + 28);
    }
  });

  it('names the lane by what it holds', () => {
    const agentsOnly = layoutGraph(sampleFleetMap());
    expect(
      laneTitle(agentsOnly.columns.find((c) => c.type === 'agent')!)
    ).to.equal('Agents');
    const flowsOnly = layoutGraph({
      nodes: [flow('flow:a', 'Triage')],
      edges: [],
    });
    expect(laneTitle(flowsOnly.columns[0])).to.equal('Flows');
  });
});

describe('Fleet Map pass 4: words and icons (items 2, 9, 11)', () => {
  it('uses the navbar flow icon for flow nodes', () => {
    const mark = nodeMark(flow('flow:a', 'Triage'), 'diagram-2');
    expect(mark.src).to.equal(FLOW_ICON_SRC);
    expect(FLOW_ICON_SRC).to.equal('/images/flow.svg');
  });

  it('accepts lanes|canvas and the old columns|spatial words', () => {
    expect(parseArrangement('lanes')).to.equal('columns');
    expect(parseArrangement('columns')).to.equal('columns');
    expect(parseArrangement('Canvas')).to.equal('spatial');
    expect(parseArrangement('spatial')).to.equal('spatial');
    expect(parseArrangement('grid')).to.equal(undefined);
    expect(ARRANGEMENT_LABEL).to.deep.equal({
      columns: 'Lanes',
      spatial: 'Canvas',
    });
  });

  it('names a window by its value', () => {
    expect(windowLabel('1h')).to.equal('Last hour');
    expect(windowLabel('any')).to.equal('Any time');
  });
});
