import {
  expect,
  fixture,
  fixtureCleanup,
  html,
  waitUntil,
} from '@open-wc/testing';
import sinon from 'sinon';
import './session-recording-player';
import './session-chat-view';
import './browser-step-strip';
import type { SessionRecordingPlayer } from './session-recording-player';
import type { SessionChatView } from './session-chat-view';
import type { BrowserStepStrip } from './browser-step-strip';
import type { RuntimeSessionArtifactDescriptor, RecordingSync } from '../types';
import { adjacentMediaStep, currentMediaStep } from '../utils/media-sync';
import { resetSessionArtifactCache } from '../utils/session-artifacts';

const steps = [0, 2000, 4000, 6000, 8000].map((offset_ms, index) => ({
  activity_id: `step-${index}`,
  activity_type: 'browser_step',
  offset_ms,
  rule: 'exact' as const,
  approximate: false,
}));
const sync: RecordingSync = {
  artifact_id: 'video',
  mappings: steps,
  skew_ms: 30000,
  unmapped_activity_ids: ['outside'],
};
function artifact(id = 'video'): RuntimeSessionArtifactDescriptor {
  return {
    id,
    runtime_session_id: 'session',
    kind: 'recording',
    name: `${id}.webm`,
    content_type: 'video/webm',
    size_bytes: 1000,
    sha256: '0'.repeat(64),
    labels: {},
    availability: 'available',
    legal_hold: false,
    created_at: '2026-10-01T00:00:00Z',
    media_timing: {
      started_at: '2026-10-01T00:00:00Z',
      duration_ms: 10000,
      clock: 'agent',
    },
    content_block: {
      type: 'resource_link',
      uri: `/api/v1/runtime-sessions/session/artifacts/${id}`,
      name: `${id}.webm`,
      mimeType: 'video/webm',
      size: 1000,
    },
  };
}

