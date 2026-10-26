import { expect, fixture, html } from '@open-wc/testing';
import { setViewport } from '@web/test-runner-commands';
import { useFakeTimers } from 'sinon';
import './fleet-map';
import type { FleetMap } from './fleet-map';
import { sampleFleetMap } from '../test-helpers/fleet-map-graph';

async function map(extra: Record<string, unknown> = {}) {
  await setViewport({ width: 1280, height: 900 });
  const el = await fixture<FleetMap>(
    html`<fleet-map
      .graph=${sampleFleetMap()}
      instance-admin
      spatial-only
      reduced-motion
      user-id="v3-user"
      positions-scope="v3-test"
      style="height:600px;width:1100px"
    ></fleet-map>`
  );
  Object.assign(el, extra);
  await el.updateComplete;
  return el;
}

const positions = (el: FleetMap) =>
  Object.fromEntries(
    [...el.shadowRoot!.querySelectorAll<HTMLElement>('[data-node]')].map(
      (n) => [
        n.dataset.node!,
        { x: parseFloat(n.style.left), y: parseFloat(n.style.top) },
      ]
    )
  );

describe('fleet-map v3', () => {
  afterEach(() => {
    for (const key of Object.keys(localStorage))
      if (key.startsWith('preloop:fleet-map')) localStorage.removeItem(key);
  });

  it('uses the collection toolbar and stays quiet when there are no gaps', async () => {
    const graph = sampleFleetMap();
    const el = await map({
      graph: { ...graph, nodes: graph.nodes.map((n) => ({ ...n, gaps: [] })) },
    });
    expect(el.shadowRoot!.querySelector('list-toolbar')).to.exist;
    expect(el.shadowRoot!.querySelector('[data-testid="fleet-map-gaps"]')).not
      .to.exist;
    expect(el.shadowRoot!.querySelector('[data-testid="fleet-map-gap-select"]'))
      .not.to.exist;
    expect(el.shadowRoot!.querySelector('select')).not.to.exist;
  });

  it('lets plain wheel scroll the page in page mode, but consumes zoom and pan', async () => {
    const el = await map({ wheelMode: 'page' });
    const stage = el.shadowRoot!.querySelector('.stage')!;
    const wheel = (ctrlKey = false) =>
      new WheelEvent('wheel', { deltaY: 20, ctrlKey, cancelable: true });
    const page = wheel();
    stage.dispatchEvent(page);
    expect(page.defaultPrevented).to.equal(false);
    const zoom = wheel(true);
    stage.dispatchEvent(zoom);
    expect(zoom.defaultPrevented).to.equal(true);
    Object.assign(el, { wheelMode: 'pan' });
    const pan = wheel();
    stage.dispatchEvent(pan);
    expect(pan.defaultPrevented).to.equal(true);
  });

  it('keeps a minimum legible spatial scale for a dense fleet', async () => {
    const base = sampleFleetMap();
    const el = await map({
      graph: {
        ...base,
        nodes: [
          ...base.nodes,
          ...Array.from({ length: 60 }, (_, i) => ({
            ...base.nodes[0],
            id: `user:extra-${i}`,
          })),
        ],
      },
    });
    (el as unknown as { fit(): void }).fit();
    expect((el as unknown as { scale: number }).scale).to.be.at.least(0.85);
  });

  it('owns the whole arrangement after a drag, across refresh and membership changes', async () => {
    const el = await map();
    const stage = el.shadowRoot!.querySelector('.stage')!;
    const node = el.shadowRoot!.querySelector('[data-node="device:mbp"]')!;
    const pointer = (type: string, x: number) =>
      new PointerEvent(type, {
        bubbles: true,
        pointerId: 7,
        clientX: x,
        clientY: 10,
        button: 0,
      });
    node.dispatchEvent(pointer('pointerdown', 10));
    stage.dispatchEvent(pointer('pointermove', 70));
    stage.dispatchEvent(pointer('pointerup', 70));
    await el.updateComplete;
    const before = positions(el);
    const saved = JSON.parse(
      localStorage.getItem('preloop:fleet-map:positions:v3-test:v3-user')!
    );
    expect(Object.keys(saved).length).to.equal(Object.keys(before).length);
    const base = sampleFleetMap();
    el.graph = {
      ...base,
      nodes: [...base.nodes, { ...base.nodes[0], id: 'user:new-person' }],
    };
    await el.updateComplete;
    const after = positions(el);
    for (const [id, point] of Object.entries(before))
      expect(after[id], id).to.deep.equal(point);
    const restored = await map({ graph: el.graph });
    for (const [id, point] of Object.entries(before))
      expect(positions(restored)[id], id).to.deep.equal(point);
  });

  it('shows measured model content and never invents an answers bubble', async () => {
    const el = await map({ bubbles: true });
    el.recordActivity('gateway_activity', {
      type: 'model_gateway_call',
      payload: {
        managed_agent_id: 'codex',
        model_alias: 'gpt-6.1',
        prompt_tokens: 4100,
        completion_tokens: 380,
      },
    });
    await el.updateComplete;
    const text = el.shadowRoot!.querySelector('.bubble')?.textContent;
    expect(text).to.contain('4.1k in / 380 out');
    expect(text).not.to.contain('answers');
  });

  it('counts native tools and keeps their content in the selected agent Recent list', async () => {
    const el = await map({ bubbles: true });
    el.recordActivity('runtime_sessions', {
      type: 'runtime_session_updated',
      payload: {
        activity_type: 'tool_call',
        managed_agent_id: 'codex',
        tool_name: 'Edit',
        status: 'succeeded',
        event_id: 'native-1',
      },
    });
    await el.updateComplete;
    expect(el.litNodes).to.include('agent:codex');
    expect(
      el.shadowRoot!.querySelector('[data-tools="agent:codex"]')?.textContent
    ).to.contain('1');
    el.shadowRoot!.querySelector<HTMLButtonElement>(
      '[data-node="agent:codex"]'
    )!.click();
    await el.updateComplete;
    expect(
      el.shadowRoot!.querySelector('[data-testid="fleet-map-recent"]')
        ?.textContent
    ).to.contain('Edit');
  });
});

