import { expect, fixture, html, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';
import { unifiedWebSocketManager } from '../services/unified-websocket-manager';
import './preloop-session-observer';
import './session-chat-view';
import './session-replay-panel';
import './session-artifact-row';
import './session-artifact-summary';
import type { PreloopSessionObserver } from './preloop-session-observer';
import type { SessionChatView } from './session-chat-view';
import type { SessionReplayPanel } from './session-replay-panel';
import type { SessionArtifactRow } from './session-artifact-row';
import type { SessionArtifactSummary } from './session-artifact-summary';
import type { BrowserStepThumbnail } from './browser-step-thumbnail';
import type {
  FlowGatewayEvent,
  RuntimeSessionActivityItem,
  RuntimeSessionArtifactDescriptor,
} from '../types';
import { heldSessionArtifactCount } from '../utils/session-artifacts';

const SESSION_ID = '11111111-1111-4111-8111-111111111111';
const TRANSCRIPT = 'a0000000-0000-4000-8000-000000000001';
const SUMMARY = 'a0000000-0000-4000-8000-000000000002';
const SHOT = 'a0000000-0000-4000-8000-000000000003';
const AUDIO = 'a0000000-0000-4000-8000-000000000004';
const EVICTED = 'a0000000-0000-4000-8000-000000000005';

const PNG = Uint8Array.from(
  atob(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII='
  ),
  (c) => c.charCodeAt(0)
);

const TRANSCRIPT_TEXT = [
  'WEBVTT',
  '',
  '00:00.000 --> 00:04.000',
  'Agent: Thanks for calling the Heilbronn warehouse.',
  ...Array.from({ length: 300 }, (_, i) => `line ${i + 5}`),
].join('\n');
const SUMMARY_TEXT =
  '# Call summary\nCaller asked about order 4411.\nResolved.';

type Spec = {
  id: string;
  kind: string;
  name: string;
  contentType: string;
  at: string;
  labels?: Record<string, unknown>;
  parent?: string;
  availability?: string;
};

const SPECS: Spec[] = [
  {
    id: TRANSCRIPT,
    kind: 'transcript',
    name: 'call-4411.vtt',
    contentType: 'text/vtt',
    at: '2026-10-01T10:00:01Z',
    labels: { tags: ['demo'], consent_basis: 'contract', site: 'heilbronn' },
  },
  {
    id: SUMMARY,
    kind: 'document',
    name: 'call-4411-summary.md',
    contentType: 'text/markdown',
    at: '2026-10-01T10:00:03Z',
    parent: TRANSCRIPT,
  },
  {
    id: SHOT,
    kind: 'screenshot',
    name: 'order-4411.png',
    contentType: 'image/png',
    at: '2026-10-01T10:00:05Z',
  },
  {
    id: AUDIO,
    kind: 'audio',
    name: 'call-4411.ogg',
    contentType: 'audio/ogg',
    at: '2026-10-01T10:00:06Z',
  },
  {
    id: EVICTED,
    kind: 'screenshot',
    name: 'old-dashboard.png',
    contentType: 'image/png',
    at: '2026-10-01T10:00:08Z',
    availability: 'evicted',
  },
];

function row(spec: Spec): RuntimeSessionActivityItem {
  return {
    activity_type: 'artifact',
    timestamp: spec.at,
    title: `${spec.kind} ${spec.name}`,
    summary: `${spec.kind} ${spec.name}`,
    status: 'success',
    api_usage_id: null,
    tool_name: null,
    server_name: 'deposit_api',
    auth_subject_type: null,
    api_key_id: null,
    api_key_name: null,
    estimated_cost: null,
    total_tokens: null,
    metadata: {
      artifact: {
        id: spec.id,
        kind: spec.kind,
        name: spec.name,
        content_type: spec.contentType,
        size_bytes: 1234,
        labels: spec.labels || {},
        producer: 'deposit_api',
      },
    },
  };
}

function descriptor(spec: Spec): RuntimeSessionArtifactDescriptor {
  return {
    id: spec.id,
    runtime_session_id: SESSION_ID,
    kind: spec.kind,
    name: spec.name,
    content_type: spec.contentType,
    size_bytes: 1234,
    sha256: `${spec.id.slice(-1)}`.repeat(64),
    labels: spec.labels || {},
    producer: 'deposit_api',
    parent_artifact_id: spec.parent || null,
    availability: spec.availability || 'available',
    legal_hold: false,
    created_at: spec.at,
    content_block: {
      type: 'resource_link',
      uri: `/api/v1/runtime-sessions/${SESSION_ID}/artifacts/${spec.id}`,
      name: spec.name,
      mimeType: spec.contentType,
      size: 1234,
    },
  };
}

const ROWS = SPECS.map(row);
const DESCRIPTORS = Object.fromEntries(
  SPECS.map((spec) => [spec.id, descriptor(spec)])
);

function modelEvent(
  id: string,
  timestamp: string,
  text: string
): FlowGatewayEvent {
  return {
    id,
    type: 'model_gateway_request',
    timestamp,
    flow_id: null,
    flow_execution_id: null,
    payload: {
      model_alias: 'test-model',
      conversation_preview: { messages: [{ role: 'assistant', text }] },
    },
  } as unknown as FlowGatewayEvent;
}

const EVENTS = [
  modelEvent('e1', '2026-10-01T10:00:02Z', 'Transcript saved, summarising.'),
  modelEvent('e2', '2026-10-01T10:00:07Z', 'Done with the call.'),
];

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function deepQueryAll(root: ParentNode, selector: string): HTMLElement[] {
  const found: HTMLElement[] = [];
  const walk = (node: ParentNode) => {
    found.push(...Array.from(node.querySelectorAll<HTMLElement>(selector)));
    node.querySelectorAll('*').forEach((child) => {
      const shadow = (child as HTMLElement).shadowRoot;
      if (shadow) walk(shadow);
    });
  };
  walk(root);
  return found;
}

describe('artifacts in the session timeline', () => {
  let fetchStub: sinon.SinonStub;
  let connectStub: sinon.SinonStub;
  let subscribeStub: sinon.SinonStub;
  let artifactList: RuntimeSessionArtifactDescriptor[];

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    artifactList = Object.values(DESCRIPTORS).reverse();
    connectStub = sinon.stub(unifiedWebSocketManager, 'connect').resolves();
    subscribeStub = sinon
      .stub(unifiedWebSocketManager, 'subscribe')
      .returns(() => undefined);
    fetchStub = sinon.stub(window, 'fetch').callsFake(async (input) => {
      const url = String(input instanceof Request ? input.url : input);
      if (url.includes(`/artifacts/${EVICTED}`)) {
        return json({ availability: 'evicted' }, 410);
      }
      if (url.includes(`/artifacts/${TRANSCRIPT}`)) {
        return new Response(TRANSCRIPT_TEXT, {
          headers: { 'Content-Type': 'text/vtt' },
        });
      }
      if (url.includes(`/artifacts/${SUMMARY}`)) {
        return new Response(SUMMARY_TEXT, {
          headers: { 'Content-Type': 'text/markdown' },
        });
      }
      if (url.includes(`/artifacts/${SHOT}`)) {
        return new Response(new Blob([PNG], { type: 'image/png' }));
      }
      if (url.includes(`/artifacts/${AUDIO}`)) {
        return new Response(
          new Blob([new Uint8Array(8)], { type: 'audio/ogg' })
        );
      }
      if (url.includes('/artifacts?')) {
        return json({ items: artifactList, next_cursor: null });
      }
      if (url.includes('/activity')) return json({ items: ROWS });
      if (url.includes('/gateway-events')) {
        return json({ logs: EVENTS, pagination: { has_more: false } });
      }
      return json({});
    });
  });

  afterEach(() => {
    fetchStub.restore();
    connectStub.restore();
    subscribeStub.restore();
    localStorage.clear();
    window.history.replaceState({}, '', window.location.pathname);
  });

  function artifactFetches(): string[] {
    return fetchStub
      .getCalls()
      .map((c) => String(c.args[0]))
      .filter((u) => /\/artifacts\/[^?]/.test(u));
  }

  it('renders five artifact rows interleaved with model turns, excerpt, chips and 410', async () => {
    const el = await fixture<SessionChatView>(html`
      <session-chat-view
        .sessionId=${SESSION_ID}
        .events=${EVENTS}
        .activity=${ROWS}
        .artifacts=${DESCRIPTORS}
      ></session-chat-view>
    `);
    await el.updateComplete;
    const rows = Array.from(
      el.shadowRoot!.querySelectorAll<SessionArtifactRow>(
        'session-artifact-row'
      )
    );
    expect(rows).to.have.length(5);

    // Time order: transcript (:01) < model e1 (:02) < summary (:03) <
    // screenshot (:05) < audio (:06) < model e2 (:07) < evicted (:08).
    const thread = el.shadowRoot!.innerHTML;
    const at = (needle: string) => thread.indexOf(needle);
    expect(at(`artifact-${TRANSCRIPT}`)).to.be.lessThan(at('Transcript saved'));
    expect(at('Transcript saved')).to.be.lessThan(at(`artifact-${SUMMARY}`));
    expect(at(`artifact-${AUDIO}`)).to.be.lessThan(at('Done with the call'));
    expect(at('Done with the call')).to.be.lessThan(at(`artifact-${EVICTED}`));

    // Excerpt without any click: first three lines only.
    const transcript = rows[0].shadowRoot!;
    await waitUntil(
      () =>
        transcript
          .querySelector('[data-testid="artifact-excerpt"]')
          ?.textContent?.includes('00:00.000'),
      'transcript excerpt never loaded'
    );
    const excerpt = transcript.querySelector(
      '[data-testid="artifact-excerpt"]'
    )!.textContent!;
    expect(excerpt).to.contain('WEBVTT');
    expect(excerpt).to.not.contain('Heilbronn warehouse');

    // The excerpt read is bounded and carries the user token.
    const call = fetchStub
      .getCalls()
      .find((c) => String(c.args[0]).includes(`/artifacts/${TRANSCRIPT}`))!;
    expect(String(call.args[0])).to.equal(
      `/api/v1/runtime-sessions/${SESSION_ID}/artifacts/${TRANSCRIPT}`
    );
    expect(
      new Headers((call.args[1] as RequestInit).headers).get('Authorization')
    ).to.equal('Bearer test-access-token');

    // Chips: site and consent_basis first.
    const chips = Array.from(
      transcript.querySelectorAll<HTMLElement>('[data-label]')
    ).map((chip) => chip.dataset.label);
    expect(chips).to.deep.equal(['site', 'consent_basis', 'tags']);
    expect(transcript.textContent).to.contain('site: heilbronn');
    expect(
      transcript.querySelector('[data-testid="artifact-sha"]')!.textContent
    ).to.contain('sha256 111111111111');

    // Lineage.
    const summary = rows[1].shadowRoot!;
    expect(
      summary.querySelector('[data-testid="artifact-lineage"]')!.textContent
    ).to.contain('derived from call-4411.vtt');

    // Image alt text from the name.
    const thumb = rows[2].shadowRoot!.querySelector(
      'browser-step-thumbnail'
    ) as BrowserStepThumbnail;
    await waitUntil(() => thumb.shadowRoot!.querySelector('img'));
    expect(thumb.shadowRoot!.querySelector('img')!.alt).to.equal(
      'order-4411.png'
    );

    // Audio is not fetched until play.
    expect(artifactFetches().some((u) => u.includes(AUDIO))).to.equal(false);

    // Evicted: grey row, reason, storage link, never fetched.
    const evicted = rows[4].shadowRoot!;
    const reason = evicted.querySelector(
      '[data-testid="artifact-unavailable"]'
    )!;
    expect(reason.textContent).to.contain('Evicted');
    expect(reason.querySelector('a')!.textContent).to.contain('Storage budget');
    expect(reason.querySelector('a')!.getAttribute('href')).to.equal(
      '/console/settings/account#session-artifact-storage'
    );
    expect(
      evicted
        .querySelector('[data-testid="artifact-row"]')!
        .classList.contains('gone')
    ).to.equal(true);
    expect(artifactFetches().some((u) => u.includes(EVICTED))).to.equal(false);
  });

  it('shows the 410 state when the byte route says the bytes are gone', async () => {
    const stale = { ...SPECS[1], id: EVICTED, availability: 'available' };
    const el = await fixture<SessionArtifactRow>(html`
      <session-artifact-row
        .item=${row(stale)}
        .artifact=${{
          id: EVICTED,
          kind: 'document',
          group: 'document',
          name: 'stale.md',
          contentType: 'text/markdown',
          sizeBytes: 10,
          labels: {},
          producer: 'deposit_api',
          toolName: null,
          sha256: null,
          parentArtifactId: null,
          availability: 'available',
        }}
        .sessionId=${SESSION_ID}
      ></session-artifact-row>
    `);
    await waitUntil(() =>
      el.shadowRoot!.querySelector('[data-testid="artifact-unavailable"]')
    );
    expect(
      el
        .shadowRoot!.querySelector('[data-testid="artifact-row"]')!
        .getAttribute('data-availability')
    ).to.equal('evicted');
  });

  it('expands to 200 lines on Show more, and plays audio only on request', async () => {
    const el = await fixture<SessionChatView>(html`
      <session-chat-view
        .sessionId=${SESSION_ID}
        .events=${[]}
        .activity=${[ROWS[0], ROWS[3]]}
        .artifacts=${DESCRIPTORS}
      ></session-chat-view>
    `);
    const [transcript, audio] = Array.from(
      el.shadowRoot!.querySelectorAll<SessionArtifactRow>(
        'session-artifact-row'
      )
    );
    await waitUntil(() =>
      transcript.shadowRoot!.querySelector('[data-testid="artifact-show-more"]')
    );
    (
      transcript.shadowRoot!.querySelector(
        '[data-testid="artifact-show-more"]'
      ) as HTMLButtonElement
    ).click();
    await waitUntil(() =>
      transcript
        .shadowRoot!.querySelector('[data-testid="artifact-excerpt"]')!
        .textContent!.includes('Heilbronn warehouse')
    );
    const lines = transcript
      .shadowRoot!.querySelector('[data-testid="artifact-excerpt"]')!
      .textContent!.split('\n');
    // 200 lines plus the "..." marker.
    expect(lines).to.have.length(201);

    (
      audio.shadowRoot!.querySelector(
        '[data-testid="artifact-play"]'
      ) as HTMLButtonElement
    ).click();
    await waitUntil(() =>
      audio.shadowRoot!.querySelector('[data-testid="artifact-audio"]')
    );
    const player = audio.shadowRoot!.querySelector('audio')!;
    expect(player.hasAttribute('controls')).to.equal(true);
    expect(player.src.startsWith('blob:')).to.equal(true);
    el.remove();
    expect(heldSessionArtifactCount()).to.equal(0);
  });

  it('rows are focusable and Enter opens an image in the viewer', async () => {
    const el = await fixture<SessionArtifactRow>(html`
      <session-artifact-row
        .item=${ROWS[2]}
        .artifact=${{
          id: SHOT,
          kind: 'screenshot',
          group: 'screenshot',
          name: 'order-4411.png',
          contentType: 'image/png',
          sizeBytes: 10,
          labels: {},
          producer: 'deposit_api',
          toolName: null,
          sha256: null,
          parentArtifactId: null,
          availability: 'available',
        }}
        .sessionId=${SESSION_ID}
      ></session-artifact-row>
    `);
    expect(el.tabIndex).to.equal(0);
    el.focus();
    const opened = new Promise<CustomEvent>((resolve) =>
      el.addEventListener('artifact-open', (e) => resolve(e as CustomEvent), {
        once: true,
      })
    );
    el.dispatchEvent(
      new KeyboardEvent('keydown', {
        key: 'Enter',
        bubbles: true,
        composed: true,
      })
    );
    expect((await opened).detail.artifactId).to.equal(SHOT);
  });

  it('header shows Artifacts 5 with four kind icons, filters to one transcript row and clears with the chip', async () => {
    const el = await fixture<PreloopSessionObserver>(html`
      <preloop-session-observer
        .sessions=${[
          {
            id: SESSION_ID,
            session_source_type: 'claude_code',
            runtime_principal_name: 'Call agent',
            started_at: '2026-10-01T10:00:00Z',
            last_activity_at: '2026-10-01T10:00:08Z',
            total_requests: 2,
          },
        ]}
        defaultReplayMode="conversation"
      ></preloop-session-observer>
    `);
    const summaryEl = () =>
      el.shadowRoot!.querySelector(
        'session-artifact-summary'
      ) as SessionArtifactSummary | null;
    await waitUntil(
      () =>
        summaryEl()?.shadowRoot?.querySelector(
          '[data-testid="artifact-summary"]'
        ),
      'summary never rendered',
      { timeout: 4000 }
    );
    const summary = summaryEl()!.shadowRoot!;
    expect(summary.textContent!.replace(/\s+/g, ' ')).to.contain('Artifacts 5');
    const kinds = Array.from(
      summary.querySelectorAll<HTMLButtonElement>('button.kind')
    );
    expect(kinds.map((b) => b.dataset.kind)).to.deep.equal([
      'screenshot',
      'transcript',
      'document',
      'audio',
    ]);

    const chat = el.shadowRoot!.querySelector(
      'session-chat-view'
    ) as SessionChatView;
    const visibleRows = () =>
      chat.shadowRoot!.querySelectorAll('session-artifact-row').length;
    await waitUntil(() => visibleRows() === 5, 'rows never rendered');

    kinds.find((b) => b.dataset.kind === 'transcript')!.click();
    await el.updateComplete;
    await chat.updateComplete;
    expect(visibleRows()).to.equal(1);
    expect(chat.shadowRoot!.textContent).to.not.contain('Done with the call');
    await summaryEl()!.updateComplete;
    const chip = summary.querySelector(
      '[data-testid="artifact-filter-chip"]'
    ) as HTMLElement;
    expect(chip).to.exist;
    chip.dispatchEvent(new CustomEvent('sl-remove', { bubbles: true }));
    await el.updateComplete;
    await chat.updateComplete;
    expect(visibleRows()).to.equal(5);
    expect(
      summary.querySelector('[data-testid="artifact-filter-chip"]')
    ).to.equal(null);
  });

  it('empty state explains how to produce artifacts, with docs and Tools links', async () => {
    const el = await fixture<SessionArtifactSummary>(html`
      <session-artifact-summary .counts=${{}}></session-artifact-summary>
    `);
    const trigger = el.shadowRoot!.querySelector(
      '[data-testid="artifact-summary-empty"]'
    ) as HTMLButtonElement;
    expect(trigger.textContent).to.contain('Artifacts 0');
    trigger.click();
    await el.updateComplete;
    const popover = el.shadowRoot!.querySelector(
      '[data-testid="artifact-empty-state"]'
    )!;
    expect(popover.textContent!.replace(/\s+/g, ' ')).to.contain(
      'No artifacts yet. Agents can save transcripts, screenshots and files with the deposit_artifact tool or the API.'
    );
    expect(
      popover
        .querySelector('[data-testid="artifact-docs-link"]')!
        .getAttribute('href')
    ).to.equal('https://docs.preloop.ai/guide/artifacts');
    expect(
      popover
        .querySelector('[data-testid="artifact-tools-link"]')!
        .getAttribute('href')
    ).to.equal('/console/tools');
  });

  it('?artifact=<id> scrolls to and highlights the row', async () => {
    const el = await fixture<PreloopSessionObserver>(html`
      <preloop-session-observer
        .sessions=${[
          {
            id: SESSION_ID,
            session_source_type: 'claude_code',
            runtime_principal_name: 'Call agent',
            started_at: '2026-10-01T10:00:00Z',
            last_activity_at: '2026-10-01T10:00:08Z',
            total_requests: 2,
          },
        ]}
        .focusArtifactId=${SUMMARY}
        defaultReplayMode="conversation"
      ></preloop-session-observer>
    `);
    const highlightedInChat = () => {
      const chat = el.shadowRoot!.querySelector('session-chat-view');
      return chat?.shadowRoot
        ? deepQueryAll(chat.shadowRoot, 'session-artifact-row[highlighted]')
        : [];
    };
    await waitUntil(
      () => highlightedInChat().length === 1,
      'row never highlighted',
      { timeout: 4000 }
    );
    const [highlighted] = highlightedInChat() as SessionArtifactRow[];
    expect(highlighted.artifact!.id).to.equal(SUMMARY);
    await waitUntil(
      () =>
        (highlighted.getRootNode() as ShadowRoot).activeElement === highlighted,
      'row never focused'
    );
  });

  it('renders artifact turns in the transcript panel, never as tool calls', async () => {
    const panel = await fixture<SessionReplayPanel>(html`
      <session-replay-panel
        .session=${{ id: SESSION_ID, canLoadEvents: true } as any}
        .events=${EVENTS}
        .activity=${ROWS}
        .artifacts=${DESCRIPTORS}
        replayMode="timeline"
      ></session-replay-panel>
    `);
    await panel.updateComplete;
    const turns = Array.from(
      panel.shadowRoot!.querySelectorAll<HTMLElement>('.chat-turn')
    );
    expect(
      turns.filter((t) => t.classList.contains('artifact-turn'))
    ).to.have.length(5);
    expect(panel.scrollToArtifact(AUDIO)).to.equal(true);
    panel.artifactKindFilter = 'audio';
    await panel.updateComplete;
    expect(panel.shadowRoot!.querySelectorAll('.chat-turn')).to.have.length(1);
  });
});
