import { expect, fixture, html, oneEvent } from '@open-wc/testing';

import './preloop-runner-pool-select.ts';
import type { PreloopRunnerPoolSelect } from './preloop-runner-pool-select';

const RUNNERS = [
  {
    name: 'office-mac',
    labels: ['gpu'],
    status: 'online',
  },
  {
    name: 'lab-1',
    labels: ['local'],
    status: 'offline',
  },
];

async function mount(
  props: Partial<{
    value: string | null;
    context: 'flow' | 'account';
    accountPool: string | null;
    hostedMinutesLeft: number | null;
    label: string;
    helpText: string;
    disabled: boolean;
  }> = {}
): Promise<PreloopRunnerPoolSelect> {
  return fixture<PreloopRunnerPoolSelect>(html`
    <preloop-runner-pool-select
      .value=${props.value ?? null}
      .context=${props.context ?? 'flow'}
      .runners=${RUNNERS}
      .accountPool=${props.accountPool ?? null}
      .hostedMinutesLeft=${props.hostedMinutesLeft ?? null}
      .label=${props.label ?? 'Runner pool'}
      .helpText=${props.helpText ?? 'Where the next run executes.'}
      ?disabled=${props.disabled ?? false}
    ></preloop-runner-pool-select>
  `);
}

function optionValues(element: PreloopRunnerPoolSelect): string[] {
  const select = element.shadowRoot?.querySelector('sl-select');
  return Array.from(select?.querySelectorAll('sl-option') || []).map(
    (option) => option.getAttribute('value') ?? ''
  );
}

describe('PreloopRunnerPoolSelect', () => {
  it('renders grouped options and headers', async () => {
    const element = await mount();
    const text = element.shadowRoot?.textContent || '';
    expect(text).to.contain('Account default:');
    expect(text).to.contain('Runners by label');
    expect(text).to.contain('Specific runner');
    expect(text).to.contain('Runners labelled gpu (1 online)');
    expect(text).to.contain('office-mac (online)');
    expect(element.shadowRoot?.querySelector('sl-divider')).to.exist;
  });

  it('preselects the empty inherit row in flow context and auto for a null account value', async () => {
    const flow = await mount({ context: 'flow', value: null });
    const flowSelect = flow.shadowRoot?.querySelector(
      'sl-select'
    ) as HTMLSelectElement;
    expect(flowSelect.value).to.equal('');
    expect(optionValues(flow)[0]).to.equal('');

    const account = await mount({ context: 'account', value: null });
    const accountSelect = account.shadowRoot?.querySelector(
      'sl-select'
    ) as HTMLSelectElement;
    expect(accountSelect.value).to.equal('auto');
    expect(optionValues(account)).to.not.include('');
    expect(optionValues(account)[0]).to.equal('auto');
  });

  it('emits null for the flow default row and server for hosted', async () => {
    const element = await mount({ context: 'flow', value: 'office-mac' });
    const select = element.shadowRoot?.querySelector(
      'sl-select'
    ) as HTMLSelectElement;

    const inherit = oneEvent(element, 'pool-change');
    select.value = '';
    select.dispatchEvent(new CustomEvent('sl-change'));
    expect((await inherit).detail.value).to.equal(null);

    const hosted = oneEvent(element, 'pool-change');
    select.value = 'server';
    select.dispatchEvent(new CustomEvent('sl-change'));
    expect((await hosted).detail.value).to.equal('server');
  });

  it('emits a typed value and shows the not registered row', async () => {
    const element = await mount({ context: 'flow', value: null });
    const input = element.shadowRoot?.querySelector(
      'sl-input'
    ) as HTMLInputElement;
    const changed = oneEvent(element, 'pool-change');
    input.value = 'legacy-pin';
    input.dispatchEvent(new CustomEvent('sl-input'));
    expect((await changed).detail.value).to.equal('legacy-pin');
    await element.updateComplete;
    expect(element.shadowRoot?.textContent).to.contain(
      'legacy-pin (not registered)'
    );
  });

  it('disables the select and free-text field', async () => {
    const element = await mount({ disabled: true });
    const select = element.shadowRoot?.querySelector('sl-select');
    const input = element.shadowRoot?.querySelector('sl-input');
    expect(select?.hasAttribute('disabled')).to.equal(true);
    expect(input?.hasAttribute('disabled')).to.equal(true);
  });

  it('renders the next-run hint', async () => {
    const element = await mount({
      context: 'flow',
      value: null,
      accountPool: null,
    });
    const hint = element.shadowRoot?.querySelector('.runner-pool-hint');
    expect(hint).to.exist;
    expect(hint?.textContent).to.equal(
      'Next run: a private runner (office-mac online). Falls back to Preloop hosted when none is free.'
    );
  });
});
