import { expect, fixture, html } from '@open-wc/testing';
import './global-notice';
import { GLOBAL_NOTICE_MESSAGES } from './global-notice';

describe('global-notice', () => {
  const original = window.location.pathname + window.location.search;
  afterEach(() => window.history.replaceState({}, '', original));

  it('ignores free text in ?message= and leaves the URL alone', async () => {
    window.history.replaceState(
      {},
      '',
      '/console/agents?kinds=codex&message=Your_account_is_suspended_call_555'
    );
    const el = await fixture(html`<global-notice></global-notice>`);
    expect(el.shadowRoot!.querySelector('sl-alert')).to.not.exist;
    expect(window.location.search).to.contain('kinds=codex');
  });

  it('shows a known key as its fixed copy and keeps the other params', async () => {
    const key = 'test_notice';
    const text = 'A fixed notice.';
    GLOBAL_NOTICE_MESSAGES[key] = text;
    try {
      window.history.replaceState(
        {},
        '',
        `/console/agents?kinds=codex&message=${key}`
      );
      const el = await fixture(html`<global-notice></global-notice>`);
      expect(el.shadowRoot!.querySelector('sl-alert')!.textContent).to.contain(
        text
      );
      expect(window.location.search).to.equal('?kinds=codex');
    } finally {
      delete GLOBAL_NOTICE_MESSAGES[key];
    }
  });
});
