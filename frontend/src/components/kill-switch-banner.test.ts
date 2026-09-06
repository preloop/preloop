/**
 * Behavior contract for the kill-switch banner (#157).
 *
 * The banner is the console's halted-state surface: while any scope of the
 * account kill switch is active it must render on every page, state which
 * traffic classes are blocked, attribute the halt, and expose the staged
 * recovery (per-scope resume + resume-all). While inactive it renders
 * nothing.
 */
import { expect, fixture, html, waitUntil } from '@open-wc/testing';
import './kill-switch-banner.ts';
import type { KillSwitchBanner } from './kill-switch-banner.ts';
import type { KillSwitchStatus } from '../types';

const INACTIVE: KillSwitchStatus = { active: false, scopes: [] };

const FULL_HALT: KillSwitchStatus = {
  active: true,
  scopes: [
    {
      scope: 'gateway',
      activated_by_user_id: 'user-1',
      activated_by_username: 'jdoe',
      activated_at: '2026-09-06T12:00:00Z',
      reason: 'runaway spend loop',
    },
    {
      scope: 'tools',
      activated_by_user_id: 'user-1',
      activated_by_username: 'jdoe',
      activated_at: '2026-09-06T12:00:00Z',
      reason: null,
    },
    {
      scope: 'flows',
      activated_by_user_id: 'user-1',
      activated_by_username: 'jdoe',
      activated_at: '2026-09-06T12:00:00Z',
      reason: null,
    },
  ],
};

const PARTIAL_HALT: KillSwitchStatus = {
  active: true,
  scopes: [
    {
      scope: 'flows',
      activated_by_user_id: 'user-2',
      activated_by_username: 'asmith',
      activated_at: '2026-09-06T13:30:00Z',
      reason: 'verify behavior before resuming',
    },
  ],
};

