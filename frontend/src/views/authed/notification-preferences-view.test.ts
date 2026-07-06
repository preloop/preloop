import { html, fixture, expect } from '@open-wc/testing';
import sinon from 'sinon';
import './notification-preferences-view';
import { NotificationPreferencesView } from './notification-preferences-view';

const tick = (ms = 150) => new Promise((r) => setTimeout(r, ms));

const PREFS = {
  id: 'pref-1',
  preferred_channel: 'email',
  enable_email: true,
  enable_mobile_push: false,
  mobile_device_tokens: [
    {
      platform: 'ios',
      token: 'abcdefghijklmnopqrstuvwxyz',
      registered_at: '2026-03-01T10:00:00Z',
    },
  ],
};

describe('NotificationPreferencesView', () => {
  let fetchStub: sinon.SinonStub;

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
  });

  afterEach(() => {
    fetchStub?.restore();
    localStorage.clear();
  });

  function stubPrefs(prefs: unknown, status = 200) {
    return sinon
      .stub(window, 'fetch')
      .callsFake(async () => new Response(JSON.stringify(prefs), { status }));
  }

  it('renders preferences and registered devices after loading', async () => {
    fetchStub = stubPrefs(PREFS);
    const el = (await fixture(
      html`<notification-preferences-view></notification-preferences-view>`
    )) as NotificationPreferencesView;
    await tick();
    await el.updateComplete;
    expect((el as any).isLoading).to.be.false;
    expect(el.shadowRoot?.textContent).to.contain('Notification Preferences');
    expect(el.shadowRoot?.querySelectorAll('sl-switch').length).to.equal(2);
    expect(el.shadowRoot?.querySelector('.device-item')).to.exist;
  });

  it('shows the empty state when no devices are registered', async () => {
    fetchStub = stubPrefs({ ...PREFS, mobile_device_tokens: [] });
    const el = (await fixture(
      html`<notification-preferences-view></notification-preferences-view>`
    )) as NotificationPreferencesView;
    await tick();
    await el.updateComplete;
    expect(el.shadowRoot?.textContent).to.contain(
      'No mobile devices registered'
    );
  });

  it('shows an error message when preferences fail to load', async () => {
    fetchStub = stubPrefs({ detail: 'boom' }, 500);
    const el = (await fixture(
      html`<notification-preferences-view></notification-preferences-view>`
    )) as NotificationPreferencesView;
    await tick();
    await el.updateComplete;
    expect((el as any).errorMessage)
      .to.be.a('string')
      .and.not.equal('');
    expect(el.shadowRoot?.querySelector('sl-alert[variant="danger"]')).to.exist;
  });

  it('persists an email-toggle change via the preferences endpoint', async () => {
    fetchStub = stubPrefs(PREFS);
    const el = (await fixture(
      html`<notification-preferences-view></notification-preferences-view>`
    )) as NotificationPreferencesView;
    await tick();
    await el.updateComplete;
    await (el as any).handleToggleEmail({ target: { checked: false } });
    await tick();
    const putCall = fetchStub
      .getCalls()
      .find((c) => (c.args[1]?.method || 'GET') === 'PUT');
    expect(putCall, 'a PUT request should be sent').to.exist;
    expect(String(putCall!.args[0])).to.contain(
      '/api/v1/notification-preferences/me'
    );
  });
});