describe('fleet-map v3 lifecycle and controls', () => {
  afterEach(() => {
    for (const key of Object.keys(localStorage))
      if (key.startsWith('preloop:fleet-map')) localStorage.removeItem(key);
  });

  it('stops the story clock after run and merged-work expiry, then restarts for new activity', async () => {
    const el = await map({ activityWindow: 'any' });
    const clock = useFakeTimers({
      now: Date.now(),
      toFake: ['Date', 'setInterval', 'clearInterval'],
    });
    const state = el as unknown as { storyTimer?: number };
    const run = (id: string) => ({
      type: 'status_update',
      flow_id: 'triage',
      execution_id: id,
      payload: { status: 'SUCCEEDED', flow_name: 'Issue triage' },
    });
    try {
      el.recordActivity('flow_executions', run('first'));
      expect(state.storyTimer).not.to.equal(undefined);
      clock.tick(59_000);
      expect(state.storyTimer).not.to.equal(undefined);
      clock.tick(1000);
      await el.updateComplete;
      expect(state.storyTimer).to.equal(undefined);
      expect(el.shadowRoot!.querySelector('[data-node="flow:triage"]')).not.to
        .exist;

      el.recordActivity('code_activity', {
        type: 'code_activity',
        payload: { action: 'merged', pr: 'example/widgets#51' },
      });
      expect(state.storyTimer).not.to.equal(undefined);
      clock.tick(119_000);
      expect(state.storyTimer).not.to.equal(undefined);
      clock.tick(1000);
      await el.updateComplete;
      expect(state.storyTimer).to.equal(undefined);
      expect(el.shadowRoot!.querySelector('[data-kind="work"]')).not.to.exist;

      el.recordActivity('flow_executions', run('second'));
      expect(state.storyTimer).not.to.equal(undefined);
    } finally {
      el.remove();
      clock.restore();
    }
  });

  it('resumes the story clock when a wider window reveals retained work', async () => {
    const el = await map({ activityWindow: '5m' });
    const clock = useFakeTimers({
      now: Date.now(),
      toFake: ['Date', 'setInterval', 'clearInterval'],
    });
    const state = el as unknown as { storyTimer?: number };
    try {
      el.recordActivity('code_activity', {
        type: 'code_activity',
        payload: {
          action: 'issue_opened',
          subject: {
            kind: 'issue',
            key: 'example/widgets#41',
            reference: '#41',
          },
        },
      });
      clock.tick(301_000);
      await el.updateComplete;
      expect(state.storyTimer).to.equal(undefined);
      expect(el.shadowRoot!.querySelector('[data-kind="work"]')).not.to.exist;
      el.activityWindow = 'any';
      await el.updateComplete;
      expect(state.storyTimer).not.to.equal(undefined);
      expect(el.shadowRoot!.querySelector('[data-kind="work"]')).to.exist;
      el.activityWindow = '5m';
      await el.updateComplete;
      expect(state.storyTimer).to.equal(undefined);
    } finally {
      el.remove();
      clock.restore();
    }
  });

  it('resets a complete manual layout and restores it with Undo without moving the camera', async () => {
    const el = await map();
    const stage = el.shadowRoot!.querySelector('.stage')!;
    const target = el.shadowRoot!.querySelector('[data-node="device:mbp"]')!;
    const pointer = (type: string, x: number) =>
      new PointerEvent(type, {
        bubbles: true,
        pointerId: 9,
        button: 0,
        clientX: x,
        clientY: 40,
      });
    target.dispatchEvent(pointer('pointerdown', 30));
    stage.dispatchEvent(pointer('pointermove', 100));
    stage.dispatchEvent(pointer('pointerup', 100));
    await el.updateComplete;
    const owned = positions(el),
      transform =
        el.shadowRoot!.querySelector<HTMLElement>('.plane')!.style.transform;
    el.fit();
    await el.updateComplete;
    expect(positions(el)).to.deep.equal(owned);
    const fitted =
      el.shadowRoot!.querySelector<HTMLElement>('.plane')!.style.transform;
    let resets = 0;
    el.addEventListener('fleet-map-layout-reset', () => resets++);
    el.resetLayout();
    await el.updateComplete;
    expect(positions(el)).not.to.deep.equal(owned);
    expect(resets).to.equal(1);
    expect(
      el.shadowRoot!.querySelector('.reset-notice')?.textContent
    ).to.contain('Undo');
    el.undoResetLayout();
    await el.updateComplete;
    expect(positions(el)).to.deep.equal(owned);
    expect(
      el.shadowRoot!.querySelector<HTMLElement>('.plane')!.style.transform
    ).to.equal(fitted);
    expect(transform).to.be.a('string');
  });

  it('keeps navigation inside a short host and makes the legend a disclosure', async () => {
    const el = await map();
    el.style.height = '480px';
    await new Promise((resolve) => setTimeout(resolve, 60));
    await el.updateComplete;
    const stage = el
      .shadowRoot!.querySelector('.stage')!
      .getBoundingClientRect();
    const tools = el
      .shadowRoot!.querySelector('.canvas-tools')!
      .getBoundingClientRect();
    expect(tools.bottom).to.be.at.most(stage.bottom);
    expect(tools.right).to.be.at.most(stage.right);
    expect(
      el.shadowRoot!.querySelector<HTMLElement>('.legend')!.hidden
    ).to.equal(true);
    el.shadowRoot!.querySelector<HTMLButtonElement>('.legend-toggle')!.click();
    await el.updateComplete;
    expect(
      el.shadowRoot!.querySelector<HTMLElement>('.legend')!.hidden
    ).to.equal(false);
  });

  it('shows current flow events and attached work in both arrangements', async () => {
    for (const arrangement of ['columns', 'spatial']) {
      const el = await map({
        spatialOnly: false,
        defaultArrangement: arrangement,
        bubbles: true,
        positionsScope: `story-${arrangement}`,
      });
      el.recordActivity('flow_executions', {
        type: 'execution_started',
        flow_id: 'triage',
        execution_id: 'r1',
        payload: {
          status: 'PENDING',
          flow_name: 'Issue triage',
          fleet_context: {
            trigger_kind: 'issue_opened',
            subject: { repo: 'example/widgets', reference: '#41' },
          },
        },
      });
      await el.updateComplete;
      await el.updateComplete;
      expect(el.shadowRoot!.querySelector('[data-node="flow:triage"]')).to
        .exist;
      expect(
        el.shadowRoot!.querySelector('[data-kind="work"]')?.textContent
      ).to.contain('#41');
      expect(el.shadowRoot!.querySelector('.bubble')?.textContent).to.contain(
        'Issue triage started'
      );
      expect(el.shadowRoot!.querySelector('path[data-edge="caused"]')).to.exist;
    }
  });

  it('deduplicates native events without reading argument or model content', async () => {
    const el = await map({ bubbles: true });
    const event = {
      type: 'runtime_session_updated',
      payload: {
        activity_id: 'n1',
        activity_type: 'tool_call',
        managed_agent_id: 'codex',
        tool_name: 'Bash',
        status: 'allowed',
        metadata: {
          arguments_summary: { command: 100 },
          arguments: { command: 'PRIVATE COMMAND' },
        },
      },
    };
    el.recordActivity('runtime_sessions', event);
    el.recordActivity('runtime_sessions', event);
    await el.updateComplete;
    expect(
      el
        .shadowRoot!.querySelector('[data-tools="agent:codex"]')
        ?.textContent?.trim()
    ).to.equal('1');
    expect(el.shadowRoot!.querySelector('.bubble')?.textContent).to.contain(
      'Bash  allowed'
    );
    expect(el.shadowRoot!.textContent).not.to.contain('PRIVATE COMMAND');
  });
});

