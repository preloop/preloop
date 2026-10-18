import { expect } from '@open-wc/testing';
import {
  FLEET_MAP_COLUMNS,
  blastRadius,
  filterGraph,
  layoutGraph,
  nodeGaps,
} from './fleet-map-api';
import { sampleFleetMap } from './test-helpers/fleet-map-graph';

const all = new Set(FLEET_MAP_COLUMNS);

describe('fleet map graph', () => {
  it('walks a provider account back to agents, devices and people, not sideways', () => {
    const reach = blastRadius(sampleFleetMap(), 'account:prod').nodes;
    expect([...reach].sort()).to.deep.equal(
      [
        'account:prod',
        'agent:codex',
        'device:mbp',
        'model:gpt',
        'user:maya',
      ].sort()
    );
    expect(reach.has('model:gpt')).to.equal(true);
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

  it('filters by provider keeping the chain that reaches it', () => {
    const view = filterGraph(sampleFleetMap(), {
      types: all,
      owner: '',
      provider: 'provider:anthropic',
    });
    expect(view.nodes.map((n) => n.id).sort()).to.deep.equal(
      [
        'account:anthropic',
        'model:sonnet',
        'agent:claude',
        'device:mbp',
        'provider:anthropic',
        'user:maya',
      ].sort()
    );
    expect(
      view.edges.every((e) => view.nodes.some((n) => n.id === e.source))
    ).to.equal(true);
  });

  it('hides types and their incident edges', () => {
    const graph = sampleFleetMap();
    const noModels = filterGraph(graph, {
      types: new Set(FLEET_MAP_COLUMNS.filter((t) => t !== 'model')),
      owner: '',
      provider: '',
    });
    expect(noModels.nodes.some((n) => n.type === 'model')).to.equal(false);
    expect(noModels.edges.some((e) => e.kind === 'uses_model')).to.equal(false);
  });

  it('lays out one column per present type, deterministically', () => {
    const graph = sampleFleetMap();
    const first = layoutGraph(graph);
    expect(first.columns.map((c) => c.type)).to.deep.equal([
      ...FLEET_MAP_COLUMNS,
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
