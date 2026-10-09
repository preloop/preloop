import { expect, fixture, html, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';
import './hosted-allowance';
import type { HostedAllowance } from './hosted-allowance';

describe('hosted inventory and durable allowance', () => {
  let fetchStub: sinon.SinonStub;
  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-token');
  });
  afterEach(() => {
    fetchStub.restore();
    localStorage.clear();
  });
  function stub(
    enabled: boolean,
    unknown = false,
    kind = 'monthly',
    reset: string | null = '2026-11-01T00:00:00Z'
  ) {
    fetchStub = sinon
      .stub(window, 'fetch')
      .callsFake(async (input: RequestInfo | URL) => {
        const path = String(input);
        return new Response(
          JSON.stringify(
            path.includes('/features')
              ? { features: { hosted_models: enabled }, plugins: [] }
              : {
                  models: [
                    {
                      id: 'hosted-1',
                      name: 'Hosted example',
                      alias: 'example/model',
                      provider_name: 'example',
                      tariff: {
                        input_price_per_1k: 0.001,
                        output_price_per_1k: 0.002,
                        request_price: 0,
                      },
                      own_alias_shadowing: true,
                    },
                  ],
                  allowance: {
                    kind,
                    included_usd: 10,
                    spent_usd: unknown ? null : 3,
                    held_usd: unknown ? null : 2,
                    remaining_usd: unknown ? null : 5,
                    reset_at: reset,
                    coverage: unknown ? 'unknown' : 'known',
                  },
                }
          ),
          { status: 200, headers: { 'Content-Type': 'application/json' } }
        );
      });
  }
  it('shows hosted tariffs, own alias precedence, spend and reservations separately', async () => {
    stub(true);
    const el = await fixture<HostedAllowance>(
      html`<hosted-allowance show-models></hosted-allowance>`
    );
    await waitUntil(() =>
      el.shadowRoot!.textContent!.includes('Held (open reservations)')
    );
    const text = el.shadowRoot!.textContent!.replace(/\s+/g, ' ');
    expect(text)
      .to.contain('Built-in (Preloop hosted)')
      .and.contain('Hosted example')
      .and.contain('takes precedence');
    expect(text).to.contain('$3.00').and.contain('$2.00').and.contain('$5.00');
    expect(text).to.contain('$1.00 / million input tokens');
  });
  it('preserves unknown balances instead of displaying zero', async () => {
    stub(true, true);
    const el = await fixture<HostedAllowance>(
      html`<hosted-allowance></hosted-allowance>`
    );
    await waitUntil(() => el.shadowRoot!.textContent!.includes('Not verified'));
    expect(el.shadowRoot!.textContent).not.to.contain('$0.00');
  });
  it('does not fetch hosted endpoints without the capability', async () => {
    stub(false);
    const el = await fixture<HostedAllowance>(
      html`<hosted-allowance></hosted-allowance>`
    );
    await el.updateComplete;
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(
      fetchStub
        .getCalls()
        .some((call) => String(call.args[0]).includes('/hosted-models'))
    ).to.equal(false);
    expect(el.shadowRoot!.querySelector('section')).to.equal(null);
  });
  for (const [kind, expected] of [
    ['monthly', 'Monthly reset date is not yet verified.'],
    ['one_time', 'One-time credit does not reset.'],
  ]) {
    it(`uses ${kind} kind when the reset date is unknown`, async () => {
      stub(true, true, kind, null);
      const el = await fixture<HostedAllowance>(
        html`<hosted-allowance></hosted-allowance>`
      );
      await waitUntil(() => el.shadowRoot!.textContent!.includes(expected));
      if (kind === 'monthly')
        expect(el.shadowRoot!.textContent).not.to.contain(
          'One-time credit does not reset.'
        );
    });
  }
});
