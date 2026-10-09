import { LitElement, html } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import { fixture, expect } from '@open-wc/testing';
import { ConsoleStatus } from './console-status';

@customElement('status-test-view')
class StatusTestView extends LitElement {
  readonly status = new ConsoleStatus(this);
  @state() loading = true;
  @state() error: string | null = null;
  @state() visible = true;
  render() {
    return this.visible ? html`<h1>Example</h1>` : html``;
  }
}

describe('ConsoleStatus', () => {
  it('announces loading, completion and failure without making visible copy', async () => {
    const view = await fixture<StatusTestView>(
      html`<status-test-view></status-test-view>`
    );
    const region = () =>
      view.shadowRoot!.querySelector<HTMLElement>('[data-console-status]')!;
    expect(region().getAttribute('role')).to.equal('status');
    expect(region().getAttribute('aria-atomic')).to.equal('true');
    expect(region().textContent).to.equal('Loading updates.');
    expect(region().style.clipPath).to.equal('inset(50%)');
    view.loading = false;
    await view.updateComplete;
    expect(region().textContent).to.equal('Page ready.');
    view.error = 'Unavailable';
    await view.updateComplete;
    expect(region().textContent).to.include('Could not complete');
    view.status.announce('1 new approval request.');
    await view.updateComplete;
    expect(region().textContent).to.equal('1 new approval request.');
    view.visible = false;
    await view.updateComplete;
    expect(view.shadowRoot!.querySelector('[data-console-status]')).to.equal(
      null
    );
  });
});
