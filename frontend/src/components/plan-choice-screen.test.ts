import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import './plan-choice-screen';
import type { PlanChoiceScreen } from './plan-choice-screen';

/**
 * The published cloud ladder, exactly as the public pricing page and the
 * console plan page read it. Enterprise carries no price on either interval,
 * which is how the catalog says "quoted".
 */
const CONTENT = {
  pricing: {
    title: 'Pricing',
    lead: 'Start free with your own keys.',
    plans: [
      {
        id: 'free',
        name: 'Free',
        price_monthly: 0,
        price_annually: 0,
        deployment: 'cloud',
        tagline: 'Try Preloop with your own keys.',
        features: [],
      },
      {
        id: 'pro',
        name: 'Pro',
        price_monthly: 12,
        price_annually: 120,
        deployment: 'cloud',
        tagline: 'One person, up to 10 agents.',
        features: [],
      },
      {
        id: 'team',
        name: 'Team',
        price_monthly: 120,
        price_annually: 1200,
        deployment: 'cloud',
        tagline: 'Up to 5 people.',
        features: [],
      },
      {
        id: 'enterprise',
        name: 'Enterprise',
        price_monthly: null,
        price_annually: null,
        deployment: 'cloud',
        tagline: 'Priced per deployment.',
        cta_text: 'Contact us',
        cta_url: '/request-demo',
        features: [],
      },
      {
        id: 'dedicated',
        name: 'Dedicated',
        price_monthly: 900,
        price_annually: 9000,
        deployment: 'dedicated',
        tagline: 'Your own install.',
        features: [],
      },
    ],
    comparison: {
      title: 'Compare cloud plans',
      groups: [
        {
          title: 'Plan',
          rows: [
            {
              label: 'Agents',
              values: { free: '3', pro: '10', team: '100', enterprise: 'All' },
            },
          ],
        },
      ],
    },
    faqs: [],
  },
};

