import { expect, fixture, html } from '@open-wc/testing';
import { setViewport } from '@web/test-runner-commands';
import './talk-start-picker';
import './session-chat-view';
import type { TalkStartPicker } from './talk-start-picker';

describe('talk-start-picker', () => {
  it('shows projects before recent directories and a typed path at phone width', async () => {
    await setViewport({ width: 390, height: 844 });
    const el = await fixture<TalkStartPicker>(
      html`<talk-start-picker
        .options=${{ projects: [{ name: 'Example', path: '/work/example' }], recent_directories: ['/work/recent'], capabilities: { start_directory: true, command_session: true } }}
      ></talk-start-picker>`
    );
    const buttons = [...el.shadowRoot!.querySelectorAll('button')];
    expect(buttons.map((button) => button.textContent?.trim())).to.deep.equal([
      'Example /work/example',
      '/work/recent',
    ]);
    expect(
      el.shadowRoot!.querySelector('input')!.getBoundingClientRect().right
    ).to.be.at.most(390);
    let selected = '';
    el.addEventListener('directory-selected', (event) => {
      selected = (event as CustomEvent).detail;
    });
    buttons[0].click();
    expect(selected).to.equal('/work/example');
  });

  it('explains missing capabilities with an update command before sending', async () => {
    const el = await fixture<TalkStartPicker>(
      html`<talk-start-picker
        .options=${{ capabilities: {}, projects: [], recent_directories: [] }}
      ></talk-start-picker>`
    );
    expect(el.shadowRoot!.textContent).to.contain(
      'Update the connected sidecar'
    );
    expect(el.shadowRoot!.textContent).to.contain('preloop update');
  });
});

describe('queued Talk prompt on a phone', () => {
  it('keeps the prompt and truthful queue state visible at 390 px', async () => {
    await setViewport({ width: 390, height: 844 });
    const el = await fixture(
      html`<session-chat-view
        .liveEnabled=${false}
        .pending=${[{ id: 'queued-example', text: 'Example follow-up', at: new Date().toISOString(), state: 'sent', deliveryMessage: 'Queued, runs when this step finishes' }]}
      ></session-chat-view>`
    );
    expect(el.shadowRoot!.textContent).to.contain('Example follow-up');
    expect(el.shadowRoot!.textContent).to.contain(
      'Queued, runs when this step finishes'
    );
    const bubble = el.shadowRoot!.querySelector('.pending')!;
    expect(bubble.getBoundingClientRect().right).to.be.at.most(390);
  });
});
