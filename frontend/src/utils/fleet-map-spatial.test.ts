import { expect } from '@open-wc/testing';
import {
  loadPositions,
  savePositions,
  spatialLayout,
} from './fleet-map-spatial';
import { clusterOf } from './fleet-map-motion';
import { layoutGraph } from '../fleet-map-api';
import { sampleFleetMap } from '../test-helpers/fleet-map-graph';

describe('fleet-map spatial ownership', () => {
  afterEach(() => {
    for (const key of Object.keys(localStorage))
      if (key.startsWith('preloop:fleet-map')) localStorage.removeItem(key);
  });
  it('retains inactive slots for seven days, while active slots stay fresh', () => {
    savePositions(
      'expiry',
      { 'agent:a': { x: 25, y: 40 }, 'agent:b': { x: 200, y: 40 } },
      'test'
    );
    localStorage.setItem(
      'preloop:fleet-map:positions:test:expiry:seen',
      JSON.stringify({
        'agent:a': Date.now() - 8 * 86400_000,
        'agent:b': Date.now(),
      })
    );
    expect(loadPositions('expiry', 'test')).to.deep.equal({
      'agent:b': { x: 200, y: 40 },
    });
  });
  it('keeps unrelated owned nodes still when an agent arrives inside a machine', () => {
    const graph = sampleFleetMap();
    const first = spatialLayout(
      graph,
      layoutGraph(graph).positions,
      {},
      clusterOf(graph)
    );
    const pinned = Object.fromEntries(first.positions);
    const agent = {
      ...graph.nodes.find((n) => n.id === 'agent:codex')!,
      id: 'agent:new',
    };
    const next = {
      ...graph,
      nodes: [...graph.nodes, agent],
      edges: [
        ...graph.edges,
        {
          ...graph.edges.find(
            (e) => e.kind === 'hosts' && e.target === 'agent:codex'
          )!,
          id: 'new-host',
          target: agent.id,
        },
      ],
    };
    const layout = spatialLayout(
      next,
      layoutGraph(next).positions,
      pinned,
      clusterOf(next)
    );
    for (const id of [
      'user:maya',
      'user:sam',
      'device:build',
      'model:gpt',
      'model:sonnet',
    ])
      expect(layout.positions.get(id), id).to.deep.equal(
        first.positions.get(id)
      );
    const host = layout.positions.get('device:mbp')!,
      child = layout.positions.get(agent.id)!;
    expect(child.x).to.be.greaterThan(host.x);
    expect(child.y + child.h).to.be.at.most(host.y + host.h);
  });
  it('uses more width when room is available and keeps cards legible', () => {
    const graph = sampleFleetMap(),
      seed = layoutGraph(graph).positions;
    const small = spatialLayout(graph, seed, {}, clusterOf(graph), new Set(), {
      width: 1000,
      height: 700,
    });
    const large = spatialLayout(graph, seed, {}, clusterOf(graph), new Set(), {
      width: 1900,
      height: 1000,
    });
    expect(large.positions.get('model:gpt')!.w).to.be.at.least(
      small.positions.get('model:gpt')!.w
    );
    // Packed (pass 4): more room never spreads shelves past a short line.
    expect(large.width).to.be.at.least(small.width);
    expect(large.width).to.be.below(1900);
    for (const p of large.positions.values()) expect(p.w).to.be.at.least(180);
  });
});

describe('fleet-map spatial packing', () => {
  it('packs people islands, then flows, then providers holding models, then work', () => {
    const base = sampleFleetMap();
    const extra = (id: string, type: 'flow' | 'work') => ({
      ...base.nodes[0],
      id,
      type,
      label: id,
      ...(type === 'work' ? { work_kind: 'issue' as const } : {}),
    });
    const graph = {
      ...base,
      nodes: [
        ...base.nodes,
        extra('flow:triage', 'flow'),
        extra('work:issue:41', 'work'),
      ],
    };
    const layout = spatialLayout(
      graph,
      layoutGraph(graph).positions,
      {},
      clusterOf(graph),
      new Set(),
      { width: 1600, height: 900 }
    );
    const x = (id: string) => layout.positions.get(id)!.x;
    const people = graph.nodes.filter((n) => n.type === 'user');
    for (const person of people)
      expect(x(person.id)).to.be.below(x('flow:triage'));
    expect(x('flow:triage')).to.be.below(x('provider:openai'));
    expect(x('provider:openai')).to.be.below(x('work:issue:41'));
    expect(x('provider:anthropic')).to.be.below(x('work:issue:41'));
    // Models and accounts are drawn inside their provider box.
    expect(layout.groups.get('provider:openai')).to.deep.equal([
      'account:dev',
      'account:prod',
      'model:gpt',
    ]);
    const box = layout.positions.get('provider:openai')!;
    const model = layout.positions.get('model:gpt')!;
    expect(model.x).to.be.above(box.x);
    expect(model.y + model.h).to.be.at.most(box.y + box.h);
    // Tight: neighbouring shelves are never more than a short line apart.
    const shelves = [
      ...new Set(
        [...layout.positions.values()].filter((p) => p.w >= 216).map((p) => p.x)
      ),
    ].sort((a, b) => a - b);
    for (let i = 1; i < shelves.length; i++)
      expect(shelves[i] - shelves[i - 1]).to.be.at.most(box.w + 96);
  });
});

describe('fleet-map columns', () => {
  it('puts flows in the agents lane, under them, and keeps the agent count', () => {
    const base = sampleFleetMap();
    const graph = {
      ...base,
      nodes: [
        ...base.nodes,
        { ...base.nodes[0], id: 'flow:triage', type: 'flow' as const },
      ],
    };
    const layout = layoutGraph(graph);
    const types = layout.columns.map((c) => c.type);
    expect(types).not.to.include('flow');
    const lane = layout.columns.find((c) => c.type === 'agent')!;
    expect(lane.groups!.find((g) => g.type === 'agent')!.count).to.equal(
      base.nodes.filter((n) => n.type === 'agent').length
    );
    expect(layout.positions.get('flow:triage')!.x).to.equal(lane.x);
    expect(layout.width).to.be.at.most(1832);
  });

  it('stretches the row pitch into a tall stage and keeps it tight in a short one', () => {
    const graph = sampleFleetMap();
    const pitch = (height: number) => {
      const layout = layoutGraph(graph, {
        room: { width: 1880, height },
      });
      const ys = [...layout.positions]
        .filter(([id]) => id.startsWith('device:'))
        .map(([, p]) => p.y)
        .sort((a, b) => a - b);
      return { gap: ys[1] - ys[0], height: layout.height };
    };
    const tall = pitch(1000);
    const short = pitch(300);
    expect(short.gap).to.equal(48 + 10);
    expect(tall.gap).to.be.above(short.gap);
    expect(tall.gap).to.be.at.most(48 + 64);
    expect(tall.height).to.be.at.most(1000 - 16 - 64);
  });
});
