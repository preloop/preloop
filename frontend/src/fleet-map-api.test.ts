import { expect } from '@open-wc/testing';
import {
  FLEET_MAP_TYPES,
  blastRadius,
  filterGraph,
  getFleetUsage,
  layoutGraph,
  nodeGaps,
  scopeToUsers,
} from './fleet-map-api';
import sinon from 'sinon';
import { sampleFleetMap } from './test-helpers/fleet-map-graph';

const all = new Set(FLEET_MAP_TYPES);

describe('fleet map graph', () => {
  it('walks a provider account back to agents, devices and people, not sideways', () => {
    const reach = blastRadius(sampleFleetMap(), 'account:prod').nodes;
    expect([...reach].sort()).to.deep.equal(
      ['account:prod', 'agent:codex', 'device:mbp', 'user:maya'].sort()
    );
    expect(reach.has('model:gpt')).to.equal(false);
    expect(reach.has('agent:claude')).to.equal(false);
  });

  it('does not infer impact by walking backwards from an owner', () => {
    expect([...blastRadius(sampleFleetMap(), 'user:sam').nodes]).to.deep.equal([
      'user:sam',
    ]);
  });

  it('returns nothing for an unknown node', () => {
    expect(blastRadius(sampleFleetMap(), 'nope').nodes.size).to.equal(0);
  });

  it('labels explicit gaps and counts findings without inferring missing traffic', () => {
    const graph = sampleFleetMap();
    const byId = new Map(graph.nodes.map((node) => [node.id, node]));
    expect(
      nodeGaps(byId.get('agent:cursor')!).map((g) => g.code)
    ).to.deep.equal(['discovered_agent']);
    expect(nodeGaps(byId.get('account:dev')!).map((g) => g.code)).to.deep.equal(
      ['not_via_gateway']
    );
    expect(nodeGaps(byId.get('agent:claude')!)).to.deep.equal([]);
    expect(
      nodeGaps({
        ...byId.get('device:mbp')!,
        gaps: ['open_finding'],
        finding_count: 2,
      })[0].label
    ).to.equal('2 open fleet findings');
  });

  it('scopes to a person: their devices, agents, models, providers', () => {
    const view = scopeToUsers(sampleFleetMap(), new Set(['user:sam']));
    expect(view.nodes.map((n) => n.id).sort()).to.deep.equal(
      [
        'user:sam',
        'device:build',
        'runner:r2',
        'agent:cursor',
        'account:dev',
        'provider:openai',
      ].sort()
    );
    expect(
      view.edges.every(
        (e) =>
          view.nodes.some((n) => n.id === e.source) &&
          view.nodes.some((n) => n.id === e.target)
      )
    ).to.equal(true);
  });

  it('hides types and their incident edges', () => {
    const graph = sampleFleetMap();
    const noModels = filterGraph(graph, {
      types: new Set(FLEET_MAP_TYPES.filter((t) => t !== 'model')),
    });
    expect(noModels.nodes.some((n) => n.type === 'model')).to.equal(false);
    expect(noModels.edges.some((e) => e.kind === 'uses_model')).to.equal(false);
  });

  it('lays out one lane per present type, models inside providers', () => {
    const graph = sampleFleetMap();
    const first = layoutGraph(graph);
    expect(first.columns.map((c) => c.type)).to.deep.equal([
      'user',
      'device',
      'agent',
      'provider',
    ]);
    expect(first.groups.get('provider:anthropic')).to.deep.equal([
      'account:anthropic',
      'model:sonnet',
    ]);
    expect(layoutGraph(graph).positions).to.deep.equal(first.positions);
    const users = first.positions.get('user:maya')!;
    const agents = first.positions.get('agent:claude')!;
    expect(agents.x).to.be.greaterThan(users.x);
    const noRunners = layoutGraph({
      nodes: graph.nodes.filter((n) => n.type !== 'runner'),
      edges: [],
    });
    expect(noRunners.columns.some((c) => c.type === 'runner')).to.equal(false);
  });

  it('places the neighbours of the first device above the second', () => {
    const layout = layoutGraph(sampleFleetMap());
    const y = (id: string) => layout.positions.get(id)!.y;
    expect(y('device:mbp')).to.be.lessThan(y('device:build'));
    expect(y('agent:claude')).to.be.lessThan(y('agent:cursor'));
  });
});