describe('synchronized recording player', () => {
  let media: Blob;
  let fetchStub: sinon.SinonStub;
  before(async () => {
    media = await (
      await fetch('/src/components/fixtures/recording-10s.webm')
    ).blob();
  });
  beforeEach(() => {
    localStorage.setItem('accessToken', 'fixture-token');
    fetchStub = sinon
      .stub(window, 'fetch')
      .callsFake(
        async () =>
          new Response(media, { headers: { 'Content-Type': 'video/webm' } })
      );
  });
  afterEach(() => {
    fixtureCleanup();
    sinon.restore();
    resetSessionArtifactCache();
    localStorage.removeItem('accessToken');
  });
  async function player(
    recordings = [artifact()],
    mappings = [sync]
  ): Promise<SessionRecordingPlayer> {
    return fixture(
      html`<session-recording-player
        .sessionId=${'session'}
        .recordings=${recordings}
        .sync=${mappings}
      ></session-recording-player>`
    );
  }
  it('resolves current and adjacent steps without snapping before the first', () => {
    expect(currentMediaStep(steps, -1)).to.equal(null);
    expect(currentMediaStep(steps, 6100)?.activity_id).to.equal('step-3');
    expect(adjacentMediaStep(steps, 4000, 1)?.offset_ms).to.equal(6000);
    expect(adjacentMediaStep(steps, 4000, -1)?.offset_ms).to.equal(2000);
  });
  it('clicking a timeline step seeks the real 10 s WebM; playback highlights in order', async () => {
    const el = await player();
    const activity = steps.map((item, index) => ({
      activity_id: item.activity_id,
      activity_type: 'browser_step',
      timestamp: `2026-10-01T00:00:0${index * 2}Z`,
      title: 'click',
      metadata: {
        action: 'click',
        source_step_id: item.activity_id,
        step_index: index,
      },
    }));
    const chat = await fixture<SessionChatView>(
      html`<session-chat-view
        .liveEnabled=${false}
        .sessionId=${'session'}
        .activity=${activity}
        .recordingSync=${sync}
        .recordingEnabled=${true}
      ></session-chat-view>`
    );
    const strip = await fixture<BrowserStepStrip>(
      html`<browser-step-strip
        .sessionId=${'session'}
        .steps=${activity}
      ></browser-step-strip>`
    );
    chat.addEventListener(
      'recording-seek',
      (event) => void el.seekActivity((event as CustomEvent).detail.activityId)
    );
    const cues =
      chat.shadowRoot!.querySelectorAll<HTMLButtonElement>('.recording-cue');
    expect(cues.length).to.equal(5);
    cues[3].click();
    await waitUntil(
      () => (el.shadowRoot?.querySelector('video')?.currentTime ?? 0) >= 5.5
    );
    const video = el.shadowRoot!.querySelector('video')!;
    expect(video.currentTime).to.be.closeTo(6, 0.5);
    expect(video.duration).to.be.closeTo(10, 0.1);
    const highlighted: string[] = [];
    el.addEventListener('recording-playhead', (event) => {
      const id = (event as CustomEvent).detail.activityId;
      if (id && highlighted.at(-1) !== id) highlighted.push(id);
      chat.recordingActivityId = id;
      strip.currentActivityId = id;
    });
    await el.seekActivity('step-0');
    video.playbackRate = 4;
    await video.play();
    await waitUntil(() => video.ended, 'fixture should finish', {
      timeout: 10000,
    });
    await chat.updateComplete;
    await strip.updateComplete;
    expect(highlighted).to.deep.equal(steps.map((item) => item.activity_id));
    expect(
      chat
        .shadowRoot!.querySelector('.recording-current')
        ?.getAttribute('data-recording-activity')
    ).to.equal('step-4');
    expect(
      strip
        .shadowRoot!.querySelector('[aria-current="step"]')
        ?.getAttribute('data-step-key')
    ).to.contain('step-4');
  });
  it('maps model turns and native tools by identity, without timestamp guesses', async () => {
    const mapping: RecordingSync = {
      ...sync,
      mappings: [
        {
          activity_id: 'model-row',
          activity_type: 'model_gateway_call',
          api_usage_id: 'usage-1',
          offset_ms: 1000,
          rule: 'server_skew',
          approximate: true,
        },
        {
          activity_id: 'tool-row',
          activity_type: 'tool_call',
          offset_ms: 2000,
          rule: 'server_skew',
          approximate: true,
        },
      ],
    };
    const chat = await fixture<SessionChatView>(
      html`<session-chat-view
        .liveEnabled=${false}
        .events=${[{ id: 'usage-1', type: 'model_gateway_request', timestamp: '2026-10-01T00:00:01Z', payload: { model_alias: 'example-model', conversation_preview: { messages: [{ role: 'assistant', text: 'Opening the page.', source: 'response' }] } } }]}
        .activity=${[
          {
            activity_id: 'tool-row',
            activity_type: 'tool_call',
            tool_name: 'read_file',
            timestamp: '2026-10-01T00:00:02Z',
            title: 'read_file',
            summary: '{}',
            status: 'success',
            metadata: {},
          },
          {
            activity_id: 'outside',
            activity_type: 'tool_call',
            tool_name: 'other_tool',
            timestamp: '2026-10-01T00:00:02Z',
            title: 'other_tool',
            summary: '{}',
            status: 'success',
            metadata: {},
          },
        ]}
        .recordingSync=${mapping}
        .recordingEnabled=${true}
      ></session-chat-view>`
    );
    const seeks: string[] = [];
    chat.addEventListener('recording-seek', (event) =>
      seeks.push((event as CustomEvent).detail.activityId)
    );
    chat
      .shadowRoot!.querySelectorAll<HTMLButtonElement>('.recording-cue')
      .forEach((button) => button.click());
    expect(seeks).to.deep.equal(['model-row', 'tool-row']);
    expect(chat.shadowRoot!.textContent).to.contain('Not in this recording');
  });

  it('rejects an offset beyond decoded duration when metadata omitted duration', async () => {
    const recording = artifact();
    recording.media_timing!.duration_ms = null;
    const el = await player(
      [recording],
      [{ ...sync, mappings: [{ ...steps[0], offset_ms: 15000 }] }]
    );
    await el.seekActivity('step-0');
    await waitUntil(() =>
      el.shadowRoot!.textContent!.includes('No recording for this moment')
    );
    expect(el.shadowRoot!.querySelector('video')!.currentTime).to.equal(0);
  });

  it('keeps two mappings independent and leaves gaps unseeked', async () => {
    const second = {
      ...sync,
      artifact_id: 'second',
      mappings: [{ ...steps[3], offset_ms: 1000 }],
    };
    const el = await player([artifact(), artifact('second')], [sync, second]);
    const selector = el.shadowRoot!.querySelector('select')!;
    selector.value = 'second';
    selector.dispatchEvent(new Event('change'));
    await el.updateComplete;
    await el.seekActivity('step-3');
    await waitUntil(
      () => (el.shadowRoot!.querySelector('video')?.currentTime ?? 0) >= 0.9
    );
    expect(el.shadowRoot!.querySelector('video')!.currentTime).to.be.closeTo(
      1,
      0.1
    );
    await el.seekActivity('outside');
    expect(el.shadowRoot!.textContent).to.contain(
      'No recording for this moment'
    );
    expect(el.shadowRoot!.querySelector('video')!.currentTime).to.be.closeTo(
      1,
      0.1
    );
  });
  it('plain legacy/GIF players have no seek affordances and unavailable bytes are never fetched', async () => {
    const legacy = artifact();
    legacy.media_timing = null;
    const el = await player([legacy]);
    expect(el.shadowRoot!.textContent).to.contain(
      'This recording has no timing data'
    );
    expect(el.seekable).to.equal(false);
    el.recordings = [
      { ...artifact(), content_type: 'image/gif', kind: 'screencast' },
    ];
    await el.updateComplete;
    expect(el.shadowRoot!.textContent).to.contain(
      'Seek not supported for GIF in this browser'
    );
    expect(el.seekable).to.equal(false);
    el.recordings = [{ ...artifact(), availability: 'evicted' }];
    await el.updateComplete;
    await el.seekActivity('step-0');
    expect(fetchStub.called).to.equal(false);
    expect(el.shadowRoot!.textContent?.toLowerCase()).to.contain('evicted');
  });
  it('shows producer skew and supports keyboard navigation except inside inputs', async () => {
    const el = await player();
    expect(
      el.shadowRoot!.querySelector('[title]')?.getAttribute('title')
    ).to.equal('producer clock offset 30.0 s');
    el.handleShortcut(new KeyboardEvent('keydown', { key: ']' }));
    await waitUntil(
      () => (el.shadowRoot!.querySelector('video')?.currentTime ?? 0) >= 1.9
    );
    expect(el.shadowRoot!.querySelector('video')!.currentTime).to.be.closeTo(
      2,
      0.1
    );
    const input = document.createElement('input');
    el.append(input);
    input.addEventListener('keydown', (event) => el.handleShortcut(event));
    input.dispatchEvent(
      new KeyboardEvent('keydown', { key: ']', bubbles: true })
    );
    expect(el.shadowRoot!.querySelector('video')!.currentTime).to.be.closeTo(
      2,
      0.1
    );
  });
});

