import { expect, fixture, html } from '@open-wc/testing';
import sinon from 'sinon';

import './talking-indicator';
import type { TalkingIndicator } from './talking-indicator';
import { TALK_STALE_MS } from '../utils/talk-channel';
import { resetTalkWindowsForTests } from '../utils/talk-window';

describe('talking-indicator', () => {
  afterEach(() => {
    sinon.restore();
    resetTalkWindowsForTests();
  });

  async function mount(): Promise<TalkingIndicator> {
    return fixture<TalkingIndicator>(
      html`<talking-indicator></talking-indicator>`
    );
  }

  it('shows nothing until a talk window announces itself', async () => {
    const el = await mount();
    expect(el.shadowRoot!.querySelector('[data-testid="talking-chip"]')).to.not
      .exist;
  });

  it('adds a chip per open window and an unread dot on a new message', async () => {
    const el = await mount();
    el.receive({
      type: 'open',
      agentId: 'a1',
      agentName: 'Hermes',
      sessionId: 's1',
      at: Date.now(),
    });
    await el.updateComplete;
    const chip = el.shadowRoot!.querySelector('[data-testid="talking-chip"]');
    expect(chip).to.exist;
    expect(chip!.textContent).to.contain('Hermes');
    expect(chip!.querySelector('[data-testid="talking-unread"]')).to.not.exist;

    el.receive({
      type: 'message',
      agentId: 'a1',
      agentName: 'Hermes',
      sessionId: 's1',
      at: Date.now(),
    });
    await el.updateComplete;
    expect(
      el
        .shadowRoot!.querySelector('[data-testid="talking-chip"]')!
        .querySelector('[data-testid="talking-unread"]')
    ).to.exist;
  });

  it('focuses the window and clears the dot when the chip is clicked', async () => {
    const openStub = sinon
      .stub(window, 'open')
      .returns({ closed: false, focus: () => {} } as unknown as Window);
    const el = await mount();
    el.receive({
      type: 'message',
      agentId: 'a1',
      agentName: 'Hermes',
      sessionId: 's1',
      at: Date.now(),
    });
    await el.updateComplete;

    (
      el.shadowRoot!.querySelector(
        '[data-testid="talking-chip"]'
      ) as HTMLElement
    ).click();
    await el.updateComplete;

    expect(openStub.firstCall.args[0]).to.equal(
      '/console/agents/a1/talk?session=s1&window=1'
    );
    expect(
      el
        .shadowRoot!.querySelector('[data-testid="talking-chip"]')!
        .querySelector('[data-testid="talking-unread"]')
    ).to.not.exist;
  });

  it('prunes a window that stopped sending heartbeats', async () => {
    const el = await mount();
    el.receive({
      type: 'open',
      agentId: 'a1',
      agentName: 'Hermes',
      sessionId: null,
      at: 1_000,
    });
    await el.updateComplete;
    expect(el.shadowRoot!.querySelector('[data-testid="talking-chip"]')).to
      .exist;

    el.pruneNow(1_001 + TALK_STALE_MS);
    await el.updateComplete;
    expect(el.shadowRoot!.querySelector('[data-testid="talking-chip"]')).to.not
      .exist;
  });

  it('removes the chip when the window closes', async () => {
    const el = await mount();
    el.receive({
      type: 'open',
      agentId: 'a1',
      agentName: 'Hermes',
      sessionId: null,
      at: Date.now(),
    });
    await el.updateComplete;
    el.receive({
      type: 'close',
      agentId: 'a1',
      sessionId: null,
      at: Date.now(),
    });
    await el.updateComplete;
    expect(el.shadowRoot!.querySelector('[data-testid="talking-chip"]')).to.not
      .exist;
  });
});
