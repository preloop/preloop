import { expect, fixture, html, oneEvent } from '@open-wc/testing';
import type SlDetails from '@shoelace-style/shoelace/dist/components/details/details.js';
import type SlInput from '@shoelace-style/shoelace/dist/components/input/input.js';
import type SlSelect from '@shoelace-style/shoelace/dist/components/select/select.js';

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
    const flowSelect = flow.shadowRoot?.querySelector('sl-select') as SlSelect;
    expect(flowSelect.value).to.equal('');
    expect(optionValues(flow)[0]).to.equal('');

    const account = await mount({ context: 'account', value: null });
    const accountSelect = account.shadowRoot?.querySelector(
      'sl-select'
    ) as SlSelect;
    expect(accountSelect.value).to.equal('auto');
    expect(optionValues(account)).to.not.include('');
    expect(optionValues(account)[0]).to.equal('auto');
  });

  it('emits null for the flow default row and server for hosted', async () => {
    const element = await mount({ context: 'flow', value: 'office-mac' });
    const select = element.shadowRoot?.querySelector('sl-select') as SlSelect;

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
    const input = element.shadowRoot?.querySelector('sl-input') as SlInput;
    const changed = oneEvent(element, 'pool-change');
    input.value = 'legacy-pin';
    input.dispatchEvent(new CustomEvent('sl-input'));
    const event = await changed;
    expect(event.detail.value).to.equal('legacy-pin');
    element.value = event.detail.value;
    await element.updateComplete;
    expect(element.shadowRoot?.textContent).to.contain(
      'legacy-pin (not registered)'
    );
  });

  it('keeps customDraft while typing a prefix of a known label', async () => {
    const element = await mount({ context: 'flow', value: null });
    element.addEventListener('pool-change', (event: Event) => {
      const detail = (event as CustomEvent<{ value: string | null }>).detail;
      element.value = detail.value;
    });
    const input = element.shadowRoot?.querySelector('sl-input') as SlInput;
    expect(input.label).to.equal('Label or runner id');

    input.value = 'gpu';
    input.dispatchEvent(new CustomEvent('sl-input'));
    await element.updateComplete;
    expect(input.value).to.equal('gpu');

    input.value = 'gpu-large';
    input.dispatchEvent(new CustomEvent('sl-input'));
    await element.updateComplete;
    expect(input.value).to.equal('gpu-large');
    expect(element.value).to.equal('gpu-large');
  });

  it('opens free-text details when the current value is not a selectable token', async () => {
    const element = await mount({ context: 'flow', value: 'office gpu' });
    const details = element.shadowRoot?.querySelector(
      'sl-details'
    ) as SlDetails;
    const input = element.shadowRoot?.querySelector('sl-input') as SlInput;
    expect(input.value).to.equal('office gpu');
    expect(details.open).to.equal(true);
    expect(element.shadowRoot?.textContent).to.not.contain('(not registered)');
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
