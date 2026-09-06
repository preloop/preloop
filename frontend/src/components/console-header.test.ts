/**
 * Accessibility contract for the console header's user menu trigger.
 *
 * The avatar replaced an <sl-icon-button>, so the trigger has to keep the
 * button semantics it used to get for free: a real focusable control with an
 * accessible name that opens the dropdown on activation. These tests pin that
 * contract so a future restyle cannot silently drop keyboard access.
 */
import { expect, fixture, html, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';
import {
  ConnectionState,
  unifiedWebSocketManager,
} from '../services/unified-websocket-manager';
import './console-header.ts';
import type { ConsoleHeader } from './console-header.ts';
import { publishAttentionSummary } from '../utils/attention-summary';
import { Router } from '@vaadin/router';

const USER = {
  id: 'user-1',
  username: 'alice',
  email: 'alice@example.com',
  full_name: 'Alice Smith',
  avatar_url: null,
};

/** Answer every console-header startup request so rendering is deterministic. */
function stubFetch(approvals: () => unknown[] = () => []): () => void {
  const original = window.fetch;
  window.fetch = (async (input: RequestInfo | URL) => {
    const url = typeof input === 'string' ? input : input.toString();
    const body = url.includes('/users/me')
      ? USER
      : url.includes('/approval-requests')
        ? approvals()
        : [];
    return new Response(JSON.stringify(body), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  }) as typeof window.fetch;
  return () => {
    window.fetch = original;
  };
}

describe('console-header user menu trigger', () => {
  let restoreFetch: () => void;
  let el: ConsoleHeader;

  beforeEach(async () => {
    // fetchWithAuth short-circuits to /login without a token, so the header
    // would never reach the stubbed responses.
    localStorage.setItem('accessToken', 'test-token');
    restoreFetch = stubFetch();
    el = await fixture<ConsoleHeader>(html`<console-header></console-header>`);
    await el.updateComplete;
  });

  afterEach(() => {
    restoreFetch();
    localStorage.removeItem('accessToken');
  });

  function trigger(): HTMLButtonElement {
    const found = el.shadowRoot!.querySelector<HTMLButtonElement>(
      'button.user-menu-trigger'
    );
    expect(found, 'user menu trigger button').to.exist;
    return found!;
  }

  it('is a real button, not a bare avatar', () => {
    expect(trigger().type).to.equal('button');
  });

  it('has an accessible name', () => {
    expect(trigger().getAttribute('aria-label')).to.equal('User Menu');
  });

  it('wraps the avatar', () => {
    expect(trigger().querySelector('user-avatar')).to.exist;
  });

  it('is keyboard focusable', () => {
    const button = trigger();
    button.focus();
    expect(el.shadowRoot!.activeElement).to.equal(button);
  });

  it('opens the dropdown when activated', async () => {
    const button = trigger();
    const dropdown = button.closest('sl-dropdown') as HTMLElement & {
      open: boolean;
    };
    expect(dropdown.open).to.be.false;

    button.click();

    await waitUntil(() => dropdown.open, 'dropdown did not open');
  });

  it('shows the signed-in user in the menu', async () => {
    await waitUntil(
      () => el.shadowRoot!.textContent?.includes('Alice Smith') ?? false,
      'user name never rendered'
    );
  });
});

/**
 * The bell's empty state. "No notifications" over an amber strip saying
 * "2 need attention" is two true sentences that read as a contradiction, so
 * the empty state names what is empty and repeats the attention counts the
 * Overview or the Attention page published.
 */
describe('console-header bell empty state', () => {
  let restoreFetch: () => void;

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-token');
    restoreFetch = stubFetch();
    sessionStorage.removeItem('preloop:attention-summary');
  });

  afterEach(() => {
    restoreFetch();
    localStorage.removeItem('accessToken');
    sessionStorage.removeItem('preloop:attention-summary');
  });

  function dropdownText(el: ConsoleHeader): string {
    return (
      el
        .shadowRoot!.querySelector('.notification-dropdown')!
        .textContent?.replace(/\s+/g, ' ')
        .trim() || ''
    );
  }

  it('says what is empty when nothing needs attention', async () => {
    const el = await fixture<ConsoleHeader>(
      html`<console-header></console-header>`
    );
    await el.updateComplete;

    const text = dropdownText(el);
    expect(text).to.contain('No new notifications');
    expect(text).to.not.contain('need attention:');
  });

  it('states the attention counts published by the Overview', async () => {
    publishAttentionSummary([
      {
        id: 'flow:flow-1',
        kind: 'flow',
        severity: 'critical',
        title: 'Pull Request Reviewer',
        detail: '11 failed runs',
        href: '/console/flows',
        at: null,
        fingerprint: 'flow-1:11',
        dismissable: true,
      },
      {
        id: 'pricing:model-1',
        kind: 'pricing',
        severity: 'warning',
        title: 'No price catalog loaded',
        detail: 'Estimated spend is $0',
        href: '/console/cost',
        at: null,
        fingerprint: 'pricing:1',
        dismissable: true,
      },
    ]);

    const el = await fixture<ConsoleHeader>(
      html`<console-header></console-header>`
    );
    await el.updateComplete;

    expect(dropdownText(el)).to.contain(
      '2 items need attention: 1 flow, 1 pricing'
    );
  });

  it('follows a summary published while the header is on screen', async () => {
    const el = await fixture<ConsoleHeader>(
      html`<console-header></console-header>`
    );
    await el.updateComplete;

    publishAttentionSummary([
      {
        id: 'approval:approval-1',
        kind: 'approval',
        severity: 'critical',
        title: 'read_file',
        detail: 'waiting on you',
        href: '/console/approvals',
        at: null,
        fingerprint: 'approval-1',
        dismissable: false,
      },
    ]);
    await el.updateComplete;

    expect(dropdownText(el)).to.contain('1 item needs attention: 1 approval');
  });
});

