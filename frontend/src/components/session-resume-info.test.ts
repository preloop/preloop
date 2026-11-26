import { expect, fixture, html } from '@open-wc/testing';
import './session-resume-info';
import type { SessionResumeInfo } from './session-resume-info';

describe('session-resume-info', () => {
  it('copies the native identity and exact resume command', async () => {
    const session = {
      harness_session_id: 'native-example-session',
      start_directory: '/tmp/example project',
      resume_command:
        "cd '/tmp/example project' && claude --resume native-example-session",
    };
    const el = await fixture<SessionResumeInfo>(
      html`<session-resume-info .session=${session}></session-resume-info>`
    );
    const buttons = el.shadowRoot!.querySelectorAll('sl-copy-button');
    expect(buttons[0].value).to.equal(session.harness_session_id);
    expect(buttons[1].value).to.equal(session.resume_command);
    expect(el.shadowRoot!.textContent).to.contain(session.start_directory);
  });
  it('does not expose a pre-start placeholder', async () => {
    const el = await fixture<SessionResumeInfo>(
      html`<session-resume-info
        .session=${{ session_source_id: 'temporary-sidecar-id' }}
      ></session-resume-info>`
    );
    expect(el.shadowRoot!.querySelector('.identity')).to.equal(null);
  });
});