describe('fleet-map work geometry', () => {
  it('shows recorded PR states and reserves unknown for missing evidence', async () => {
    const graph = sampleFleetMap();
    const states = [
      ['Open', 'Open'],
      ['Closed', 'Closed'],
      ['Merged', 'Merged'],
      [null, 'State unknown'],
    ] as const;
    const el = await map({
      graph: {
        ...graph,
        nodes: [
          ...graph.nodes,
          ...states.map(([status], index) => ({
            ...graph.nodes[0],
            id: `work:pull_request:example/widgets#${index + 1}`,
            type: 'work' as const,
            resource_id: `example/widgets#${index + 1}`,
            label: `PR #${index + 1}`,
            href: `https://example.com/example/widgets/pull/${index + 1}`,
            status,
            work_kind: 'pull_request' as const,
          })),
        ],
      },
      positionsScope: 'pr-state-labels',
    });

    for (const [index, [status, label]] of states.entries()) {
      const node = el.shadowRoot!.querySelector<HTMLElement>(
        `[data-node="work:pull_request:example/widgets#${index + 1}"]`
      )!;
      node.click();
      await el.updateComplete;
      const chip = el.shadowRoot!.querySelector(
        '[data-testid="fleet-map-panel"] .chips sl-badge'
      );
      expect(chip?.textContent?.trim()).to.equal(label);
      expect(node.getAttribute('data-status')).to.equal(status);
    }
  });

  it('fits a newly arriving Work column and shows merged state above a prior review', async () => {
    const el = await map({
      spatialOnly: false,
      defaultArrangement: 'columns',
      positionsScope: 'work-geometry',
    });
    const subject = {
      kind: 'pull_request',
      key: 'example/widgets#51',
      reference: '#51',
      url: 'https://example.com/example/widgets/pull/51',
    };
    el.recordActivity('code_activity', {
      type: 'code_activity',
      payload: {
        action: 'reviewed',
        subject,
        review_state: 'approved',
        managed_agent_id: 'codex',
      },
    });
    await el.updateComplete;
    await el.updateComplete;
    const stage = el
      .shadowRoot!.querySelector('.stage')!
      .getBoundingClientRect();
    const chip = el.shadowRoot!.querySelector('[data-kind="work"]')!;
    expect(chip.getBoundingClientRect().right).to.be.at.most(stage.right);
    el.recordActivity('code_activity', {
      type: 'code_activity',
      payload: { action: 'merged', subject },
    });
    await el.updateComplete;
    expect(chip.getAttribute('data-status')).to.equal('Merged');
    expect(chip.textContent).to.contain('Merged');
  });
  it('repacks new work into the work shelf, right of the flows, before the user owns a spatial layout', async () => {
    const el = await map({ positionsScope: 'work-packing' });
    for (const id of [41, 42]) {
      el.recordActivity('flow_executions', {
        type: 'execution_started',
        flow_id: 'triage',
        execution_id: `r${id}`,
        payload: {
          status: 'RUNNING',
          fleet_context: {
            flow_name: 'Issue triage',
            trigger_kind: 'issue_opened',
            subject: { repo: 'example/widgets', reference: `#${id}` },
          },
        },
      });
      await el.updateComplete;
      await el.updateComplete;
    }
    const work = [
      ...el.shadowRoot!.querySelectorAll<HTMLElement>('[data-kind="work"]'),
    ];
    expect(work).to.have.length(2);
    const left = (n: Element) => parseFloat((n as HTMLElement).style.left);
    expect(Math.abs(left(work[0]) - left(work[1]))).to.be.below(2);
    const flow = el.shadowRoot!.querySelector('[data-node="flow:triage"]')!;
    expect(left(flow)).to.be.below(left(work[0]));
    for (const node of el.shadowRoot!.querySelectorAll('[data-kind="user"]'))
      expect(left(node)).to.be.below(left(flow));
  });
});

