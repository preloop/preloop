import { expect, fixture, html } from '@open-wc/testing';
import sinon from 'sinon';

import './talk-button';
import type { TalkButton } from './talk-button';
import type { ManagedAgentSummary } from '../types';
import { resetTalkWindowsForTests } from '../utils/talk-window';

function agent(overrides: Partial<ManagedAgentSummary> = {}) {
  return {
    id: 'agent-1',
    display_name: 'Hermes',
    agent_kind: 'hermes',
    control_state: 'plugin_connected',
    control_enabled: true,
    control_online: true,
    control_capabilities: ['send_text_prompt'],
    ...overrides,
  } as ManagedAgentSummary;
}

describe('talk-button', () => {
  afterEach(() => {
    sinon.restore();
    resetTalkWindowsForTests();
  });

  it('opens the talk window for the agent', async () => {
    const openStub = sinon
      .stub(window, 'open')
      .returns({ closed: false, focus: () => {} } as unknown as Window);
    const el = await fixture<TalkButton>(
      html`<talk-button .agent=${agent()}></talk-button>`
    );
    await el.updateComplete;

    (el.querySelector('sl-button') as HTMLElement).click();

    expect(openStub.firstCall.args[0]).to.equal(
      '/console/agents/agent-1/talk?window=1'
    );
    expect(openStub.firstCall.args[1]).to.equal('preloop-talk-agent-1');
  });

  it('opens the window on the session being read', async () => {
    const openStub = sinon
      .stub(window, 'open')
      .returns({ closed: false, focus: () => {} } as unknown as Window);
    const el = await fixture<TalkButton>(
      html`<talk-button
        .agent=${agent()}
        .session=${{ id: 'sess-7' }}
      ></talk-button>`
    );
    await el.updateComplete;

    (el.querySelector('sl-button') as HTMLElement).click();
    expect(openStub.firstCall.args[0]).to.equal(
      '/console/agents/agent-1/talk?session=sess-7&window=1'
    );
  });

  it('is disabled, with the reason, when control is not connected', async () => {
    const el = await fixture<TalkButton>(
      html`<talk-button
        .agent=${agent({
          control_state: 'install_pending',
          control_enabled: false,
          control_online: false,
        })}
      ></talk-button>`
    );
    await el.updateComplete;
    expect(el.querySelector('sl-button')!.hasAttribute('disabled')).to.be.true;
    expect(el.querySelector('sl-tooltip')!.getAttribute('content')).to.contain(
      'Agent Control'
    );
  });

  it('renders nothing for an agent kind with no Agent Control', async () => {
    const el = await fixture<TalkButton>(
      html`<talk-button
        .agent=${agent({
          control_state: 'unsupported',
          control_enabled: false,
          control_online: false,
          control_capabilities: [],
        })}
      ></talk-button>`
    );
    await el.updateComplete;
    expect(el.querySelector('sl-button')).to.not.exist;
  });
});
