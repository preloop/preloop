import { html, fixture, expect } from '@open-wc/testing';
import './assignments-view';
import { AssignmentsView } from './assignments-view';

describe('AssignmentsView', () => {
  it('renders the coming-soon placeholder', async () => {
    const el = (await fixture(
      html`<assignments-view></assignments-view>`
    )) as AssignmentsView;
    await el.updateComplete;
    expect(el.shadowRoot?.querySelector('h1')).to.exist;
    expect(el.shadowRoot?.textContent).to.contain('Coming Soon');
  });
});