describe('PlanChoiceScreen', () => {
  let fetchStub: sinon.SinonStub;
  /** Where a contact card sent the reader, without navigating the runner. */
  let navigated: string[];

  function json(data: unknown, status = 200) {
    return new Response(JSON.stringify(data), {
      status,
      headers: { 'Content-Type': 'application/json' },
    });
  }

  function createFetchStub(
    opts: {
      content?: unknown;
      contentStatus?: number;
      checkout?: Record<string, unknown>;
      checkoutStatus?: number;
      choiceStatus?: number;
    } = {}
  ) {
    return sinon
      .stub(window, 'fetch')
      .callsFake(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === 'string' ? input : input.toString();
        const method = (init?.method || 'GET').toUpperCase();

        if (url.includes('/landing-content.json')) {
          return json(opts.content ?? CONTENT, opts.contentStatus ?? 200);
        }

        if (url.includes('/api/v1/billing/create-checkout-session')) {
          // Never 'redirect' in a test: that assignment would navigate the
          // runner away. The request itself is the thing being asserted.
          return json(
            opts.checkout ?? {
              action: 'stub',
              message: 'Checkout was not opened in this test.',
            },
            opts.checkoutStatus ?? 200
          );
        }

        if (url.includes('/api/v1/billing/plan-choice')) {
          return json({ ok: true }, opts.choiceStatus ?? 200);
        }

        return json({ detail: `Unhandled: ${method} ${url}` }, 500);
      });
  }

  async function mount(
    opts: Parameters<typeof createFetchStub>[0] & { trialDays?: number } = {}
  ): Promise<PlanChoiceScreen> {
    fetchStub = createFetchStub(opts);
    const element = await fixture<PlanChoiceScreen>(
      html`<plan-choice-screen
        .trialDays=${opts.trialDays ?? 14}
      ></plan-choice-screen>`
    );
    // The screen loads its content in connectedCallback; wait for the row.
    await waitUntil(
      () => !element.shadowRoot!.querySelector('.choice-loading'),
      'expected the screen to finish loading'
    );
    await element.updateComplete;
    (element as unknown as { _navigate: (url: string) => void })._navigate = (
      url: string
    ) => navigated.push(url);
    return element;
  }

  function cardNames(el: PlanChoiceScreen): string[] {
    return Array.from(el.shadowRoot!.querySelectorAll('pricing-card')).map(
      (card) =>
        (card.shadowRoot?.querySelector('.plan-name')?.textContent ?? '').trim()
    );
  }

  function card(el: PlanChoiceScreen, planName: string): Element | undefined {
    return Array.from(el.shadowRoot!.querySelectorAll('pricing-card')).find(
      (c) =>
        (
          c.shadowRoot?.querySelector('.plan-name')?.textContent ?? ''
        ).trim() === planName
    );
  }

  function cta(el: PlanChoiceScreen, planName: string): string {
    return (
      card(el, planName)?.shadowRoot?.querySelector('sl-button.cta')
        ?.textContent ?? ''
    )
      .replace(/\s+/g, ' ')
      .trim();
  }

  function note(el: PlanChoiceScreen, planName: string): string {
    return (
      card(el, planName)?.shadowRoot?.querySelector('.cta-note')?.textContent ??
      ''
    )
      .replace(/\s+/g, ' ')
      .trim();
  }

  function click(el: PlanChoiceScreen, planName: string): void {
    (
      card(el, planName)?.shadowRoot?.querySelector(
        'sl-button.cta'
      ) as HTMLElement
    )?.click();
  }

  /** Every URL this test's fetch stub was asked for. */
  function urls(): string[] {
    return fetchStub.getCalls().map((c) => String(c.args[0]));
  }

  function choiceCalls(): sinon.SinonSpyCall[] {
    return fetchStub
      .getCalls()
      .filter((c) => String(c.args[0]).includes('billing/plan-choice'));
  }

  beforeEach(() => {
    navigated = [];
    // This screen only ever renders for somebody who is signed in; without a
    // token `fetchWithAuth` treats every call as a dead session.
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
  });

  afterEach(() => {
    fetchStub?.restore();
    localStorage.clear();
  });

  it('keeps a labelled neutral loader and makes no pricing request while checking', async () => {
    fetchStub = createFetchStub();
    const el = await fixture<PlanChoiceScreen>(
      html`<plan-choice-screen
        .checking=${true}
        email="person@example.com"
      ></plan-choice-screen>`
    );
    await el.updateComplete;
    expect(el.shadowRoot!.textContent).to.contain('person@example.com');
    expect(
      el.shadowRoot!.querySelector('sl-spinner')!.getAttribute('label')
    ).to.equal('Checking your account');
    expect(el.shadowRoot!.querySelector('pricing-card')).to.equal(null);
    expect(
      fetchStub!
        .getCalls()
        .some((call) => String(call.args[0]).includes('landing-content'))
    ).to.equal(false);
    el.checking = false;
    await waitUntil(
      () => el.shadowRoot!.querySelector('pricing-card') !== null
    );
  });

  it('lets the signed-in reader sign out and reaches branded help', async () => {
    const original = (window as any).BRAND_CONFIG;
    (window as any).BRAND_CONFIG = {
      name: 'Example',
      docs_url: 'https://example.com/help',
      branding: { logo_light: '/logo.svg', logo_dark: '/logo.svg' },
    };
    try {
      const el = await mount();
      expect(el.shadowRoot!.querySelector('logo-component')).to.exist;
      expect(el.shadowRoot!.querySelector('a')!.getAttribute('href')).to.equal(
        'https://example.com/help'
      );
      const signOut = Array.from(
        el.shadowRoot!.querySelectorAll('button')
      ).find((button) => button.textContent?.includes('Sign out'))!;
      signOut.click();
      await waitUntil(() => localStorage.getItem('accessToken') === null);
      expect(
        fetchStub!
          .getCalls()
          .some((call) => String(call.args[0]).includes('logout'))
      ).to.equal(true);
    } finally {
      (window as any).BRAND_CONFIG = original;
    }
  });

  it('shows the same cloud ladder as the pricing page, and no dismiss', async () => {
    const element = await mount();

    expect(cardNames(element)).to.deep.equal([
      'Free',
      'Pro',
      'Team',
      'Enterprise',
    ]);
    // Nothing self-hosted: the console sells hosted subscriptions only.
    expect(cardNames(element)).to.not.include('Dedicated');
    expect(element.shadowRoot!.querySelector('pricing-plan-comparison')).to
      .exist;
    // No escape hatch of any kind: this is the founder decision that replaced
    // the dismissible trial dialog.
    expect(element.shadowRoot!.textContent).to.not.match(
      /later|not now|skip|dismiss|maybe/i
    );
  });

  it('states the configured trial terms on the paid cards', async () => {
    const element = await mount({ trialDays: 14 });

    expect(cta(element, 'Pro')).to.equal('Choose Pro');
    expect(note(element, 'Pro')).to.contain('Free for 14 days');
    expect(note(element, 'Pro')).to.contain('card is required');
    expect(cta(element, 'Free')).to.equal('Start on Free');
    expect(note(element, 'Free')).to.contain('No card required');
  });

  it('promises no trial when none is configured', async () => {
    const element = await mount({ trialDays: 0 });

    expect(note(element, 'Pro')).to.not.contain('0 days');
    expect(note(element, 'Pro')).to.contain('final amount');
  });

  it('records the Free choice and hands the console back', async () => {
    const element = await mount();
    let made = 0;
    element.addEventListener('plan-choice-made', () => (made += 1));

    click(element, 'Free');
    await waitUntil(() => made === 1, 'expected the choice to be reported');

    const calls = choiceCalls();
    expect(calls).to.have.length(1);
    expect((calls[0].args[1] as RequestInit).method).to.equal('POST');
    // Free needs no card and therefore no Stripe.
    expect(
      urls().filter((u) => u.includes('create-checkout-session'))
    ).to.have.length(0);
  });

  it('keeps the screen up when the Free choice could not be recorded', async () => {
    const element = await mount({ choiceStatus: 500 });
    let made = 0;
    element.addEventListener('plan-choice-made', () => (made += 1));

    click(element, 'Free');
    await waitUntil(
      () =>
        !!element.shadowRoot!.querySelector(
          '[data-testid="plan-choice-error"]'
        ),
      'expected the failure to be shown'
    );

    expect(made).to.equal(0);
    // Still answerable: the button is not left spinning.
    expect(cta(element, 'Free')).to.equal('Start on Free');
  });

  it('sends a paid plan to checkout and records nothing at the click', async () => {
    const element = await mount();

    click(element, 'Pro');
    await waitUntil(
      () => urls().some((u) => u.includes('create-checkout-session')),
      'expected a checkout request'
    );

    const call = fetchStub
      .getCalls()
      .find((c) => String(c.args[0]).includes('create-checkout-session'))!;
    expect(
      JSON.parse((call.args[1] as RequestInit).body as string)
    ).to.deep.equal({
      plan_id: 'pro',
      interval: 'year',
      return_to: '/console',
    });
    // The whole point: backing out at Stripe returns to `/console`, which is
    // this screen again, because the click itself settled nothing.
    expect(choiceCalls()).to.have.length(0);
  });

  it('carries the chosen billing period into checkout', async () => {
    const element = await mount();
    const toggle = element.shadowRoot!.querySelector('billing-toggle')!;
    toggle.dispatchEvent(
      new CustomEvent('interval-change', {
        detail: { value: 'month' },
        bubbles: true,
        composed: true,
      })
    );
    await element.updateComplete;

    click(element, 'Team');
    await waitUntil(
      () => urls().some((u) => u.includes('create-checkout-session')),
      'expected a checkout request'
    );
    const call = fetchStub
      .getCalls()
      .find((c) => String(c.args[0]).includes('create-checkout-session'))!;
    expect(
      JSON.parse((call.args[1] as RequestInit).body as string).interval
    ).to.equal('month');
  });

  it('explains a refused checkout in the server words and stays put', async () => {
    const element = await mount({
      checkout: { detail: { message: 'This plan is not for sale yet.' } },
      checkoutStatus: 400,
    });
    let made = 0;
    element.addEventListener('plan-choice-made', () => (made += 1));

    click(element, 'Pro');
    await waitUntil(
      () =>
        !!element.shadowRoot!.querySelector(
          '[data-testid="plan-choice-error"]'
        ),
      'expected the refusal to be shown'
    );

    expect(
      element.shadowRoot!.querySelector('[data-testid="plan-choice-error"]')!
        .textContent
    ).to.contain('This plan is not for sale yet.');
    expect(made).to.equal(0);
    expect(choiceCalls()).to.have.length(0);
  });

  it('stops asking when the server says the account is already subscribed', async () => {
    const element = await mount({
      checkout: {
        action: 'refresh',
        message: 'Your subscription is already up to date.',
      },
    });
    let made = 0;
    element.addEventListener('plan-choice-made', () => (made += 1));

    click(element, 'Pro');
    await waitUntil(() => made === 1, 'expected the screen to give way');
    // The subscription is the record of the choice; nothing extra is written.
    expect(choiceCalls()).to.have.length(0);
  });

  it('sends a quoted plan to its contact route without settling anything', async () => {
    const element = await mount();

    expect(cta(element, 'Enterprise')).to.equal('Contact us');
    click(element, 'Enterprise');
    await element.updateComplete;

    expect(navigated).to.deep.equal(['/request-demo']);
    // A sales conversation is not a plan: the screen is still owed an answer.
    expect(choiceCalls()).to.have.length(0);
    expect(
      urls().filter((u) => u.includes('create-checkout-session'))
    ).to.have.length(0);
  });

  it('still offers Free when the price list could not be read', async () => {
    const element = await mount({ contentStatus: 500 });
    let made = 0;
    element.addEventListener('plan-choice-made', () => (made += 1));

    expect(element.shadowRoot!.querySelectorAll('pricing-card')).to.have.length(
      0
    );
    const fallback = element.shadowRoot!.querySelector(
      '[data-testid="plan-choice-free-fallback"]'
    ) as HTMLElement;
    expect(fallback).to.exist;

    fallback.click();
    await waitUntil(() => made === 1, 'expected the choice to be reported');
    expect(choiceCalls()).to.have.length(1);
  });
});
