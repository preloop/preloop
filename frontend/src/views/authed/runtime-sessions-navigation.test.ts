// Capture the test runner session before the router changes the URL.
import { setViewport } from '@web/test-runner-commands';
import { expect, fixture, html, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';
import { Router } from '../../router';
import { mockApi, type MockApi } from '../../test-helpers/capability-api';
import { unifiedWebSocketManager } from '../../services/unified-websocket-manager';
import { runtimeSessionPathRedirect } from '../../utils/runtime-session-route';
import './runtime-sessions-view';
import type { RuntimeSessionsView } from './runtime-sessions-view';

describe('Session route navigation', () => {
  let api: MockApi;
  let router: Router;
  let connect: sinon.SinonStub;
  let outlet: HTMLElement;
  const sessions = [
    {
      id: 'child-session',
      title: 'Review the changes',
      parent_session_id: 'parent-session',
    },
    {
      id: 'parent-session',
      title: 'Prepare the release',
      parent_session_id: null,
    },
  ].map((session) => ({
    ...session,
    session_source_type: 'claude_code',
    session_reference: session.id,
    started_at: '2026-11-01T12:00:00Z',
    last_activity_at: '2026-11-01T12:05:00Z',
    activity_status: 'ended',
    total_requests: 0,
    token_usage: { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 },
  }));

  beforeEach(async () => {
    await setViewport({ width: 1280, height: 900 });
    localStorage.setItem('accessToken', 'test-token');
    connect = sinon.stub(unifiedWebSocketManager, 'connect').resolves();
    api = mockApi({
      routes: [
        { path: '/api/v1/approval-requests', body: [] },
        {
          path: '/api/v1/runtime-sessions',
          body: { items: sessions, total: 2 },
        },
        ...sessions.map((session) => ({
          path: `/api/v1/runtime-sessions/${session.id}`,
          body: { session, usage_by_model: [] },
        })),
      ],
      fallback: { status: 200, body: { items: [], total: 0 } },
    });
    outlet = await fixture<HTMLElement>(html`<div></div>`);
    router = new Router(outlet);
    await router.setRoutes(
      [
        {
          path: '/console/runtime-sessions',
          component: 'runtime-sessions-view',
        },
        {
          path: '/console/runtime-sessions/:sessionId',
          action: (context, commands) =>
            commands.redirect(
              runtimeSessionPathRedirect(
                context.params.sessionId,
                context.search,
                context.hash
              )
            ),
        },
      ],
      true
    );
  });

  afterEach(() => {
    router.unsubscribe();
    outlet.replaceChildren();
    api.restore();
    connect.restore();
    localStorage.removeItem('accessToken');
    history.replaceState({}, '', '/');
  });

  async function expectSession(title: string): Promise<void> {
    await waitUntil(() => {
      const view = outlet.querySelector('runtime-sessions-view');
      const observer = view?.shadowRoot?.querySelector(
        'preloop-session-observer'
      );
      return (
        observer?.shadowRoot
          ?.querySelector('#session-title')
          ?.textContent?.trim() === title
      );
    }, `Session heading did not change to ${title}`);
  }

  it('follows the Parent link on the reused view and restores Back and Forward', async () => {
    await router.render('/console/runtime-sessions?sessionId=child-session', {
      history: 'replace',
    });
    await expectSession('Review the changes');
    const view = outlet.firstElementChild as RuntimeSessionsView;
    const observer = view.shadowRoot!.querySelector(
      'preloop-session-observer'
    )!;
    observer
      .shadowRoot!.querySelector<HTMLAnchorElement>('[data-parent-session]')!
      .click();
    await waitUntil(() => location.search === '?sessionId=parent-session');
    await expectSession('Prepare the release');
    expect(outlet.firstElementChild).to.equal(view);
    await waitUntil(
      () =>
        api.callsTo('/api/v1/runtime-sessions/parent-session/gateway-events')
          .length > 0
    );
    history.back();
    await expectSession('Review the changes');
    history.forward();
    await expectSession('Prepare the release');
  });

  it('opens a session outside the list through the path redirect', async () => {
    api.restore();
    api = mockApi({
      routes: [
        { path: '/api/v1/approval-requests', body: [] },
        {
          path: '/api/v1/runtime-sessions',
          body: { items: [sessions[0]], total: 1 },
        },
        ...sessions.map((session) => ({
          path: `/api/v1/runtime-sessions/${session.id}`,
          body: { session, usage_by_model: [] },
        })),
      ],
      fallback: { status: 200, body: { items: [], total: 0 } },
    });
    await router.render('/console/runtime-sessions?sessionId=child-session', {
      history: 'replace',
    });
    await expectSession('Review the changes');
    const view = outlet.firstElementChild;
    await router.render('/console/runtime-sessions/parent-session', {
      history: 'push',
    });
    await expectSession('Prepare the release');
    expect(outlet.firstElementChild).to.equal(view);
    expect(location.search).to.equal('?sessionId=parent-session');
  });
});
