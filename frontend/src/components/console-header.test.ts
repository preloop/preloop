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
