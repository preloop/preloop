import { expect, fixture, html, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';
import './session-chat-view';
import './session-replay-panel';
import './browser-step-strip';
import './browser-step-row';
import './artifact-image-viewer';
import type { SessionChatView } from './session-chat-view';
import type { SessionReplayPanel } from './session-replay-panel';
import type { BrowserStepStrip } from './browser-step-strip';
import type { ArtifactImageViewer } from './artifact-image-viewer';
import type { BrowserStepThumbnail } from './browser-step-thumbnail';
import type { FlowGatewayEvent, RuntimeSessionActivityItem } from '../types';
import { heldSessionArtifactCount } from '../utils/session-artifacts';

const SESSION_ID = '11111111-1111-4111-8111-111111111111';
const SHOT_A = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa';
const SHOT_B = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb';
const SHOT_GONE = 'cccccccc-cccc-4ccc-8ccc-cccccccccccc';

// 1x1 transparent PNG.
const PNG = Uint8Array.from(
  atob(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII='
  ),
  (c) => c.charCodeAt(0)
);

function step(
  index: number,
  timestamp: string,
  action: string,
  extra: Partial<Record<string, unknown>> = {}
): RuntimeSessionActivityItem {
  return {
    activity_type: 'browser_step',
    timestamp,
    title: `${action} https://claims.example.test/`,
    summary: null,
    status: 'success',
    api_usage_id: null,
    tool_name: action,
    server_name: null,
    auth_subject_type: null,
    api_key_id: null,
    api_key_name: null,
    estimated_cost: null,
    total_tokens: null,
    metadata: {
      source: 'playwright_mcp',
      source_step_id: `call-${index}`,
      step_index: index,
      action,
      url: 'https://claims.example.test/form',
      target: null,
      reasoning: null,
      screenshot: null,
      ...extra,
    },
  };
}

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
      conversation_preview: {
        messages: [{ role: 'assistant', text }],
      },
    },
  } as unknown as FlowGatewayEvent;
}

const STEPS = [
  step(0, '2026-10-01T10:00:01Z', 'navigate', {
    screenshot: {
      artifact_id: SHOT_A,
      availability: 'available',
      content_type: 'image/png',
      size_bytes: PNG.length,
    },
  }),
  step(1, '2026-10-01T10:00:03Z', 'click', {
    target: 'button "Submit claim"',
    reasoning: 'The form is complete, so submit it.',
    screenshot: {
      artifact_id: SHOT_GONE,
      availability: 'evicted',
      content_type: 'image/png',
      size_bytes: 10,
    },
  }),
  step(2, '2026-10-01T10:00:05Z', 'screenshot', {
    screenshot: {
      artifact_id: SHOT_B,
      availability: 'available',
      content_type: 'image/png',
      size_bytes: PNG.length,
    },
  }),
];