function stubFetch(statusByCall: KillSwitchStatus[]): () => void {
  const original = window.fetch;
  let statusIndex = 0;
  window.fetch = (async (input: RequestInfo | URL) => {
    const url = typeof input === 'string' ? input : input.toString();
    if (url.includes('/account/kill-switch/status')) {
      const status = statusByCall[statusIndex] ?? INACTIVE;
      statusIndex += 1;
      return new Response(JSON.stringify(status), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    }
    return new Response(JSON.stringify({}), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  }) as typeof window.fetch;
  return () => {
    window.fetch = original;
  };
}

function deactivateRequests(): string[] {
  // Captured by stubFetch below via a closure on window.fetch calls.
  return (
    (window as unknown as { __deactivateBodies?: string[] })
      .__deactivateBodies ?? []
  );
}

describe('kill-switch-banner', () => {
  let restoreFetch: () => void;
  let el: KillSwitchBanner;

  beforeEach(async () => {
    localStorage.setItem('accessToken', 'test-token');
    (
      window as unknown as { __deactivateBodies?: string[] }
    ).__deactivateBodies = [];
  });

  afterEach(() => {
    restoreFetch?.();
    localStorage.removeItem('accessToken');
  });

  async function mount(statusByCall: KillSwitchStatus[]): Promise<void> {
    const original = window.fetch;
    const bodies: string[] = [];
    window.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input.toString();
      if (url.includes('/account/kill-switch/deactivate')) {
        bodies.push(String(init?.body ?? ''));
        return new Response(JSON.stringify(INACTIVE), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      if (url.includes('/account/kill-switch/status')) {
        return new Response(JSON.stringify(statusByCall[0] ?? INACTIVE), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      return original(input, init);
    }) as typeof window.fetch;
    restoreFetch = () => {
      window.fetch = original;
    };
    (
      window as unknown as { __deactivateBodies?: string[] }
    ).__deactivateBodies = bodies;

    el = await fixture<KillSwitchBanner>(
      html`<kill-switch-banner></kill-switch-banner>`
    );
    await waitUntil(() => (el as any).status !== null);
    await el.updateComplete;
  }

  it('renders nothing while the account is not halted', async () => {
    await mount([INACTIVE]);
    expect(el.shadowRoot?.textContent?.trim()).to.equal('');
  });

  it('announces a full halt with every blocked traffic class', async () => {
    await mount([FULL_HALT]);
    const text = el.shadowRoot!.textContent ?? '';
    expect(text).to.contain('Agent requests are halted');
    expect(text).to.contain('Model requests blocked');
    expect(text).to.contain('Tool calls blocked');
    expect(text).to.contain('Flow executions blocked');
  });

  it('attributes the halt: who and why', async () => {
    await mount([FULL_HALT]);
    const text = el.shadowRoot!.textContent ?? '';
    expect(text).to.contain('by jdoe');
    expect(text).to.contain('runaway spend loop');
  });

  it('marks the alert role so assistive tech announces it', async () => {
    await mount([FULL_HALT]);
    const banner = el.shadowRoot!.querySelector('.banner');
    expect(banner?.getAttribute('role')).to.equal('alert');
  });

  it('distinguishes a partial (staged) halt', async () => {
    await mount([PARTIAL_HALT]);
    const text = el.shadowRoot!.textContent ?? '';
    expect(text).to.contain('partially halted');
    expect(text).to.contain('Flow executions blocked');
    expect(text).to.not.contain('Model requests blocked');
  });

  it('resumes a single scope from a partial halt (staged recovery)', async () => {
    await mount([PARTIAL_HALT]);
    const button = [...(el.shadowRoot?.querySelectorAll('button') || [])].find(
      (candidate) => candidate.textContent?.includes('Resume flow executions')
    );
    expect(button, 'per-scope resume button').to.exist;
    (button as HTMLElement).click();
    await el.updateComplete;

    const bodies = deactivateRequests();
    expect(bodies).to.have.length(1);
    expect(JSON.parse(bodies[0])).to.deep.equal({ scopes: ['flows'] });
  });

  it('offers resume-all only when more than one scope is halted', async () => {
    await mount([PARTIAL_HALT]);
    expect(el.shadowRoot!.textContent?.includes('Resume all')).to.be.false;

    await mount([FULL_HALT]);
    const resumeAll = [
      ...(el.shadowRoot?.querySelectorAll('button') || []),
    ].find((candidate) => candidate.textContent?.trim() === 'Resume all');
    expect(resumeAll, 'resume-all button').to.exist;
    (resumeAll as HTMLElement).click();
    await el.updateComplete;

    const bodies = deactivateRequests();
    expect(bodies).to.have.length(1);
    expect(JSON.parse(bodies[0])).to.deep.equal({
      scopes: ['gateway', 'tools', 'flows'],
    });
  });

  it('keeps the banner up with an inline error when the lift fails', async () => {
    const original = window.fetch;
    window.fetch = (async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input.toString();
      if (url.includes('/account/kill-switch/status')) {
        return new Response(JSON.stringify(FULL_HALT), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      if (url.includes('/account/kill-switch/deactivate')) {
        return new Response(
          JSON.stringify({
            detail: 'Insufficient permissions. Required: manage_kill_switch',
          }),
          { status: 403, headers: { 'Content-Type': 'application/json' } }
        );
      }
      return original(input);
    }) as typeof window.fetch;
    restoreFetch = () => {
      window.fetch = original;
    };

    el = await fixture<KillSwitchBanner>(
      html`<kill-switch-banner></kill-switch-banner>`
    );
    await waitUntil(() => el.shadowRoot?.querySelector('.banner') !== null);
    await el.updateComplete;

    const button = [...(el.shadowRoot?.querySelectorAll('button') || [])].find(
      (candidate) => candidate.textContent?.includes('Resume model requests')
    );
    (button as HTMLElement).click();
    await el.updateComplete;
    await el.updateComplete;

    await waitUntil(() =>
      el.shadowRoot!.textContent!.includes('Insufficient permissions')
    );
    const text = el.shadowRoot!.textContent ?? '';
    expect(text).to.include('Insufficient permissions');
    expect(text).to.include('Agent requests are halted');
  });
});