const issue = (n: number) => ({
  kind: 'issue',
  key: `example/widgets#${n}`,
  reference: `#${n}`,
  title: `Issue ${n}`,
  url: `https://example.com/example/widgets/issues/${n}`,
});

describe('fleet-map v3 pass 2', () => {
  afterEach(() => {
    for (const key of Object.keys(localStorage))
      if (key.startsWith('preloop:fleet-map')) localStorage.removeItem(key);
  });

  it('keeps search compact and windows fully named in dropdown options', async () => {
    const el = await map({ spatialOnly: false });
    await setViewport({ width: 1920, height: 1080 });
    el.style.width = '1880px';
    await new Promise((resolve) => setTimeout(resolve, 50));
    await el.updateComplete;
    const bar = el.shadowRoot!.querySelector('list-toolbar')!;
    expect(
      bar.shadowRoot!.querySelector('sl-input')!.getBoundingClientRect().width
    ).to.be.at.most(240);
    for (const value of ['5m', '24h']) {
      const option = el.shadowRoot!.querySelector<HTMLElement>(
        `[data-window="${value}"]`
      )!;
      expect(option.textContent?.trim()).to.equal(
        value === '5m' ? 'Last 5 minutes' : 'Last 24 hours'
      );
      option.click();
      await el.updateComplete;
      expect(el.activityWindow).to.equal(value);
    }
  });

  it('keeps tracker labels on the issue chip after the PR opens', async () => {
    const el = await map({ positionsScope: 'labels' });
    const send = (payload: Record<string, unknown>) =>
      el.recordActivity('code_activity', { type: 'code_activity', payload });
    send({
      event_id: 'o',
      action: 'issue_opened',
      managed_agent_id: 'claude',
      subject: issue(41),
    });
    send({
      event_id: 'l',
      action: 'issue_labeled',
      flow_id: 'triage',
      subject: issue(41),
      labels_added: ['bug', 'p2'],
      labels_removed: [],
    });
    await el.updateComplete;
    await el.updateComplete;
    const chip = () =>
      el.shadowRoot!.querySelector(
        '[data-node="work:issue:example/widgets#41"]'
      )!;
    const tags = () =>
      [...chip().querySelectorAll('[data-label]')].map((t) =>
        t.textContent!.trim()
      );
    expect(tags()).to.deep.equal(['bug', 'p2']);
    send({
      event_id: 'p',
      action: 'opened',
      flow_id: 'implementation',
      subject: {
        kind: 'pull_request',
        key: 'example/widgets#51',
        reference: '#51',
      },
      related_issue: issue(41),
    });
    send({
      event_id: 'u',
      action: 'issue_unlabeled',
      flow_id: 'triage',
      subject: issue(41),
      labels_removed: ['p2'],
    });
    await el.updateComplete;
    await el.updateComplete;
    expect(chip().textContent).to.contain('PR open');
    expect(tags()).to.deep.equal(['bug']);
  });

  it('shows two label tags and a count, with every label in the title, inside the chip', async () => {
    const el = await map({ positionsScope: 'many-labels' });
    const send = (payload: Record<string, unknown>) =>
      el.recordActivity('code_activity', { type: 'code_activity', payload });
    send({
      event_id: 'o',
      action: 'issue_opened',
      managed_agent_id: 'claude',
      subject: issue(41),
    });
    send({
      event_id: 'l',
      action: 'issue_labeled',
      flow_id: 'triage',
      subject: issue(41),
      labels_added: [
        'bug',
        'p2',
        'needs-design-review-from-platform',
        'area/checkout',
      ],
    });
    await el.updateComplete;
    await el.updateComplete;
    const chip = el.shadowRoot!.querySelector<HTMLElement>(
      '[data-node="work:issue:example/widgets#41"]'
    )!;
    expect(
      [...chip.querySelectorAll('[data-label]')].map((t) =>
        t.textContent!.trim()
      )
    ).to.deep.equal(['bug', 'p2']);
    expect(
      chip.querySelector('[data-labels-more]')!.textContent!.trim()
    ).to.equal('+2');
    expect(chip.title).to.contain(
      'Labels: bug, p2, needs-design-review-from-platform, area/checkout'
    );
    expect(chip.getAttribute('aria-label')).to.contain('labels bug, p2');
    const box = chip.getBoundingClientRect();
    for (const tag of chip.querySelectorAll('.tag')) {
      const r = tag.getBoundingClientRect();
      if (r.width) expect(r.left).to.be.at.least(box.left);
    }
    expect(
      chip.querySelector('.sub')!.getBoundingClientRect().right
    ).to.be.at.most(box.right);
  });

  it('fits two long label tags and the count inside the chip', async () => {
    const el = await map({ positionsScope: 'long-labels' });
    const send = (payload: Record<string, unknown>) =>
      el.recordActivity('code_activity', { type: 'code_activity', payload });
    send({
      event_id: 'o',
      action: 'issue_opened',
      managed_agent_id: 'claude',
      subject: issue(41),
    });
    send({
      event_id: 'l',
      action: 'issue_labeled',
      flow_id: 'triage',
      subject: issue(41),
      labels_added: [
        'needs-design-review-from-platform',
        'customer-escalation-tier-one',
        'p2',
      ],
    });
    await el.updateComplete;
    await el.updateComplete;
    const chip = el.shadowRoot!.querySelector<HTMLElement>(
      '[data-node="work:issue:example/widgets#41"]'
    )!;
    const sub = chip.querySelector('.sub')!.getBoundingClientRect();
    for (const tag of chip.querySelectorAll('.tag, [data-labels-more]'))
      expect(tag.getBoundingClientRect().right).to.be.at.most(sub.right + 0.5);
    const more = chip.querySelector<HTMLElement>('[data-labels-more]')!;
    expect(more.textContent!.trim()).to.equal('+1');
    expect(more.scrollWidth).to.be.at.most(more.clientWidth + 1);
    expect(more.getBoundingClientRect().width).to.be.above(8);
  });

  it('hides a note the per-frame pass cannot place instead of leaving it in place', async () => {
    const el = await map({ bubbles: true, positionsScope: 'hide-note' });
    el.recordActivity('gateway_activity', {
      type: 'model_gateway_call',
      payload: {
        managed_agent_id: 'claude',
        model_alias: 'claude-sonnet-5-5',
        api_usage_id: 'h1',
        prompt_tokens: 10,
        completion_tokens: 2,
        outcome: 'success',
      },
    });
    await el.updateComplete;
    const bubble = el.shadowRoot!.querySelector<HTMLElement>('.bubble')!;
    expect(bubble.style.visibility).to.equal('');
    const internals = el as unknown as {
      placeNotes: () => unknown[];
      applyTransform(): void;
    };
    // A zoom where the stage has no free spot left.
    internals.placeNotes = () => [];
    internals.applyTransform();
    expect(bubble.style.visibility).to.equal('hidden');
  });

  it('draws no line between an issue and the PR inside it, and labels work lines by their action', async () => {
    for (const arrangement of ['columns', 'spatial']) {
      const el = await map({
        spatialOnly: false,
        defaultArrangement: arrangement,
        positionsScope: `contain-${arrangement}`,
      });
      el.recordActivity('code_activity', {
        type: 'code_activity',
        payload: {
          event_id: 'o',
          action: 'issue_opened',
          managed_agent_id: 'claude',
          subject: issue(41),
        },
      });
      el.recordActivity('code_activity', {
        type: 'code_activity',
        payload: {
          event_id: 'p',
          action: 'opened',
          flow_id: 'implementation',
          subject: {
            kind: 'pull_request',
            key: 'example/widgets#51',
            reference: '#51',
          },
          related_issue: issue(41),
        },
      });
      await el.updateComplete;
      await el.updateComplete;
      const issueId = 'work:issue:example/widgets#41';
      const prId = 'work:pull_request:example/widgets#51';
      expect(el.shadowRoot!.querySelector(`[data-node="${prId}"]`), arrangement)
        .to.exist;
      for (const path of el.shadowRoot!.querySelectorAll(
        'path[data-edge="caused"]'
      )) {
        const id = path.getAttribute('data-edge-id') ?? '';
        expect(
          id.includes(issueId) && id.includes(prId),
          `${arrangement} ${id}`
        ).to.equal(false);
      }
      expect(
        !!el.shadowRoot!.querySelector('path[data-edge="caused"].active'),
        arrangement
      ).to.equal(true);
      // The lit line to the issue says the action that created it (drawn as
      // text in Columns where it covers no node; always on the line).
      const lines = [
        ...el.shadowRoot!.querySelectorAll('path.edge.active'),
      ].map((p) => p.getAttribute('data-activity'));
      expect(lines, arrangement).to.include('opened issue #41');
      const texts = [
        ...el.shadowRoot!.querySelectorAll('text.activity-label'),
      ].map((t) => t.textContent);
      if (arrangement === 'spatial') expect(texts).to.deep.equal([]);
      expect(texts.join(' ')).not.to.contain('Observed work');
    }
  });

  it('connects a flow to the model its run calls and names the call on that line', async () => {
    const el = await map({
      spatialOnly: false,
      defaultArrangement: 'columns',
      positionsScope: 'flow-model',
    });
    el.recordActivity('flow_executions', {
      type: 'execution_started',
      flow_id: 'triage',
      execution_id: 'run-1',
      payload: {
        status: 'RUNNING',
        fleet_context: { flow_name: 'Issue triage' },
      },
    });
    await el.updateComplete;
    expect(
      el.shadowRoot!.querySelector(
        'path[data-edge-id="flow:triage>uses_model>model:sonnet"]'
      )
    ).not.to.exist;
    // The gateway names the flow on calls made inside its run.
    el.recordActivity('gateway_activity', {
      type: 'model_gateway_call',
      flow_id: 'triage',
      payload: {
        model_alias: 'claude-sonnet-5-5',
        prompt_tokens: 2100,
        completion_tokens: 330,
        api_usage_id: 'fm-1',
      },
    });
    el.flushActivity();
    await el.updateComplete;
    await el.updateComplete;
    const line = el.shadowRoot!.querySelector<SVGPathElement>(
      'path[data-edge-id="flow:triage>uses_model>model:sonnet"]'
    )!;
    expect(line, 'flow to model line').to.exist;
    expect(line.classList.contains('active')).to.equal(true);
    expect(line.getAttribute('data-activity')).to.equal(
      'claude-sonnet-5-5 · 2.1k in / 330 out'
    );
  });

  it('places bubbles beside their node in Columns, never on another node', async () => {
    const el = await map({
      spatialOnly: false,
      defaultArrangement: 'columns',
      bubbles: true,
      positionsScope: 'bubbles-columns',
    });
    el.style.height = '900px';
    el.recordActivity('gateway_activity', {
      type: 'model_gateway_call',
      payload: {
        managed_agent_id: 'claude',
        model_alias: 'claude-sonnet-5-5',
        api_usage_id: 'u1',
        prompt_tokens: 2100,
        completion_tokens: 330,
        outcome: 'success',
      },
    });
    await el.updateComplete;
    await new Promise((resolve) => setTimeout(resolve, 50));
    await el.updateComplete;
    const bubble = el.shadowRoot!.querySelector<HTMLElement>('.bubble')!;
    expect(bubble).to.exist;
    const b = bubble.getBoundingClientRect();
    const own = el
      .shadowRoot!.querySelector(`[data-node="${bubble.dataset.bubble}"]`)!
      .getBoundingClientRect();
    // Next to its node, not in a corner stack.
    expect(Math.abs(b.top - own.bottom)).to.be.below(own.height + 40);
    for (const node of el.shadowRoot!.querySelectorAll<HTMLElement>(
      '.node:not(.child), .column-title'
    )) {
      if (node.dataset.node === bubble.dataset.bubble) continue;
      const r = node.getBoundingClientRect();
      const hit =
        b.left < r.right &&
        r.left < b.right &&
        b.top < r.bottom &&
        r.top < b.bottom;
      expect(hit, node.dataset.node).to.equal(false);
    }
  });

  it('refits the camera when a node joins an unowned spatial layout, and not once it is owned', async () => {
    const el = await map({ positionsScope: 'join-refit' });
    const internals = el as unknown as {
      userMovedAt: number;
      points: Record<string, { x: number; y: number }>;
      translateX: number;
      translateY: number;
      scale: number;
      applyTransform(): void;
    };
    internals.userMovedAt = -Infinity;
    const view = () => [
      internals.translateX,
      internals.translateY,
      internals.scale,
    ];
    const join = async (n: number) => {
      el.recordActivity('flow_executions', {
        type: 'execution_started',
        flow_id: `flow${n}`,
        execution_id: `run${n}`,
        payload: {
          status: 'RUNNING',
          fleet_context: { flow_name: `Flow ${n}` },
        },
      });
      await el.updateComplete;
      await el.updateComplete;
      await el.updateComplete;
    };
    // The camera drifted (a previous fit, a resize) but every node is in view.
    const nudge = () => {
      internals.translateX -= 8;
      internals.translateY -= 6;
      internals.applyTransform();
      return view();
    };
    await join(1);
    const fitted = view();
    const drifted = nudge();
    await join(2);
    expect(view()).not.to.deep.equal(drifted);
    expect(view()[2]).to.be.closeTo(fitted[2], 0.05);
    // Owned: the user's arrangement and camera stay put.
    const shown = el.shadowRoot!.querySelectorAll<HTMLElement>('[data-node]');
    internals.points = Object.fromEntries(
      [...shown].map((n) => [
        n.dataset.node!,
        { x: parseFloat(n.style.left), y: parseFloat(n.style.top) },
      ])
    );
    await el.updateComplete;
    const owned = nudge();
    await join(3);
    expect(view()).to.deep.equal(owned);
  });
});