describe('browser steps in the session timeline', () => {
  let fetchStub: sinon.SinonStub;

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    fetchStub = sinon.stub(window, 'fetch').callsFake(async (input) => {
      const url = String(input instanceof Request ? input.url : input);
      if (url.includes(SHOT_GONE)) {
        return new Response(JSON.stringify({ availability: 'evicted' }), {
          status: 410,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      if (url.includes('/artifacts/')) {
        return new Response(new Blob([PNG], { type: 'image/png' }), {
          status: 200,
          headers: { 'Content-Type': 'image/png' },
        });
      }
      return new Response('{}', { status: 404 });
    });
  });

  afterEach(() => {
    fetchStub.restore();
    localStorage.clear();
  });

  function rowsOf(root: ShadowRoot | null): HTMLElement[] {
    return Array.from(root?.querySelectorAll('browser-step-row') || []);
  }

  it('renders browser steps inline between model turns in the conversation', async () => {
    const el = await fixture<SessionChatView>(html`
      <session-chat-view
        .sessionId=${SESSION_ID}
        .events=${[
          modelEvent('e1', '2026-10-01T10:00:00Z', 'Opening the claim form.'),
          modelEvent('e2', '2026-10-01T10:00:04Z', 'Submitted, checking.'),
        ]}
        .activity=${STEPS}
      ></session-chat-view>
    `);
    await el.updateComplete;
    const rows = rowsOf(el.shadowRoot);
    expect(rows).to.have.length(3);
    const thread = el.shadowRoot!.innerHTML;
    // Step 1 (10:00:03) sits before the model turn at 10:00:04.
    const clickRow = el.shadowRoot!.querySelector(
      '[data-browser-step-key="browser-step-playwright_mcp-call-1"]'
    ) as HTMLElement;
    expect(clickRow).to.exist;
    expect(thread.indexOf('browser-step-playwright_mcp-call-1')).to.be.lessThan(
      thread.lastIndexOf('browser-step-playwright_mcp-call-2')
    );
    const clickContent = rows[1].shadowRoot!;
    expect(clickContent.textContent).to.contain('click');
    expect(clickContent.textContent).to.contain('button "Submit claim"');
    expect(clickContent.textContent).to.contain('#1');
    const reasoning = clickContent.querySelector('details')!;
    expect(reasoning.open).to.equal(false);
    expect(reasoning.textContent).to.contain('submit it');
    expect(
      el.scrollToBrowserStep('browser-step-playwright_mcp-call-1')
    ).to.equal(true);
  });

  it('fetches thumbnails with the user token and shows evicted reasons', async () => {
    const row = await fixture(html`
      <browser-step-row
        .item=${STEPS[0]}
        .sessionId=${SESSION_ID}
      ></browser-step-row>
    `);
    const thumb = row.shadowRoot!.querySelector(
      'browser-step-thumbnail'
    ) as BrowserStepThumbnail;
    await waitUntil(
      () =>
        thumb.shadowRoot!.querySelector('[data-testid="screenshot-thumbnail"]'),
      'thumbnail never loaded'
    );
    const call = fetchStub
      .getCalls()
      .find((c) => String(c.args[0]).includes(SHOT_A))!;
    expect(String(call.args[0])).to.equal(
      `/api/v1/runtime-sessions/${SESSION_ID}/artifacts/${SHOT_A}`
    );
    const headers = new Headers((call.args[1] as RequestInit).headers);
    expect(headers.get('Authorization')).to.equal('Bearer test-access-token');
    const img = thumb.shadowRoot!.querySelector('img')!;
    expect(img.src.startsWith('blob:')).to.equal(true);

    const opened = new Promise<CustomEvent>((resolve) =>
      row.addEventListener(
        'browser-step-open',
        (e) => resolve(e as CustomEvent),
        {
          once: true,
        }
      )
    );
    (thumb.shadowRoot!.querySelector('button') as HTMLButtonElement).click();
    expect((await opened).detail.key).to.equal(
      'browser-step-playwright_mcp-call-0'
    );

    row.remove();
    expect(heldSessionArtifactCount()).to.equal(0);
  });

  it('shows a 410 placeholder with the reason and a storage link', async () => {
    // Metadata still says available; the byte route answers 410.
    const stale = step(5, '2026-10-01T10:00:09Z', 'navigate', {
      screenshot: { artifact_id: SHOT_GONE, availability: 'available' },
    });
    const row = await fixture(html`
      <browser-step-row
        .item=${stale}
        .sessionId=${SESSION_ID}
      ></browser-step-row>
    `);
    const thumb = row.shadowRoot!.querySelector(
      'browser-step-thumbnail'
    ) as BrowserStepThumbnail;
    await waitUntil(() =>
      thumb.shadowRoot!.querySelector('[data-testid="screenshot-unavailable"]')
    );
    const box = thumb.shadowRoot!.querySelector(
      '[data-testid="screenshot-unavailable"]'
    )!;
    expect(box.textContent).to.contain('evicted');
    expect(box.querySelector('a')!.getAttribute('href')).to.equal(
      '/console/settings/account#session-artifact-storage'
    );

    // Metadata already evicted: no fetch at all.
    fetchStub.resetHistory();
    const evicted = await fixture(html`
      <browser-step-row
        .item=${STEPS[1]}
        .sessionId=${SESSION_ID}
      ></browser-step-row>
    `);
    const evictedThumb = evicted.shadowRoot!.querySelector(
      'browser-step-thumbnail'
    ) as BrowserStepThumbnail;
    await evictedThumb.updateComplete;
    expect(
      evictedThumb.shadowRoot!.querySelector(
        '[data-testid="screenshot-unavailable"]'
      )
    ).to.exist;
    const urls = fetchStub.getCalls().map((c) => String(c.args[0]));
    expect(urls.filter((u) => u.includes('/artifacts/'))).to.deep.equal([]);
  });

  it('opens the viewer full size, pages with arrows and closes on Escape', async () => {
    const images = STEPS.map((item, i) => ({
      key: `k${i}`,
      artifactId: (item.metadata as any).screenshot.artifact_id,
      availability: (item.metadata as any).screenshot.availability,
      title: `Step ${i + 1}`,
    }));
    const viewer = await fixture<ArtifactImageViewer>(html`
      <artifact-image-viewer
        .sessionId=${SESSION_ID}
        .images=${images}
        .index=${0}
      ></artifact-image-viewer>
    `);
    await waitUntil(() =>
      viewer.shadowRoot!.querySelector('[data-testid="viewer-image"]')
    );
    const nav: number[] = [];
    viewer.addEventListener('viewer-navigate', (e) => {
      nav.push((e as CustomEvent).detail.index);
      viewer.index = (e as CustomEvent).detail.index;
    });
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight' }));
    await viewer.updateComplete;
    expect(nav).to.deep.equal([1]);
    expect(
      viewer.shadowRoot!.querySelector('[data-testid="viewer-unavailable"]')!
        .textContent
    ).to.contain('evicted');
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowLeft' }));
    expect(nav).to.deep.equal([1, 0]);

    let closed = false;
    viewer.addEventListener('viewer-close', () => {
      closed = true;
      viewer.index = -1;
    });
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    await viewer.updateComplete;
    expect(closed).to.equal(true);
    expect(viewer.shadowRoot!.querySelector('[role="dialog"]')).to.equal(null);
  });

  it('header strip lists every step and emits a scrub for the clicked one', async () => {
    const strip = await fixture<BrowserStepStrip>(html`
      <browser-step-strip
        .steps=${STEPS}
        .sessionId=${SESSION_ID}
      ></browser-step-strip>
    `);
    const buttons = strip.shadowRoot!.querySelectorAll('button');
    expect(buttons).to.have.length(3);
    expect(strip.shadowRoot!.textContent!.replace(/\s+/g, ' ')).to.contain(
      '3 browser steps, 3 with screenshots'
    );
    const scrub = new Promise<CustomEvent>((resolve) =>
      strip.addEventListener(
        'browser-step-scrub',
        (e) => resolve(e as CustomEvent),
        {
          once: true,
        }
      )
    );
    (buttons[2] as HTMLButtonElement).click();
    expect((await scrub).detail.key).to.equal(
      'browser-step-playwright_mcp-call-2'
    );
  });

  it('renders steps as turns in the transcript panel, not as tool calls', async () => {
    const panel = await fixture<SessionReplayPanel>(html`
      <session-replay-panel
        .session=${{ id: SESSION_ID, canLoadEvents: true } as any}
        .events=${[modelEvent('e1', '2026-10-01T10:00:02Z', 'Filling the form.')]}
        .activity=${STEPS}
        replayMode="timeline"
      ></session-replay-panel>
    `);
    await panel.updateComplete;
    const turns = Array.from(
      panel.shadowRoot!.querySelectorAll('.chat-turn')
    ) as HTMLElement[];
    const kinds = turns.map((t) =>
      t.classList.contains('browser-step-turn') ? 'step' : 'model'
    );
    // The panel sorts newest first: step 2 (:05), step 1 (:03), model (:02), step 0 (:01).
    expect(kinds).to.deep.equal(['step', 'step', 'model', 'step']);
    expect(turns[0].dataset.browserStepKey).to.equal(
      'browser-step-playwright_mcp-call-2'
    );
    expect(turns[3].dataset.browserStepKey).to.equal(
      'browser-step-playwright_mcp-call-0'
    );
    expect(
      panel.scrollToBrowserStep('browser-step-playwright_mcp-call-2')
    ).to.equal(true);
  });

  it('viewer skips the fetch for evicted metadata, traps Tab and locks scroll', async () => {
    const images = [
      {
        key: 'a',
        artifactId: SHOT_GONE,
        availability: 'evicted',
        title: 'Step #1',
      },
      {
        key: 'b',
        artifactId: SHOT_A,
        availability: 'available',
        title: 'Step #2',
      },
    ];
    fetchStub.resetHistory();
    const viewer = await fixture<ArtifactImageViewer>(html`
      <artifact-image-viewer
        .sessionId=${SESSION_ID}
        .images=${images}
        .index=${0}
      ></artifact-image-viewer>
    `);
    await viewer.updateComplete;
    expect(
      viewer.shadowRoot!.querySelector('[data-testid="viewer-unavailable"]')
    ).to.exist;
    expect(
      fetchStub
        .getCalls()
        .filter((c) => String(c.args[0]).includes('/artifacts/'))
    ).to.have.length(0);
    expect(document.body.style.overflow).to.equal('hidden');

    // Tab cycles inside the dialog and never reaches the page behind it.
    const controls = () =>
      Array.from(
        viewer.shadowRoot!.querySelectorAll<HTMLElement>(
          'button:not([disabled]), a[href]'
        )
      );
    const first = controls()[0];
    const last = controls()[controls().length - 1];
    last.focus();
    window.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Tab', cancelable: true })
    );
    expect(viewer.shadowRoot!.activeElement).to.equal(first);
    window.dispatchEvent(
      new KeyboardEvent('keydown', {
        key: 'Tab',
        shiftKey: true,
        cancelable: true,
      })
    );
    expect(viewer.shadowRoot!.activeElement).to.equal(last);

    viewer.addEventListener('viewer-close', () => (viewer.index = -1));
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    await viewer.updateComplete;
    expect(document.body.style.overflow).to.equal('');
  });

  it('viewer releases under the session it acquired with when the session changes', async () => {
    const images = [
      {
        key: 'b',
        artifactId: SHOT_B,
        availability: 'available',
        title: 'Step #2',
      },
    ];
    const viewer = await fixture<ArtifactImageViewer>(html`
      <artifact-image-viewer
        .sessionId=${SESSION_ID}
        .images=${images}
        .index=${0}
      ></artifact-image-viewer>
    `);
    await waitUntil(() =>
      viewer.shadowRoot!.querySelector('[data-testid="viewer-image"]')
    );
    expect(heldSessionArtifactCount()).to.equal(1);
    viewer.sessionId = '22222222-2222-4222-8222-222222222222';
    await viewer.updateComplete;
    viewer.remove();
    expect(heldSessionArtifactCount()).to.equal(0);
  });

  it('viewer returns focus to the thumbnail button that opened it', async () => {
    const row = await fixture(html`
      <browser-step-row
        .item=${STEPS[2]}
        .sessionId=${SESSION_ID}
      ></browser-step-row>
    `);
    const thumb = row.shadowRoot!.querySelector(
      'browser-step-thumbnail'
    ) as BrowserStepThumbnail;
    await waitUntil(() => thumb.shadowRoot!.querySelector('button'));
    const trigger = thumb.shadowRoot!.querySelector(
      'button'
    ) as HTMLButtonElement;
    trigger.focus();
    const viewer = document.createElement('artifact-image-viewer');
    viewer.sessionId = SESSION_ID;
    viewer.images = [
      {
        key: 'c',
        artifactId: SHOT_B,
        availability: 'available',
        title: 'Step #2',
      },
    ];
    document.body.appendChild(viewer);
    viewer.index = 0;
    await viewer.updateComplete;
    viewer.addEventListener('viewer-close', () => (viewer.index = -1));
    viewer.close();
    expect(thumb.shadowRoot!.activeElement).to.equal(trigger);
    viewer.remove();
  });
});
