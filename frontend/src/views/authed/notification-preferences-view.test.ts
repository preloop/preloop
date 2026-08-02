import { html, fixture, expect } from '@open-wc/testing';
import sinon from 'sinon';
import './notification-preferences-view';
import { NotificationPreferencesView } from './notification-preferences-view';
import { invalidateApiCaches } from '../../api';

const tick = (ms = 150) => new Promise((r) => setTimeout(r, ms));

const PREFS = {
  id: 'pref-1',
  preferred_channel: 'email',
  enable_email: true,
  enable_mobile_push: false,
  stagger_email: true,
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
    // /me is cached module-wide; without this the admin and non-admin cases
    // would see whichever profile the previous test cached.
    invalidateApiCaches();
  });

  afterEach(() => {
    fetchStub?.restore();
    localStorage.clear();
    invalidateApiCaches();
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

  it('hides the quiet-alerts toggle unless both channels are enabled', async () => {
    fetchStub = stubPrefs(PREFS);
    const el = (await fixture(
      html`<notification-preferences-view></notification-preferences-view>`
    )) as NotificationPreferencesView;
    await tick();
    await el.updateComplete;
    expect(el.shadowRoot?.querySelector('[data-testid="stagger-email-toggle"]'))
      .to.not.exist;
  });

  it('shows the quiet-alerts toggle when email and push are enabled', async () => {
    fetchStub = stubPrefs({
      ...PREFS,
      enable_email: true,
      enable_mobile_push: true,
      stagger_email: true,
    });
    const el = (await fixture(
      html`<notification-preferences-view></notification-preferences-view>`
    )) as NotificationPreferencesView;
    await tick();
    await el.updateComplete;
    expect(el.shadowRoot?.textContent).to.contain('Quiet duplicate alerts');
    expect(el.shadowRoot?.querySelector('[data-testid="stagger-email-toggle"]'))
      .to.exist;
  });

  it('persists a quiet-alerts toggle change via PUT', async () => {
    fetchStub = stubPrefs({
      ...PREFS,
      enable_email: true,
      enable_mobile_push: true,
      stagger_email: true,
    });
    const el = (await fixture(
      html`<notification-preferences-view></notification-preferences-view>`
    )) as NotificationPreferencesView;
    await tick();
    await el.updateComplete;
    await (el as any).handleToggleStaggerEmail({ target: { checked: false } });
    await tick();
    const putCall = fetchStub
      .getCalls()
      .find((c) => (c.args[1]?.method || 'GET') === 'PUT');
    expect(putCall, 'a PUT request should be sent').to.exist;
    expect(String(putCall!.args[0])).to.contain(
      '/api/v1/notification-preferences/me'
    );
    const body = JSON.parse(String(putCall!.args[1]?.body || '{}'));
    expect(body.stagger_email).to.equal(false);
  });

  describe('admin test send', () => {
    /** Routes the profile call to an admin/non-admin user and the test-push
     * call to a canned provider result; everything else returns PREFS. */
    function stubAdminAndTestPush(opts: {
      isSuperuser: boolean;
      testPushBody?: unknown;
      testPushStatus?: number;
    }) {
      return sinon.stub(window, 'fetch').callsFake(async (input, init) => {
        const url = String(input);
        if (url.includes('/auth/users/me')) {
          return new Response(
            JSON.stringify({ id: 'u1', is_superuser: opts.isSuperuser }),
            { status: 200 }
          );
        }
        if (url.includes('/me/test-push')) {
          return new Response(JSON.stringify(opts.testPushBody ?? {}), {
            status: opts.testPushStatus ?? 200,
          });
        }
        return new Response(JSON.stringify(PREFS), { status: 200 });
      });
    }

    async function mount() {
      const el = (await fixture(
        html`<notification-preferences-view></notification-preferences-view>`
      )) as NotificationPreferencesView;
      await tick();
      await el.updateComplete;
      return el;
    }

    it('hides the test-send controls from non-admins', async () => {
      fetchStub = stubAdminAndTestPush({ isSuperuser: false });
      const el = await mount();
      expect((el as any).isAdmin).to.be.false;
      expect(el.shadowRoot?.querySelector('.test-send-card')).to.not.exist;
    });

    it('shows the test-send controls to admins', async () => {
      fetchStub = stubAdminAndTestPush({ isSuperuser: true });
      const el = await mount();
      expect((el as any).isAdmin).to.be.true;
      expect(el.shadowRoot?.querySelector('.test-send-card')).to.exist;
      expect(el.shadowRoot?.textContent).to.contain('Send test approval');
      expect(el.shadowRoot?.textContent).to.contain('Send test question');
    });

    it('posts the requested kind to the test-push endpoint', async () => {
      fetchStub = stubAdminAndTestPush({
        isSuperuser: true,
        testPushBody: {
          kind: 'question',
          request_id: 'r1',
          sent: 1,
          failed: 0,
          results: [],
        },
      });
      const el = await mount();
      await (el as any).handleSendTest('question');
      await tick();

      const call = fetchStub
        .getCalls()
        .find((c) => String(c.args[0]).includes('/me/test-push'));
      expect(call, 'a test-push request should be sent').to.exist;
      expect(call!.args[1]?.method).to.equal('POST');
      expect(JSON.parse(String(call!.args[1]?.body))).to.deep.equal({
        kind: 'question',
      });
    });

    it('surfaces the verbatim provider error for a failed device', async () => {
      const providerError =
        'The registration token is not a valid FCM registration token';
      fetchStub = stubAdminAndTestPush({
        isSuperuser: true,
        testPushBody: {
          kind: 'approval',
          request_id: 'r1',
          sent: 0,
          failed: 1,
          results: [
            {
              platform: 'android',
              token: 'cXqNs9Tg...ZZZZ',
              transport: 'fcm',
              success: false,
              error: providerError,
              error_reason: 'invalid_token',
              remediation: 'Check the Firebase project.',
              project_id: 'preloop-ai',
            },
          ],
        },
      });
      const el = await mount();
      await (el as any).handleSendTest('approval');
      await tick();
      await el.updateComplete;

      const text = el.shadowRoot?.textContent ?? '';
      expect(text).to.contain(providerError);
      expect(text).to.contain('invalid_token');
      expect(text).to.contain('preloop-ai');
      expect(el.shadowRoot?.querySelector('.test-result-item.failed')).to.exist;
    });

    it('renders a delivered result on success', async () => {
      fetchStub = stubAdminAndTestPush({
        isSuperuser: true,
        testPushBody: {
          kind: 'approval',
          request_id: 'r1',
          sent: 1,
          failed: 0,
          results: [
            {
              platform: 'android',
              token: 'cXqNs9Tg...ZZZZ',
              transport: 'fcm',
              success: true,
            },
          ],
        },
      });
      const el = await mount();
      await (el as any).handleSendTest('approval');
      await tick();
      await el.updateComplete;

      expect(el.shadowRoot?.querySelector('.test-result-item.ok')).to.exist;
      expect(el.shadowRoot?.textContent).to.contain('Delivered');
    });

    it('reports a failed test-send request without throwing', async () => {
      fetchStub = stubAdminAndTestPush({
        isSuperuser: true,
        testPushBody: { detail: 'Rate limit exceeded' },
        testPushStatus: 429,
      });
      const el = await mount();
      await (el as any).handleSendTest('approval');
      await tick();
      await el.updateComplete;

      expect((el as any).testError)
        .to.be.a('string')
        .and.not.equal('');
      expect((el as any).isSendingTest).to.equal(null);
    });
  });
});