describe('fleet-map hidden account layer', () => {
  it('shows the explicit model-provider relationship without changing the impact graph', async () => {
    const graph = sampleFleetMap();
    const before = JSON.stringify(graph.edges);
    const el = await map({ graph, positionsScope: 'account-summary' });
    expect(
      el.shadowRoot!.querySelector(
        'path[data-edge-id="summary:model:gpt>provider:openai"]'
      )
    ).to.exist;
    expect(JSON.stringify(graph.edges)).to.equal(before);
    el.shadowRoot!.querySelector<HTMLButtonElement>(
      '[data-node="provider:openai"]'
    )!.click();
    await el.updateComplete;
    expect(
      el.shadowRoot!.querySelector('[data-testid="fleet-map-panel"]')
        ?.textContent
    ).to.contain('Codex');
    expect(
      el.shadowRoot!.querySelector('[data-testid="fleet-map-panel"]')
        ?.textContent
    ).not.to.contain('Claude Code');
  });
});

describe('fleet-map window history', () => {
  afterEach(() => {
    for (const key of Object.keys(localStorage))
      if (key.startsWith('preloop:fleet-map')) localStorage.removeItem(key);
  });

  const at = (secondsAgo: number) =>
    new Date(Date.now() - secondsAgo * 1000).toISOString();
  const item = (
    kind: 'issue' | 'pull_request',
    n: number,
    extra: Record<string, unknown> = {}
  ) => ({
    key: `acme/api#${n}`,
    kind,
    reference: `#${n}`,
    repo: 'acme/api',
    title: `${kind === 'issue' ? 'Issue' : 'PR'} ${n}`,
    url: `https://example.com/acme/api/${kind === 'issue' ? 'issues' : 'pull'}/${n}`,
    state: 'open' as const,
    last_action: kind === 'issue' ? 'issue_labeled' : 'opened',
    last_at: at(120),
    review_state: null,
    labels: [],
    related_issue: null,
    node_ids: ['agent:claude'],
    ...extra,
  });
  const history = () => ({
    active_within_minutes: 60,
    since: at(3600),
    truncated: false,
    counts: {},
    flows: ['triage', 'implementation'].map((id) => ({
      id: `flow:${id}`,
      resource_id: id,
      label:
        id === 'triage' ? 'Issue triage' : 'Automated Issue Implementation',
      href: `/console/flows/${id}`,
      model_node_id: 'model:sonnet',
      last_active_at: at(300),
    })),
    events: [],
    work: [
      item('issue', 41, { labels: ['bug'] }),
      item('issue', 42),
      item('pull_request', 51, { related_issue: 'acme/api#41' }),
    ],
  });
  const ids = [
    'flow:triage',
    'flow:implementation',
    'work:issue:acme/api#41',
    'work:issue:acme/api#42',
    'work:pull_request:acme/api#51',
  ];

  it('shows every flow and work item of the window on load, in view, in both arrangements, with no live events', async () => {
    for (const arrangement of ['columns', 'spatial']) {
      const el = await map({
        spatialOnly: false,
        defaultArrangement: arrangement,
        positionsScope: `history-${arrangement}`,
        activityWindow: '1h',
      });
      el.codeActivity = history();
      await el.updateComplete;
      await el.updateComplete;
      await el.updateComplete;
      const stage = el
        .shadowRoot!.querySelector('.stage')!
        .getBoundingClientRect();
      for (const id of ids) {
        const node = el.shadowRoot!.querySelector(`[data-node="${id}"]`);
        expect(!!node, `${arrangement} ${id}`).to.equal(true);
        const r = node!.getBoundingClientRect();
        expect(
          r.left >= stage.left - 1 && r.right <= stage.right + 1,
          `${arrangement} ${id} x`
        ).to.equal(true);
        expect(
          r.top >= stage.top - 1 && r.bottom <= stage.bottom + 1,
          `${arrangement} ${id} y`
        ).to.equal(true);
      }
      const pr = el.shadowRoot!.querySelector<HTMLElement>(
        '[data-node="work:pull_request:acme/api#51"]'
      )!;
      expect(
        pr.classList.contains('child'),
        `${arrangement} PR under issue`
      ).to.equal(true);
      expect(
        el
          .shadowRoot!.querySelector(
            '[data-node="work:issue:acme/api#41"] [data-label]'
          )
          ?.textContent?.trim()
      ).to.equal('bug');
      // History is not traffic: nothing lit, no notes.
      expect(
        el.shadowRoot!.querySelectorAll('path.edge.active').length
      ).to.equal(0);
    }
  });

  it('hides and shows flows and work from the Show menu in both arrangements, and remembers it', async () => {
    for (const arrangement of ['columns', 'spatial']) {
      const scope = `show-${arrangement}`;
      const el = await map({
        spatialOnly: false,
        defaultArrangement: arrangement,
        positionsScope: scope,
        activityWindow: '1h',
      });
      el.codeActivity = history();
      await el.updateComplete;
      await el.updateComplete;
      const box = (type: string) =>
        el.shadowRoot!.querySelector<HTMLElement & { checked: boolean }>(
          `sl-menu-item[data-type="${type}"]`
        )!;
      expect(box('flow').textContent!.trim()).to.equal('Flows');
      expect(box('work').textContent!.trim()).to.equal('Work (issues, PRs)');
      expect(box('flow').checked && box('work').checked).to.equal(true);
      const count = (kind: string) =>
        el.shadowRoot!.querySelectorAll(`[data-kind="${kind}"]`).length;
      expect(count('flow')).to.equal(2);
      expect(count('work')).to.equal(3);
      for (const type of ['flow', 'work']) {
        box(type).dispatchEvent(new MouseEvent('click'));
        await el.updateComplete;
      }
      expect(count('flow'), arrangement).to.equal(0);
      expect(count('work'), arrangement).to.equal(0);
      // Remembered for this user and map.
      const again = await map({
        spatialOnly: false,
        defaultArrangement: arrangement,
        positionsScope: scope,
        activityWindow: '1h',
      });
      again.codeActivity = history();
      await again.updateComplete;
      await again.updateComplete;
      expect(
        again.shadowRoot!.querySelectorAll('[data-kind="work"]').length
      ).to.equal(0);
      for (const type of ['flow', 'work']) {
        box(type).dispatchEvent(new MouseEvent('click'));
        await el.updateComplete;
      }
      expect(count('flow')).to.equal(2);
      expect(count('work')).to.equal(3);
    }
  });
});

