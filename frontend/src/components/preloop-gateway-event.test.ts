import { expect, fixture, html } from '@open-wc/testing';

import './preloop-gateway-event.ts';
import type { PreloopGatewayEvent } from './preloop-gateway-event';
import type { FlowGatewayEvent } from '../types';

/**
 * A model call is a row in the execution timeline, not a card inside it:
 * one sentence behind a chevron, no border, no header band.
 */
describe('preloop-gateway-event', () => {
  function modelCall(payload: Record<string, unknown> = {}): FlowGatewayEvent {
    return {
      id: 'event-1',
      execution_id: 'exec-1',
      timestamp: '2026-03-09T10:00:00Z',
      type: 'model_gateway_call',
      payload: {
        model_alias: 'deepseek/deepseek-v4-pro',
        provider_name: 'deepseek',
        outcome: 'success',
        status_code: 200,
        estimated_cost: 0.0234,
        total_tokens: 1353363,
        duration_ms: 804,
        ...payload,
      },
    } as FlowGatewayEvent;
  }

  async function render(event: FlowGatewayEvent) {
    const element = await fixture<PreloopGatewayEvent>(
      html`<preloop-gateway-event
        .event=${event}
        hide-timestamp
      ></preloop-gateway-event>`
    );
    await element.updateComplete;
    return element;
  }

  it('summarises a model call on one line', async () => {
    const element = await render(modelCall());
    const summary = element.shadowRoot!.querySelector(
      '[slot="summary"]'
    ) as HTMLElement;

    expect(summary.textContent).to.contain('Model call');
    expect(summary.textContent).to.contain('deepseek/deepseek-v4-pro');
    expect(summary.textContent).to.contain('Success');
    expect(summary.textContent).to.contain('$0.02');
    // Compact, because 1,353,363 is a figure to compare, not to read.
    expect(summary.textContent).to.contain('1.4M tokens');
    expect(summary.textContent).to.contain('804 ms');
  });

  it('leads with the HTTP status when the call failed', async () => {
    const element = await render(
      modelCall({
        outcome: 'error',
        status_code: 402,
        estimated_cost: 0,
        total_tokens: 0,
      })
    );
    const summary = element.shadowRoot!.querySelector(
      '[slot="summary"]'
    ) as HTMLElement;

    expect(summary.textContent).to.contain('HTTP 402');
    expect(summary.textContent).to.contain('no tokens');
    // A call that cost nothing prints no price rather than "$0.00".
    expect(summary.textContent).to.not.contain('$0.00');
    const chip = summary.querySelector('sl-badge')!;
    expect(chip.getAttribute('variant')).to.equal('danger');
    expect(chip.classList.contains('chip')).to.be.true;
    expect(chip.classList.contains('solid')).to.be.false;
  });

  it('keeps the uppercase field labels behind the chevron', async () => {
    const element = await render(modelCall());
    const summary = element.shadowRoot!.querySelector('[slot="summary"]')!;

    expect(summary.querySelector('.gateway-event-label')).to.not.exist;
    // The detail still carries them, one chevron away.
    expect(element.shadowRoot!.querySelector('.gateway-event-label')).to.exist;
  });

  it('draws no border and no filled header band', async () => {
    const element = await render(modelCall());
    const details = element.shadowRoot!.querySelector('sl-details')!;
    await (details as unknown as { updateComplete: Promise<unknown> })
      .updateComplete;
    const base = details.shadowRoot!.querySelector('.details') as HTMLElement;
    const header = details.shadowRoot!.querySelector(
      '.details__header'
    ) as HTMLElement;

    const baseStyle = getComputedStyle(base);
    expect(baseStyle.borderTopWidth).to.equal('0px');
    expect(baseStyle.backgroundColor).to.equal('rgba(0, 0, 0, 0)');
    expect(getComputedStyle(header).backgroundColor).to.equal(
      'rgba(0, 0, 0, 0)'
    );
  });

  it('does not print a timestamp the timeline already shows', async () => {
    const element = await render(modelCall());
    const summary = element.shadowRoot!.querySelector('[slot="summary"]')!;
    expect(summary.querySelector('sl-tooltip')).to.not.exist;

    element.hideTimestamp = false;
    await element.updateComplete;
    expect(element.shadowRoot!.querySelector('[slot="summary"] sl-tooltip')).to
      .exist;
  });
});
