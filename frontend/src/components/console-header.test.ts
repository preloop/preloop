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
import { loadShoelaceTokens } from '../utils/test-shoelace-theme';
import { Router } from '../router';

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
describe('console-header notification button', () => {
  let restoreFetch: () => void;
  let el: ConsoleHeader;

  beforeEach(async () => {
    await loadShoelaceTokens();
    localStorage.setItem('accessToken', 'test-token');
    restoreFetch = stubFetch();
    el = await fixture<ConsoleHeader>(html`<console-header></console-header>`);
    await el.updateComplete;
  });

  afterEach(() => {
    restoreFetch();
    localStorage.removeItem('accessToken');
  });

  it('sits at the optical size of the header icons around it', async () => {
    const bell = el.shadowRoot!.querySelector<HTMLElement>(
      '.notification-button sl-icon-button'
    )!;
    expect(bell.getAttribute('name'), 'the plain bell glyph').to.equal('bell');
    // 1.375rem. The size is set on the button, not baked into a special
    // icon, and it is below the 1.5rem nav toggle because a bell's ink fills
    // its box where a hamburger's does not. At 1.8rem the glyph was 28.8px
    // in a 45px box, taller than the 32px avatar beside it.
    const fontSize = parseFloat(getComputedStyle(bell).fontSize);
    expect(fontSize, 'bell glyph size').to.equal(22);
    const box = bell.getBoundingClientRect();
    expect(box.height, 'bell button height').to.be.at.most(40);
    expect(
      box.height,
      'bell button is still a comfortable target'
    ).to.be.at.least(32);

    const avatar = el
      .shadowRoot!.querySelector('button.user-menu-trigger')!
      .getBoundingClientRect();
    expect(
      box.height,
      'bell is no louder than the avatar beside it'
    ).to.be.at.most(avatar.height + 8);
  });

  it('keeps the badge on the corner of the smaller button', async () => {
    // A pending approval is what puts a count on the bell.
    restoreFetch();
    restoreFetch = stubFetch(() => [
      {
        id: 'ar-1',
        tool_name: 'write_file',
        tool_args: {},
        status: 'pending',
        requested_at: new Date().toISOString(),
        expires_at: new Date(Date.now() + 600_000).toISOString(),
        execution_id: 'exec-1',
      },
    ]);
    el = await fixture<ConsoleHeader>(html`<console-header></console-header>`);
    await el.updateComplete;
    await waitUntil(
      () => !!el.shadowRoot?.querySelector('.notification-badge'),
      'badge never appeared'
    );

    const wrapper = el
      .shadowRoot!.querySelector('.notification-button')!
      .getBoundingClientRect();
    const badge = el
      .shadowRoot!.querySelector('.notification-badge')!
      .getBoundingClientRect();
    // Anchored to the button's own top-right corner, so the smaller button
    // did not leave it floating in the header.
    expect(
      Math.abs(badge.right - (wrapper.right + 4)),
      'badge x'
    ).to.be.at.most(1);
    expect(Math.abs(badge.top - (wrapper.top - 4)), 'badge y').to.be.at.most(1);
    expect(badge.width, 'badge is drawn').to.be.greaterThan(0);
  });
});

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
 * A handled approval is not a notification.
 *
 * The bell used to leave an unread "Approval approved" row behind every
 * resolution, so policy auto-approvals nobody ever looked at pushed the badge
 * up for good: there is no read-all, and nothing ages an entry out. The bell
 * now converges on the server's truth, which is pending unexpired approvals
 * and nothing else. The history of what was decided lives on the Approvals
 * page and in the audit trail.
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
  const OTHER_APPROVAL = {
    ...APPROVAL,
    id: 'ar-2',
    tool_name: 'read_file',
  };

  let restoreFetch: () => void;
  let restoreSubscribe: () => void;
  let approvalListeners: ((message: any) => void)[];
  let approvals: any[];
  let approvalReads: number;
  /** When set, the pending list request waits on it before answering. */
  let gate: Promise<void> | null;
  let routes: string[];
  let restoreRouterGo: () => void;

  /** Answer startup requests, including the pending approvals load. */
  function stubApprovalFetch(): () => void {
    const original = window.fetch;
    window.fetch = (async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input.toString();
      let body: unknown = [];
      if (url.includes('/users/me')) body = USER;
      else if (url.includes('/approval-requests')) {
        approvalReads++;
        if (gate) await gate;
        // Read after the gate, so a test can change the answer mid-flight.
        body = approvals;
      }
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
    approvalReads = 0;
    gate = null;
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

  function names(el: ConsoleHeader): string[] {
    return [...el.shadowRoot!.querySelectorAll('.approval-name')].map((row) =>
      row.textContent!.trim()
    );
  }

  function badge(el: ConsoleHeader): string | undefined {
    return el
      .shadowRoot!.querySelector('.notification-badge')
      ?.textContent?.trim();
  }

  function unreadCount(el: ConsoleHeader): number {
    return (el as unknown as { _userNotifications: { read: boolean }[] })
      ._userNotifications.length;
  }

  /**
   * Every way an approval can stop waiting, including the one nobody looked
   * at: a policy bypass approving without review. The badge has to drop for
   * all of them, and none may leave an unread row behind.
   */
  const RESOLUTIONS: { label: string; message: Record<string, unknown> }[] = [
    {
      label: 'approved by another operator',
      message: { type: 'approval_approved', tool_name: 'write_file' },
    },
    {
      label: 'auto-approved by policy with nobody watching',
      message: {
        type: 'approval_approved',
        tool_name: 'write_file',
        status: 'approved',
        auto_approved_reason: 'configured_bypass',
        summary: 'Auto-approved without review: native tool approvals are off',
      },
    },
    {
      label: 'declined',
      message: { type: 'approval_declined', tool_name: 'write_file' },
    },
    {
      label: 'expired',
      message: { type: 'approval_expired', tool_name: 'write_file' },
    },
    {
      label: 'cancelled',
      message: { type: 'approval_cancelled', tool_name: 'write_file' },
    },
  ];

  RESOLUTIONS.forEach(({ label, message }) => {
    it(`drops a request ${label} from the list and the badge`, async () => {
      approvals = [APPROVAL, OTHER_APPROVAL];
      const el = await header();
      expect(names(el)).to.deep.equal(['write_file', 'read_file']);
      expect(badge(el)).to.equal('2');

      emit({ ...message, approval_request_id: 'ar-1' });
      await el.updateComplete;

      expect(names(el), 'pending rows').to.deep.equal(['read_file']);
      expect(badge(el), 'badge').to.equal('1');
      // Nothing was left for the operator to acknowledge.
      expect(notificationItems(el), 'notification rows').to.have.lengthOf(0);
      expect(unreadCount(el), 'stored notifications').to.equal(0);
    });
  });

  it('empties the bell when the last pending request resolves', async () => {
    const el = await header();
    expect(badge(el)).to.equal('1');

    emit({
      type: 'approval_approved',
      approval_request_id: 'ar-1',
      tool_name: 'write_file',
    });
    await el.updateComplete;

    expect(names(el)).to.deep.equal([]);
    expect(badge(el), 'badge is gone, not "1"').to.equal(undefined);
    expect(notificationItems(el)).to.have.lengthOf(0);
    expect(
      el.shadowRoot!.querySelector('.empty-state')?.textContent
    ).to.contain('No new notifications');
  });

  it('ignores a resolution for a request this bell never carried', async () => {
    const el = await header();

    emit({
      type: 'approval_approved',
      approval_request_id: 'somebody-elses-request',
      tool_name: 'deploy',
    });
    await el.updateComplete;

    expect(names(el)).to.deep.equal(['write_file']);
    expect(badge(el)).to.equal('1');
    expect(notificationItems(el), 'notification rows').to.have.lengthOf(0);
    expect(unreadCount(el), 'stored notifications').to.equal(0);
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

    expect(notificationItems(el), 'notification rows').to.have.lengthOf(0);
  });

  it('does not resurrect a resolved row when a later list still says pending', async () => {
    const el = await header();

    emit({
      type: 'approval_approved',
      approval_request_id: 'ar-1',
      tool_name: 'write_file',
    });
    await el.updateComplete;
    expect(names(el)).to.deep.equal([]);

    // The refresh on focus answers with the row still pending, either from a
    // read replica behind the decision or from a response prepared before it.
    window.dispatchEvent(new Event('focus'));
    await waitUntil(() => approvalReads >= 2, 'refresh never ran');
    await el.updateComplete;

    expect(names(el), 'resolved row came back').to.deep.equal([]);
    expect(badge(el)).to.equal(undefined);
    expect(notificationItems(el)).to.have.lengthOf(0);
  });

  it('does not resurrect a row resolved while a list fetch is in flight', async () => {
    approvals = [APPROVAL, OTHER_APPROVAL];
    const el = await header();
    expect(names(el)).to.deep.equal(['write_file', 'read_file']);

    let release!: () => void;
    gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    window.dispatchEvent(new Event('focus'));
    await waitUntil(() => approvalReads >= 2, 'refresh never started');

    emit({
      type: 'approval_declined',
      approval_request_id: 'ar-1',
      tool_name: 'write_file',
    });
    await el.updateComplete;
    expect(names(el)).to.deep.equal(['read_file']);

    release();
    gate = null;
    await waitUntil(
      () => names(el).length > 0,
      'in-flight answer never applied'
    );
    await el.updateComplete;

    expect(names(el), 'resolved row came back').to.deep.equal(['read_file']);
    expect(badge(el)).to.equal('1');
    expect(notificationItems(el)).to.have.lengthOf(0);
  });

  it('forgets a resolution once the server list agrees', async () => {
    const el = await header();
    emit({
      type: 'approval_approved',
      approval_request_id: 'ar-1',
      tool_name: 'write_file',
    });
    await el.updateComplete;

    const held = (el as unknown as { resolvedApprovals: Map<string, number> })
      .resolvedApprovals;
    expect(held.has('ar-1'), 'held back until the server agrees').to.be.true;

    approvals = [];
    window.dispatchEvent(new Event('focus'));
    await waitUntil(() => approvalReads >= 2, 'refresh never ran');
    await el.updateComplete;

    // A tab left open for a day must not accumulate one entry per approval.
    expect(held.size, 'resolved ids held').to.equal(0);
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

  it('opens a notification that names a destination and marks it read', async () => {
    const el = await header();
    (el as unknown as { _userNotifications: unknown[] })._userNotifications = [
      {
        id: 'n-2',
        type: 'policy_added',
        title: 'Policy assigned',
        message: 'Production guardrails',
        created_at: new Date().toISOString(),
        read: false,
        href: '/console/policies',
      },
    ];
    el.requestUpdate();
    await el.updateComplete;

    const item = notificationItems(el)[0];
    expect(item.getAttribute('data-href')).to.equal('/console/policies');
    item.click();
    await el.updateComplete;

    expect(routes).to.deep.equal(['/console/policies']);
    expect(item.classList.contains('unread')).to.be.false;
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

  /**
   * Fetch + json() are native promises, so a single tickAsync(0) can fire
   * the coalesced refresh timer without the list answer landing. Flush
   * microtasks without advancing expiry timers (those are at least 1ms).
   */
  async function flushApprovalReads(count: number): Promise<void> {
    for (let i = 0; i < 25; i++) {
      await Promise.resolve();
      await clock.tickAsync(0);
      await el.updateComplete;
      if (approvalReads >= count) {
        await Promise.resolve();
        await clock.tickAsync(0);
        await el.updateComplete;
        return;
      }
    }
  }

  async function mount(): Promise<void> {
    el = await fixture<ConsoleHeader>(html`<console-header></console-header>`);
    await flushApprovalReads(1);
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

  it('omits already-expired requests from the pending menu and badge', async () => {
    approvals = [approval('alive', 5_000), approval('already-expired', -1_000)];
    await mount();
    expect(names()).to.deep.equal(['alive']);
    expect(badge()).to.equal('1');
    expect(
      el.shadowRoot!.querySelector('.section-count')!.textContent
    ).to.contain('(1)');
    expect(clock.countTimers()).to.equal(1);
  });

  it('ignores a websocket create whose deadline has already passed', async () => {
    approvals = [approval('alive', 5_000)];
    await mount();
    receiveApproval({
      ...approval('stale', -1_000),
      type: 'approval_created',
      approval_request_id: 'stale',
    });
    await clock.tickAsync(0);
    await el.updateComplete;
    expect(names()).to.deep.equal(['alive']);
    expect(badge()).to.equal('1');
  });

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

  it('drops expired rows from updated() and converges without looping', async () => {
    approvals = [approval('first', 1_000), approval('second', 5_000)];
    await mount();
    const header = el as ConsoleHeader & {
      pruneAndScheduleApprovalExpiry: () => void;
    };
    const prune = sinon.spy(header, 'pruneAndScheduleApprovalExpiry');
    await clock.tickAsync(1_000);
    await el.updateComplete;
    await el.updateComplete;
    await clock.tickAsync(0);
    await el.updateComplete;
    expect(names()).to.deep.equal(['second']);
    expect(badge()).to.equal('1');
    // Timer callback prunes once; updated() prunes once more and the
    // length-equality check stops further _pendingApprovals writes.
    expect(prune.callCount).to.equal(2);
    expect(clock.countTimers()).to.equal(1);
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
    await flushApprovalReads(2);
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
    await flushApprovalReads(2);
    expect(names()).to.deep.equal(['visible-request']);
    expect(badge()).to.equal('1');
    expect(approvalReads).to.equal(2);
  });

  it('refreshes on websocket reconnection and unregisters the state listener on removal', async () => {
    approvals = [approval('resolved-offline', 5_000)];
    await mount();
    approvals = [];
    changeState(ConnectionState.CONNECTED);
    await flushApprovalReads(2);
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
    await flushApprovalReads(2);
    expect(names()).to.deep.equal([]);
    expect(badge()).to.equal(undefined);
    expect(approvalReads).to.equal(2);
  });
});