describe('console-header bell approvals', () => {
  const APPROVAL = {
    id: 'ar-1',
    tool_name: 'write_file',
    tool_args: {},
    status: 'pending',
    requested_at: new Date().toISOString(),
    expires_at: new Date(Date.now() + 600_000).toISOString(),
    execution_id: 'exec-1',
  };

  let restoreFetch: () => void;
  let restoreSubscribe: () => void;
  let approvalListeners: ((message: any) => void)[];
  let approvals: any[];
  let routes: string[];
  let restoreRouterGo: () => void;

  /** Answer startup requests, including the pending approvals load. */
  function stubApprovalFetch(): () => void {
    const original = window.fetch;
    window.fetch = (async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input.toString();
      let body: unknown = [];
      if (url.includes('/users/me')) body = USER;
      else if (url.includes('/approval-requests')) body = approvals;
      return new Response(JSON.stringify(body), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    }) as typeof window.fetch;
    return () => {
      window.fetch = original;
    };
  }

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-token');
    approvals = [APPROVAL];
    approvalListeners = [];
    routes = [];
    restoreFetch = stubApprovalFetch();

    const originalSubscribe = unifiedWebSocketManager.subscribe;
    unifiedWebSocketManager.subscribe = ((
      topic: string,
      callback: (message: any) => void
    ) => {
      if (topic === 'approvals') approvalListeners.push(callback);
      return () => {};
    }) as typeof unifiedWebSocketManager.subscribe;
    restoreSubscribe = () => {
      unifiedWebSocketManager.subscribe = originalSubscribe;
    };

    const originalGo = Router.go;
    (Router as unknown as { go: (path: string) => void }).go = (
      path: string
    ) => {
      routes.push(path);
    };
    restoreRouterGo = () => {
      (Router as unknown as { go: unknown }).go = originalGo;
    };
  });

  afterEach(() => {
    restoreFetch();
    restoreSubscribe();
    restoreRouterGo();
    localStorage.removeItem('accessToken');
  });

  async function header(): Promise<ConsoleHeader> {
    const el = await fixture<ConsoleHeader>(
      html`<console-header></console-header>`
    );
    await el.updateComplete;
    await waitUntil(
      () => el.shadowRoot!.textContent?.includes('write_file') ?? false,
      'pending approval never rendered',
      { timeout: 2000 }
    ).catch(() => undefined);
    return el;
  }

  function emit(message: Record<string, unknown>) {
    approvalListeners.forEach((listener) => listener(message));
  }

  function notificationItems(el: ConsoleHeader): HTMLElement[] {
    return Array.from(
      el.shadowRoot!.querySelectorAll<HTMLElement>('.notification-item')
    );
  }

  it('leaves an approval notification when one resolves elsewhere', async () => {
    const el = await header();

    emit({
      type: 'approval_declined',
      approval_request_id: 'ar-1',
      tool_name: 'write_file',
    });
    await el.updateComplete;

    const items = notificationItems(el);
    expect(items.length, 'notification rows').to.equal(1);
    expect(items[0].textContent).to.contain('Approval declined');
    expect(items[0].textContent).to.contain('write_file');
    expect(items[0].querySelector('sl-icon')?.getAttribute('name')).to.equal(
      'shield-check'
    );
    // The pending row is gone, so the notification is the only trace left.
    expect(el.shadowRoot!.querySelectorAll('.approval-item').length).to.equal(
      0
    );
  });

  it('opens the approval when its notification is clicked', async () => {
    const el = await header();
    emit({
      type: 'approval_approved',
      approval_request_id: 'ar-1',
      tool_name: 'write_file',
    });
    await el.updateComplete;

    const item = notificationItems(el)[0];
    expect(item.getAttribute('data-href')).to.equal('/console/approval/ar-1');
    item.click();
    await el.updateComplete;

    expect(routes).to.deep.equal(['/console/approval/ar-1']);
    // Reading it also marks it read, so the badge stops counting it.
    expect(item.classList.contains('unread')).to.be.false;
  });

  it('says nothing about a decision made in this bell', async () => {
    const el = await header();
    const approve = el.shadowRoot!.querySelector<HTMLElement>(
      '.approval-actions sl-button[variant="success"]'
    );
    expect(approve, 'approve button').to.exist;
    approve!.click();
    await waitUntil(
      () => el.shadowRoot!.querySelectorAll('.approval-item').length === 0,
      'approval row never cleared'
    );

    emit({
      type: 'approval_approved',
      approval_request_id: 'ar-1',
      tool_name: 'write_file',
    });
    await el.updateComplete;

    expect(notificationItems(el).length, 'notification rows').to.equal(0);
  });

  it('keeps a notification with no target reachable and marks it read', async () => {
    const el = await header();
    // The websocket also pushes notifications that carry no href (a budget
    // alert, a role change). Clicking one still marks it read, so it is a
    // control, and role="presentation" on a control tells a screen reader the
    // opposite of what it does.
    (el as unknown as { _userNotifications: unknown[] })._userNotifications = [
      {
        id: 'n-1',
        type: 'system',
        title: 'Budget threshold reached',
        message: '',
        created_at: new Date().toISOString(),
        read: false,
      },
    ];
    el.requestUpdate();
    await el.updateComplete;

    const item = notificationItems(el)[0];
    expect(item.getAttribute('role')).to.equal('button');
    expect(item.getAttribute('tabindex')).to.equal('0');

    item.click();
    await el.updateComplete;

    expect(notificationItems(el)[0].classList.contains('unread')).to.be.false;
    // Nothing to open, so nothing was opened.
    expect(routes).to.deep.equal([]);
  });

  it('drops an approval that expires while the tab stays open', async () => {
    const el = await header();

    // It loaded live, so the load-time filter is not what is under test here.
    expect(el.shadowRoot!.querySelectorAll('.approval-item').length).to.equal(
      1
    );
    expect(el.shadowRoot!.querySelector('.notification-badge')).to.exist;

    // Nothing arrives when an approval times out, so the only thing between a
    // dead request and the badge is the filter on read.
    const loaded = (
      el as unknown as { _pendingApprovals: Array<{ expires_at?: string }> }
    )._pendingApprovals;
    loaded[0].expires_at = new Date(Date.now() - 1_000).toISOString();
    el.requestUpdate();
    await el.updateComplete;

    expect(el.shadowRoot!.querySelectorAll('.approval-item').length).to.equal(
      0
    );
    expect(el.shadowRoot!.querySelector('.notification-badge')).to.not.exist;
  });
});

