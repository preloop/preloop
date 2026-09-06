/**
 * Accessibility contract for the console header's user menu trigger.
 *
 * The avatar replaced an <sl-icon-button>, so the trigger has to keep the
 * button semantics it used to get for free: a real focusable control with an
 * accessible name that opens the dropdown on activation. These tests pin that
 * contract so a future restyle cannot silently drop keyboard access.
 */
import { expect, fixture, html, waitUntil } from '@open-wc/testing';
import './console-header.ts';
import type { ConsoleHeader } from './console-header.ts';
import { publishAttentionSummary } from '../utils/attention-summary';
import { unifiedWebSocketManager } from '../services/unified-websocket-manager';
import { Router } from '@vaadin/router';

const USER = {
  id: 'user-1',
  username: 'alice',
  email: 'alice@example.com',
  full_name: 'Alice Smith',
  avatar_url: null,
};

/** Answer every console-header startup request so rendering is deterministic. */
function stubFetch(): () => void {
  const original = window.fetch;
  window.fetch = (async (input: RequestInfo | URL) => {
    const url = typeof input === 'string' ? input : input.toString();
    const body = url.includes('/users/me') ? USER : [];
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

/**
 * The bell and approvals.
 *
 * The bell already lists pending approvals and opens them on click. What it
 * did not do was say anything when one of those approvals stopped waiting:
 * the row vanished mid-session with no trace, and the generic notification
 * rows it does keep went nowhere when clicked. These tests pin the approval
 * notification type, where its click lands, and the two cases that should
 * stay quiet.
 */
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

  it('does not count an approval that has already expired', async () => {
    approvals = [
      { ...APPROVAL, expires_at: new Date(Date.now() - 60_000).toISOString() },
    ];
    const el = await header();

    expect(el.shadowRoot!.querySelectorAll('.approval-item').length).to.equal(
      0
    );
    expect(el.shadowRoot!.querySelector('.notification-badge')).to.not.exist;
  });
});