describe('fleet-map usage totals', () => {
  const totals = (extra: Record<string, unknown> = {}) => ({
    request_count: 12,
    input_tokens: 41200,
    output_tokens: 3800,
    cache_read_tokens: 900,
    cache_write_tokens: null,
    estimated_cost: 0.42,
    cost_is_estimate: true as const,
    currency: 'USD',
    partial: false,
    ...extra,
  });
  it('shows window totals for a selected model or provider, with the estimate said', async () => {
    const el = await map({ positionsScope: 'usage' });
    el.usage = {
      active_within_minutes: 60,
      since: '2026-10-20T09:00:00Z',
      until: '2026-10-20T10:00:00Z',
      truncated: false,
      partial: false,
      models: { 'model:sonnet': totals() },
      providers: {
        'provider:openai': totals({
          request_count: 1,
          estimated_cost: null,
          partial: true,
        }),
      },
    };
    const pick = async (id: string) => {
      el.shadowRoot!.querySelector<HTMLElement>(`[data-node="${id}"]`)!.click();
      await el.updateComplete;
      return el
        .shadowRoot!.querySelector('[data-testid="fleet-map-usage"]')
        ?.textContent?.replace(/\s+/g, ' ')
        .trim();
    };
    expect(await pick('model:sonnet')).to.equal(
      'In this window Requests 12 Tokens in 41.2k Tokens out 3.8k Cost est. $0.42'
    );
    expect(await pick('provider:openai')).to.equal(
      'In this window Requests 1 Tokens in 41.2k Tokens out 3.8k Cost est. Unknown Partial totals. Some values are unknown or the source is capped.'
    );
    expect(await pick('model:gpt')).to.equal(
      'In this window No gateway requests in this window.'
    );
    expect(await pick('agent:claude')).to.contain(
      'No usage reported for this resource'
    );
  });
});

describe('fleet-map Show memory', () => {
  afterEach(() => {
    for (const key of Object.keys(localStorage))
      if (key.startsWith('preloop:fleet-map')) localStorage.removeItem(key);
  });
  it('remembers a choice that hides every type', async () => {
    const el = await map({
      spatialOnly: false,
      defaultArrangement: 'columns',
      positionsScope: 'hide-all',
    });
    for (const box of el.shadowRoot!.querySelectorAll<
      HTMLElement & { checked: boolean }
    >('sl-menu-item[data-type]'))
      if (box.checked) {
        box.dispatchEvent(new MouseEvent('click'));
        await el.updateComplete;
      }
    expect(el.shadowRoot!.querySelectorAll('[data-node]').length).to.equal(0);
    const again = await map({
      spatialOnly: false,
      defaultArrangement: 'columns',
      positionsScope: 'hide-all',
    });
    expect(again.shadowRoot!.querySelectorAll('[data-node]').length).to.equal(
      0
    );
  });
});