describe('console-header approval deadlines', () => {
  const NOW = Date.parse('2030-01-01T12:00:00Z');
  let clock: sinon.SinonFakeTimers;
  let restoreFetch: () => void;
  let el: ConsoleHeader;
  let approvals: Record<string, unknown>[];
  let approvalReads: number;
  let receiveApproval: Parameters<typeof unifiedWebSocketManager.subscribe>[1];
  let changeState: Parameters<typeof unifiedWebSocketManager.onStateChange>[0];
  let unsubscribeState: sinon.SinonSpy;

  function approval(id: string, delay?: number): Record<string, unknown> {
    return {
      id,
      tool_name: id,
      status: 'pending',
      requested_at: new Date(NOW).toISOString(),
      // Backend timestamps without a timezone must still be read as UTC.
      expires_at:
        delay === undefined
          ? null
          : new Date(NOW + delay).toISOString().replace('Z', ''),
    };
  }

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-token');
    clock = sinon.useFakeTimers({
      now: NOW,
      toFake: ['Date', 'setTimeout', 'clearTimeout'],
    });
    approvals = [];
    approvalReads = 0;
    restoreFetch = stubFetch(() => {
      approvalReads++;
      return approvals;
    });
    sinon.stub(unifiedWebSocketManager, 'subscribe').callsFake((topic, cb) => {
      if (topic === 'approvals') receiveApproval = cb;
      return () => {};
    });
    unsubscribeState = sinon.spy();
    sinon.stub(unifiedWebSocketManager, 'onStateChange').callsFake((cb) => {
      changeState = cb;
      return unsubscribeState;
    });
    if ('Notification' in window) {
      sinon.stub(Notification, 'requestPermission').resolves('denied');
    }
  });

  afterEach(() => {
    el?.remove();
    clock.restore();
    sinon.restore();
    restoreFetch();
    localStorage.removeItem('accessToken');
  });

  async function mount(): Promise<void> {
    el = await fixture<ConsoleHeader>(html`<console-header></console-header>`);
    await clock.tickAsync(0);
    await el.updateComplete;
  }

  function names(): string[] {
    return [...el.shadowRoot!.querySelectorAll('.approval-name')].map((row) =>
      row.textContent!.trim()
    );
  }

  function badge(): string | undefined {
    return el
      .shadowRoot!.querySelector('.notification-badge')
      ?.textContent?.trim();
  }

  it('removes expired rows, decision buttons and badge at each deadline without a message', async () => {
    approvals = [approval('first', 1_000), approval('second', 3_000)];
    await mount();
    expect(names()).to.deep.equal(['first', 'second']);
    expect(badge()).to.equal('2');
    expect(clock.countTimers()).to.equal(1);
    await clock.tickAsync(1_000);
    await el.updateComplete;
    expect(names()).to.deep.equal(['second']);
    expect(badge()).to.equal('1');
    expect(
      el.shadowRoot!.querySelector('.section-count')!.textContent
    ).to.contain('(1)');
    await clock.tickAsync(2_000);
    await el.updateComplete;
    expect(names()).to.deep.equal([]);
    expect(el.shadowRoot!.querySelector('.approval-actions')).to.not.exist;
    expect(badge()).to.equal(undefined);
    expect(approvalReads).to.equal(1);
  });

  it('keeps requests without deadlines and handles deadlines beyond the browser timer limit', async () => {
    approvals = [approval('indefinite'), approval('distant', 3_000_000_000)];
    await mount();
    await clock.tickAsync(2_147_483_647);
    await el.updateComplete;
    expect(names()).to.deep.equal(['indefinite', 'distant']);
    await clock.tickAsync(3_000_000_000 - 2_147_483_647);
    await el.updateComplete;
    expect(names()).to.deep.equal(['indefinite']);
    expect(badge()).to.equal('1');
    expect(clock.countTimers()).to.equal(0);
    expect(approvalReads).to.equal(1);
  });

  it('reschedules when a websocket request has an earlier deadline', async () => {
    approvals = [approval('later', 5_000)];
    await mount();
    receiveApproval({
      ...approval('earlier', 1_000),
      type: 'approval_created',
      approval_request_id: 'earlier',
    });
    await el.updateComplete;
    await clock.tickAsync(1_000);
    await el.updateComplete;
    expect(names()).to.deep.equal(['later']);
    expect(badge()).to.equal('1');
    await clock.tickAsync(4_000);
    await el.updateComplete;
    expect(names()).to.deep.equal([]);
  });

  it('prunes on focus after sleep and refreshes requests missed while away', async () => {
    approvals = [approval('expired-while-asleep', 1_000)];
    await mount();
    clock.setSystemTime(NOW + 2_000);
    approvals = [approval('new-request', 10_000)];
    window.dispatchEvent(new Event('focus'));
    changeState(ConnectionState.CONNECTED);
    window.dispatchEvent(new Event('focus'));
    await clock.tickAsync(0);
    await el.updateComplete;
    expect(names()).to.deep.equal(['new-request']);
    expect(badge()).to.equal('1');
    expect(approvalReads).to.equal(2);
  });

  it('refreshes when a hidden tab becomes visible', async () => {
    approvals = [approval('expires-hidden', 1_000)];
    await mount();
    const visibility = sinon.stub(document, 'visibilityState');
    visibility.get(() => 'hidden');
    document.dispatchEvent(new Event('visibilitychange'));
    expect(approvalReads).to.equal(1);
    clock.setSystemTime(NOW + 2_000);
    approvals = [approval('visible-request', 10_000)];
    visibility.get(() => 'visible');
    document.dispatchEvent(new Event('visibilitychange'));
    await clock.tickAsync(0);
    await el.updateComplete;
    expect(names()).to.deep.equal(['visible-request']);
    expect(badge()).to.equal('1');
    expect(approvalReads).to.equal(2);
  });

  it('refreshes on websocket reconnection and unregisters the state listener on removal', async () => {
    approvals = [approval('resolved-offline', 5_000)];
    await mount();
    approvals = [];
    changeState(ConnectionState.CONNECTED);
    await clock.tickAsync(0);
    await el.updateComplete;
    expect(names()).to.deep.equal([]);
    expect(badge()).to.equal(undefined);
    expect(approvalReads).to.equal(2);
    el.remove();
    expect(unsubscribeState.calledOnce).to.equal(true);
  });

  it('cancels expiry work on removal and refreshes after the element reconnects', async () => {
    approvals = [approval('expires-detached', 1_000)];
    await mount();
    el.remove();
    const timersAfterRemoval = clock.countTimers();
    expect(timersAfterRemoval).to.equal(0);
    window.dispatchEvent(new Event('focus'));
    await clock.tickAsync(2_000);
    expect(approvalReads).to.equal(1);
    document.body.append(el);
    await clock.tickAsync(0);
    await el.updateComplete;
    expect(names()).to.deep.equal([]);
    expect(badge()).to.equal(undefined);
    expect(approvalReads).to.equal(2);
  });
});