describe('Fleet Map API contract', () => {
  it('follows model-to-runner dependencies and device hosting in the server direction', () => {
    const graph = sampleFleetMap();
    const reach = blastRadius(graph, 'runner:r2').nodes;
    expect([...reach].sort()).to.deep.equal(
      [
        'runner:r2',
        'model:sonnet',
        'agent:claude',
        'device:mbp',
        'device:build',
        'user:maya',
        'user:sam',
      ].sort()
    );
    for (const edge of graph.edges) {
      expect(edge.impact_source).to.equal(edge.target);
      expect(edge.impact_target).to.equal(edge.source);
    }
  });

  it('does not include a provider or a sibling account in account impact', () => {
    const graph = sampleFleetMap();
    const reach = blastRadius(graph, 'account:prod').nodes;
    expect(reach.has('provider:openai')).to.equal(false);
    expect(reach.has('account:dev')).to.equal(false);
  });
});

describe('server summary groups', () => {
  it('materializes omitted resources without claiming they can be expanded', async () => {
    const { withClusters } = await import('./fleet-map-api');
    const graph = withClusters({
      ...sampleFleetMap(),
      truncated: true,
      clusters: [{ type: 'agent', count: 20 }],
    });
    const summary = graph.nodes.find((n) => n.id === 'cluster:agent')!;
    expect(summary.count).to.equal(20);
    expect(summary.href).to.equal('');
    expect(summary.detail).to.equal('Outside this map');
  });
});

describe('gaps only', () => {
  it('keeps direct neighbours of gap nodes in any edge order', () => {
    const graph = sampleFleetMap();
    const ids = (edges: typeof graph.edges) =>
      filterGraph({ ...graph, edges }, { types: all, gapsOnly: true })
        .nodes.map((n) => n.id)
        .sort();
    const forward = ids(graph.edges);
    expect(forward).to.deep.equal(ids([...graph.edges].reverse()));
    // The offline runner keeps the device hosting it; an agent that only
    // shares a model with that runner is two hops away and drops out.
    expect(forward).to.include('device:build');
    expect(forward).not.to.include('agent:claude');
  });
});

describe('Fleet usage client', () => {
  let fetchStub: sinon.SinonStub;
  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-token');
    fetchStub = sinon.stub(window, 'fetch');
  });
  afterEach(() => {
    fetchStub.restore();
    localStorage.removeItem('accessToken');
  });
  it('reads window totals from /fleet/usage, and the any window without a parameter', async () => {
    const body = {
      active_within_minutes: 60,
      since: '2026-10-20T09:00:00Z',
      until: '2026-10-20T10:00:00Z',
      truncated: false,
      partial: false,
      models: {},
      providers: {},
    };
    fetchStub.callsFake(
      async () => new Response(JSON.stringify(body), { status: 200 })
    );
    expect(await getFleetUsage(undefined, 60)).to.deep.equal(body);
    expect(String(fetchStub.firstCall.args[0])).to.contain(
      '/api/v1/fleet/usage?active_within_minutes=60'
    );
    await getFleetUsage(undefined, null);
    expect(String(fetchStub.secondCall.args[0])).to.match(
      /\/api\/v1\/fleet\/usage$/
    );
    fetchStub.callsFake(async () => new Response('{}', { status: 404 }));
    let failed = false;
    try {
      await getFleetUsage(undefined, 60);
    } catch {
      failed = true;
    }
    expect(failed, 'an older server without the route').to.equal(true);
  });
});