// The shared observer must wire the player without changing transcript or Talk routing.
import './preloop-session-observer';
import type { PreloopSessionObserver } from './preloop-session-observer';
import { unifiedWebSocketManager } from '../services/unified-websocket-manager';

describe('recording in the session observer', () => {
  afterEach(() => {
    fixtureCleanup();
    sinon.restore();
    localStorage.removeItem('accessToken');
    resetSessionArtifactCache();
  });
  it('loads mapping, respects the rollout flag, suppresses user scroll, and removes evicted recording cues', async () => {
    localStorage.setItem('accessToken', 'fixture-token');
    sinon.stub(unifiedWebSocketManager, 'connect');
    sinon.stub(unifiedWebSocketManager, 'subscribe').returns(() => undefined);
    const row = {
      activity_id: 'step-0',
      activity_type: 'browser_step',
      timestamp: '2026-10-01T00:00:00Z',
      title: 'click',
      metadata: { action: 'click', source_step_id: 'step-0', step_index: 0 },
    };
    const json = (body: unknown) =>
      new Response(JSON.stringify(body), {
        headers: { 'Content-Type': 'application/json' },
      });
    const request = sinon.stub(window, 'fetch').callsFake(async (input) => {
      const url = String(input);
      if (url.includes('/artifacts/'))
        return new Response(JSON.stringify({ availability: 'evicted' }), {
          status: 410,
          headers: { 'Content-Type': 'application/json' },
        });
      if (url.includes('/media-sync')) return json({ recordings: [sync] });
      if (url.includes('/artifacts?'))
        return json({ items: [artifact()], next_cursor: null });
      if (url.includes('/activity')) return json({ items: [row] });
      if (url.includes('/gateway-events'))
        return json({ logs: [], pagination: { has_more: false } });
      if (url.includes('/approval')) return json([]);
      return json({});
    });
    const el = await fixture<PreloopSessionObserver>(
      html`<preloop-session-observer
        .sessions=${[{ id: 'session', session_source_type: 'custom', started_at: '2026-10-01T00:00:00Z', last_activity_at: '2026-10-01T00:00:08Z', total_requests: 0 }]}
        defaultReplayMode="conversation"
      ></preloop-session-observer>`
    );
    await waitUntil(
      () =>
        el.shadowRoot!.querySelector('session-recording-player')?.mapping
          ?.mappings.length === 5
    );
    const player = el.shadowRoot!.querySelector('session-recording-player')!;
    const chat = el.shadowRoot!.querySelector('session-chat-view')!;
    await waitUntil(
      () =>
        !!chat.shadowRoot!.querySelector('[data-recording-activity="step-0"]')
    );
    const target = chat.shadowRoot!.querySelector(
      '[data-recording-activity="step-0"]'
    )!;
    const scroll = sinon.stub(target, 'scrollIntoView');
    player.dispatchEvent(
      new CustomEvent('recording-playhead', {
        detail: { activityId: 'step-0' },
        bubbles: true,
        composed: true,
      })
    );
    expect(scroll.callCount).to.equal(1);
    chat.dispatchEvent(
      new WheelEvent('wheel', { bubbles: true, composed: true })
    );
    player.dispatchEvent(
      new CustomEvent('recording-playhead', {
        detail: { activityId: 'step-0' },
        bubbles: true,
        composed: true,
      })
    );
    expect(scroll.callCount).to.equal(1);
    const now = Date.now();
    sinon.stub(Date, 'now').returns(now + 5001);
    player.dispatchEvent(
      new CustomEvent('recording-playhead', {
        detail: { activityId: 'step-0' },
        bubbles: true,
        composed: true,
      })
    );
    expect(scroll.callCount).to.equal(2);
    expect(
      request
        .getCalls()
        .some((call) => String(call.args[0]).includes('/media-sync'))
    ).to.equal(true);
    expect(chat.shadowRoot!.querySelector('.recording-cue')).not.to.equal(null);
    await player.seekActivity('step-0');
    expect(
      request
        .getCalls()
        .some((call) => String(call.args[0]).includes('/artifacts/'))
    ).to.equal(true);
    await el.updateComplete;
    await chat.updateComplete;
    expect(player.shadowRoot!.textContent?.toLowerCase()).to.contain('evicted');
    expect(chat.shadowRoot!.querySelector('.recording-cue')).to.equal(null);
    expect(chat.shadowRoot!.querySelector('.recording-current')).to.equal(null);
    el.features = { syncedRecordingPlayer: false };
    await el.updateComplete;
    await chat.updateComplete;
    expect(el.shadowRoot!.querySelector('session-recording-player')).to.equal(
      null
    );
    expect(chat.shadowRoot!.querySelector('.recording-cue')).to.equal(null);
  });
});
