import { expect, fixture, html, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';
import { setViewport } from '@web/test-runner-commands';
import './live-activity-view';
import type { LiveActivityView } from './live-activity-view';
import type { ActivityBoard } from '../../live-activity-api';
import { mockApi, type MockApi } from '../../test-helpers/capability-api';
import { ACTIVITY_WINDOW_KEY } from '../../utils/live-activity';

function board(overrides: Partial<ActivityBoard> = {}): ActivityBoard {
  return {
    schema_version: 1,
    generated_at: new Date().toISOString(),
    window_minutes: 60,
    presence_enabled: true,
    viewer: { user_id: 'u1', can_see_people: true, can_see_devices: true },
    metrics: {
      active_people: 4,
      idle_people: 2,
      people_in_window: 9,
      active_agents: 7,
      waiting_agents: 3,
      requests_per_min: 12,
      requests_in_window: 504,
      tool_calls_per_min: 31,
      tool_calls_in_window: 1240,
      prompts_in_window: 96,
      spend_rate_usd_per_hour: 3.2,
      spend_in_window_usd: 2.71,
      reply_latency_median_s: 100,
      reply_samples: 38,
      approval_latency_median_s: 48,
      approval_samples: 2,
    },
    series: [
      {
        bucket_start: '2026-10-18T10:00:00Z',
        requests: 9,
        tool_calls: 22,
        prompts: 2,
        spend_usd: 0.05,
      },
      {
        bucket_start: '2026-10-18T10:01:00Z',
        requests: 4,
        tool_calls: 2,
        prompts: 1,
        spend_usd: 0.01,
      },
    ],
    people: [
      {
        user_id: 'u1',
        display_name: 'Dimitris',
        state: 'active',
        since: null,
        last_signal_at: new Date().toISOString(),
        last_signal_kind: 'prompt',
        awaiting_reply_since: null,
        awaiting_approval_count: 0,
        devices: [{ device_key: 'd', hostname: 'mbp-dimo', state: 'active' }],
        agents: ['a1'],
        reply_latency_median_s: 85,
        reply_samples: 14,
        approval_latency_median_s: null,
        approval_samples: 0,
        href: '/console/settings/users/u1',
      },
    ],
    agents: [
      {
        managed_agent_id: 'a1',
        display_name: 'Claude Code · preloop',
        state: 'idle',
        since: null,
        last_activity_at: null,
        owner_display_name: 'Dimitris',
        agent_kind: 'claude_code',
        device: { device_key: 'd', hostname: 'mbp-dimo' },
        session_title: 'Fix fleet map window',
        session_href: '/console/runtime-sessions/s1',
        href: '/console/agents/a1',
        requests_per_min: 4,
      },
    ],
    truncated: false,
    unavailable: [],
    ...overrides,
  };
}

describe('live-activity-view', () => {
  let api: MockApi | undefined;
  let payload: ActivityBoard;

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    payload = board();
  });
  afterEach(() => {
    api?.restore();
    api = undefined;
    localStorage.removeItem(ACTIVITY_WINDOW_KEY);
    document.body.style.overflow = '';
  });

  async function render(status = 200): Promise<LiveActivityView> {
    api = mockApi({
      routes: [{ path: '/api/v1/activity/board', status, body: () => payload }],
    });
    const el = await fixture<LiveActivityView>(
      html`<live-activity-view></live-activity-view>`
    );
    await waitUntil(() => el.board !== null || el.forbidden || el.unavailable);
    await el.updateComplete;
    return el;
  }

  const text = (el: LiveActivityView, sel: string) =>
    el
      .shadowRoot!.querySelector(sel)
      ?.textContent?.replace(/\s+/g, ' ')
      .trim() ?? '';

  it('renders the stat row as a dl and asks for the stored window', async () => {
    const el = await render();
    expect(el.shadowRoot!.querySelector('dl.stats')).to.exist;
    expect(el.shadowRoot!.querySelectorAll('dl.stats .stat')).to.have.length(7);
    expect(text(el, '[data-testid="stat-people"]')).to.contain('4');
    expect(text(el, '[data-testid="stat-spend"]')).to.contain('$3.20/h est.');
    expect(text(el, '[data-testid="stat-reply"]')).to.contain('1m 40s');
    expect(api!.callsTo('/api/v1/activity/board')[0].search).to.equal(
      '?window_minutes=60'
    );
  });

  it('renders "—" with a reason when samples are too few', async () => {
    const el = await render();
    const value = el.shadowRoot!.querySelector(
      '[data-testid="stat-approval"] .value'
    )!;
    expect(value.textContent!.trim()).to.equal('—');
    expect(value.getAttribute('title')).to.equal(
      'Fewer than 3 decisions in this window'
    );
  });

  it('shows the member view without view_users', async () => {
    payload = board({
      viewer: { user_id: 'u1', can_see_people: false, can_see_devices: false },
      people: [],
      me: board().people[0],
    });
    const el = await render();
    expect(text(el, '[data-testid="people-me"]')).to.contain(
      'People on this account are visible to administrators'
    );
    expect(el.shadowRoot!.querySelector('[data-testid="people"]')).to.equal(
      null
    );
  });

  it('says presence is off and that there are no agents', async () => {
    payload = board({ presence_enabled: false, agents: [] });
    const el = await render();
    expect(text(el, '[data-testid="people"]')).to.contain(
      'Presence is turned off for this account.'
    );
    expect(text(el, '[data-testid="agents"]')).to.contain(
      'preloop agents onboard'
    );
  });

  it('shows the permission message on 403', async () => {
    const el = await render(403);
    expect(text(el, '[data-testid="forbidden"]')).to.contain(
      'You do not have permission to view activity.'
    );
  });

  it('renders nothing when the plugin is absent (404)', async () => {
    const el = await render(404);
    expect(el.unavailable).to.equal(true);
    expect(el.shadowRoot!.querySelector('header')).to.equal(null);
  });

  it('says live data paused when the sweep is old', async () => {
    payload = board({
      generated_at: new Date(Date.now() - 180_000).toISOString(),
    });
    const el = await render();
    expect(text(el, '[data-testid="live"]')).to.contain('Live data paused');
  });

  it('persists a window change and refetches on its cadence', async () => {
    const clock = sinon.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const el = await render();
      el.shadowRoot!.querySelector('time-range-select')!.dispatchEvent(
        new CustomEvent('range-change', { detail: { value: '5m' } })
      );
      await waitUntil(
        () => api!.callsTo('/api/v1/activity/board').length === 2
      );
      expect(localStorage.getItem(ACTIVITY_WINDOW_KEY)).to.equal('5m');
      expect(api!.callsTo('/api/v1/activity/board')[1].search).to.equal(
        '?window_minutes=5'
      );
      await clock.tickAsync(30_000);
      await waitUntil(
        () => api!.callsTo('/api/v1/activity/board').length === 3
      );
    } finally {
      clock.restore();
    }
  });

  it('enters and leaves full screen with Esc and restores body overflow', async () => {
    const el = await render();
    const button = el.shadowRoot!.querySelector<HTMLButtonElement>(
      '[data-testid="activity-fullscreen"]'
    )!;
    expect(button.getAttribute('aria-label')).to.equal('Full screen');
    button.click();
    await el.updateComplete;
    expect(el.hasAttribute('fullscreen')).to.equal(true);
    expect(document.body.style.overflow).to.equal('hidden');
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    await el.updateComplete;
    await el.updateComplete;
    expect(el.fullscreen).to.equal(false);
    expect(document.body.style.overflow).to.equal('');
    el.fullscreen = true;
    await el.updateComplete;
    el.remove();
    expect(document.body.style.overflow).to.equal('');
  });

  it('flips an agent chip to Working without changing counts', async () => {
    const el = await render();
    const before = text(el, '[data-testid="stat-agents"]');
    el.onRealtime('gateway_activity', {
      type: 'model_gateway_request_started',
      payload: { managed_agent_id: 'a1' },
    });
    await el.updateComplete;
    expect(text(el, '[data-testid="agent-row"]')).to.contain('Working');
    expect(el.shadowRoot!.querySelector('.pulse')).to.exist;
    expect(text(el, '[data-testid="stat-agents"]')).to.equal(before);
  });

  it('applies metrics and presence events', async () => {
    const el = await render();
    el.onRealtime('activity_metrics', {
      type: 'activity_metrics_updated',
      payload: { active_agents: 11 },
    });
    el.onRealtime('presence', {
      type: 'presence_changed',
      payload: { subject_type: 'person', subject_id: 'u1', state: 'away' },
    });
    await el.updateComplete;
    expect(text(el, '[data-testid="stat-agents"]')).to.contain('11');
    expect(text(el, '[data-testid="person-row"]')).to.contain('Away');
  });

  it('announces politely at most every 30s', async () => {
    const el = await render();
    const first = el.liveText;
    expect(first).to.equal('4 people active, 7 agents working');
    el.onRealtime('activity_metrics', {
      type: 'activity_metrics_updated',
      payload: { active_agents: 2 },
    });
    expect(el.liveText).to.equal(first);
  });

  it('reserves the pulse slot on every row and lines the chips up', async () => {
    payload = board({
      agents: [
        {
          ...board().agents[0],
          managed_agent_id: 'a1',
          state: 'idle',
          requests_per_min: undefined,
        },
        {
          ...board().agents[0],
          managed_agent_id: 'a2',
          display_name: 'Codex',
          state: 'working',
          requests_per_min: 12.5,
        },
        {
          ...board().agents[0],
          managed_agent_id: 'a3',
          display_name: 'OpenCode',
          state: 'waiting_approval',
          since: new Date().toISOString(),
        },
      ],
    });
    const el = await render();
    const rows = [
      ...el.shadowRoot!.querySelectorAll<HTMLElement>(
        '[data-testid="agent-row"], [data-testid="person-row"]'
      ),
    ];
    for (const row of rows)
      expect(row.querySelector('[data-testid="pulse-slot"]')).to.exist;
    const idle = rows.find((row) => row.textContent!.includes('Idle'))!;
    expect(idle.querySelector('.pulse.on')).to.equal(null);
    const lefts = [
      ...el.shadowRoot!.querySelectorAll('[data-testid="agent-row"] sl-badge'),
    ].map((chip) => Math.round(chip.getBoundingClientRect().left));
    expect(lefts).to.have.length(3);
    expect(new Set(lefts).size).to.equal(1);
  });

  it('shows the agent kind icon and the device hostname', async () => {
    const el = await render();
    const row = el.shadowRoot!.querySelector('[data-testid="agent-row"]')!;
    const icon = row.querySelector('[data-testid="agent-kind-icon"]')!;
    expect(icon.getAttribute('src')).to.equal('/images/logos/claude.svg');
    expect(
      row.querySelector('[data-testid="agent-host"]')!.textContent!.trim()
    ).to.equal('mbp-dimo');
  });

  it('puts "Waiting on them" in an amber chip with its duration', async () => {
    payload = board({
      people: [
        {
          ...board().people[0],
          state: 'idle',
          since: new Date(Date.now() - 12 * 60_000).toISOString(),
          awaiting_reply_since: new Date(Date.now() - 42_000).toISOString(),
          awaiting_approval_count: 2,
        },
      ],
    });
    const el = await render();
    const row = el.shadowRoot!.querySelector('[data-testid="person-row"]')!;
    const chip = row.querySelector('[data-testid="waiting-on-them"]')!;
    expect(chip.getAttribute('variant')).to.equal('warning');
    expect(chip.textContent!.replace(/\s+/g, ' ').trim()).to.match(
      /^Waiting on them · 4[234]s$/
    );
    const approvals = row.querySelector('[data-testid="awaiting-approvals"]')!;
    expect(approvals.getAttribute('variant')).to.equal('warning');
    expect(approvals.textContent!.replace(/\s+/g, ' ').trim()).to.equal(
      '2 approvals'
    );
    expect(text(el, '[data-testid="person-row"] .state')).to.equal(
      'Idle · 12m'
    );
  });

  it('puts the stat label under its value and the meta line last', async () => {
    const el = await render();
    const tile = el.shadowRoot!.querySelector('[data-testid="stat-people"]')!;
    const top = (sel: string) =>
      tile.querySelector(sel)!.getBoundingClientRect().top;
    expect(top('.value')).to.be.lessThan(top('dt'));
    expect(top('dt')).to.be.lessThan(top('.meta-line'));
  });

  for (const [width, height] of [
    [1600, 1000],
    [1920, 1080],
  ])
    it(`keeps every stat on one line without overflow at ${width}x${height}`, async () => {
      await setViewport({ width, height });
      try {
        payload = board({
          metrics: {
            ...board().metrics,
            spend_rate_usd_per_hour: 117.43,
            spend_in_window_usd: 126.48,
            requests_per_min: 1234,
            reply_latency_median_s: 3725,
            approval_latency_median_s: 3725,
            approval_samples: 19,
          },
        });
        const el = await render();
        el.fullscreen = true;
        await el.updateComplete;
        const cells = [
          ...el.shadowRoot!.querySelectorAll<HTMLElement>('dl.stats .stat'),
        ];
        for (const cell of cells) {
          const value = cell.querySelector<HTMLElement>('.value')!;
          expect(value.scrollWidth, cell.dataset.testid).to.be.at.most(
            value.clientWidth
          );
          const lineHeight = parseFloat(getComputedStyle(value).lineHeight);
          expect(value.getBoundingClientRect().height).to.be.below(
            lineHeight * 1.5
          );
        }
        for (let i = 1; i < cells.length; i++)
          expect(
            cells[i - 1].getBoundingClientRect().right,
            cells[i].dataset.testid
          ).to.be.at.most(cells[i].getBoundingClientRect().left);
        expect(text(el, '[data-testid="stat-spend"] .value')).to.equal(
          '$117.43/h est.'
        );
      } finally {
        await setViewport({ width: 800, height: 600 });
      }
    });

  it('shows "+N more" in full screen instead of clipping a row', async () => {
    await setViewport({ width: 1600, height: 1000 });
    try {
      const base = board().agents[0];
      payload = board({
        agents: Array.from({ length: 40 }, (_, i) => ({
          ...base,
          managed_agent_id: `a${i}`,
          display_name: `Agent ${String(i).padStart(2, '0')}`,
        })),
      });
      const el = await render();
      el.fullscreen = true;
      await el.updateComplete;
      await waitUntil(() =>
        el.shadowRoot!.querySelector('[data-testid="more-agents"]')
      );
      const section = el.shadowRoot!.querySelector<HTMLElement>(
        '[data-testid="agents"]'
      )!;
      const rows = [
        ...section.querySelectorAll<HTMLElement>('[data-testid="agent-row"]'),
      ];
      const shown = rows.filter((row) => !row.hidden);
      const bottom = section.getBoundingClientRect().bottom;
      for (const row of shown)
        expect(row.getBoundingClientRect().bottom).to.be.at.most(bottom + 0.5);
      const footer = section.querySelector<HTMLElement>(
        '[data-testid="more-agents"]'
      )!;
      expect(footer.getBoundingClientRect().bottom).to.be.at.most(bottom + 0.5);
      expect(footer.textContent).to.contain(`+${40 - shown.length} more`);
      el.fullscreen = false;
      await el.updateComplete;
      await el.updateComplete;
      expect(rows.every((row) => !row.hidden)).to.equal(true);
      expect(section.querySelector('[data-testid="more-agents"]')).to.equal(
        null
      );
    } finally {
      await setViewport({ width: 800, height: 600 });
    }
  });

  it('blanks an idle agent at 0 req/min and keeps one decimal', async () => {
    payload = board({
      agents: [
        { ...board().agents[0], managed_agent_id: 'a1', requests_per_min: 0 },
        {
          ...board().agents[0],
          managed_agent_id: 'a2',
          display_name: 'Codex',
          state: 'working',
          requests_per_min: 12,
        },
      ],
    });
    const el = await render();
    const rates = [
      ...el.shadowRoot!.querySelectorAll('[data-testid="agent-row"] .num'),
    ].map((cell) => cell.textContent!.trim());
    expect(rates).to.eql(['12.0 req/min', '']);
  });

  it('ticks relative times every second on one timer', async () => {
    const clock = sinon.useFakeTimers({
      now: Date.parse('2026-10-18T10:00:00Z'),
      shouldAdvanceTime: false,
      toFake: ['setInterval', 'clearInterval', 'Date'],
    });
    try {
      payload = board({
        generated_at: new Date().toISOString(),
        people: [
          {
            ...board().people[0],
            last_signal_at: new Date(Date.now() - 10_000).toISOString(),
          },
        ],
      });
      const el = await render();
      expect(text(el, '[data-testid="person-row"] .num')).to.contain('10s ago');
      clock.tick(1000);
      await el.updateComplete;
      expect(text(el, '[data-testid="person-row"] .num')).to.contain('11s ago');
    } finally {
      clock.restore();
    }
  });

  it('keeps a client flip across a poll that predates it', async () => {
    payload = board({
      agents: [
        {
          ...board().agents[0],
          since: new Date(Date.now() - 600_000).toISOString(),
        },
      ],
    });
    const el = await render();
    el.onRealtime('gateway_activity', {
      type: 'model_gateway_request_started',
      payload: { managed_agent_id: 'a1' },
    });
    await el.load();
    await el.updateComplete;
    expect(text(el, '[data-testid="agent-row"]')).to.contain('Working');
  });

  it('labels the sparkline with the latest bucket, the peak and the average', async () => {
    const el = await render();
    expect(text(el, '[data-testid="sparkline-now"]')).to.equal(
      '4 req/min peak 9 · avg 6.5'
    );
    expect(el.shadowRoot!.querySelector('.sparkline line.ref')).to.exist;
  });
});
