import { expect, fixture, html, waitUntil } from '@open-wc/testing';
import { render } from 'lit';
import sinon from 'sinon';
import './session-history-widget';
import './session-replay-panel';
import { unifiedWebSocketManager } from '../services/unified-websocket-manager';

const event = (id: string) => ({
  id,
  execution_id: 'example-run',
  timestamp: '2026-01-01T00:00:00Z',
  type: 'model_gateway_call',
  payload: { model_alias: 'example-model' },
});

describe('shared gateway event expansion', () => {
  afterEach(() => sinon.restore());

  it('keeps history choices after prepending, refetching and switching sessions', async () => {
    sinon.stub(unifiedWebSocketManager, 'subscribe').returns(() => {});
    const widget: any = await fixture(
      html`<session-history-widget></session-history-widget>`
    );
    sinon.stub(widget, '_handleEventShow').resolves();
    widget.sessions = [{ id: 'session-1', started_at: '2026-01-01T00:00:00Z' }];
    widget.activeSessionId = 'session-1';
    widget.loadedEvents = { 'session-1': [event('event-1')] };
    await widget.updateComplete;
    const entry = () =>
      [...widget.shadowRoot.querySelectorAll('preloop-gateway-event')].find(
        (el: any) => el.event.id === 'event-1'
      ) as any;
    await entry().updateComplete;
    const details = entry().shadowRoot.querySelector('sl-details');
    await details.updateComplete;
    details.open = true;
    await waitUntil(() => entry().expanded);
    widget.loadedEvents = { 'session-1': [event('event-2'), event('event-1')] };
    await widget.updateComplete;
    expect(entry().expanded).to.equal(true);
    widget.activeSessionId = null;
    await widget.updateComplete;
    widget.activeSessionId = 'session-1';
    await widget.updateComplete;
    await entry().updateComplete;
    expect(entry().expanded).to.equal(true);
    const restored = entry().shadowRoot.querySelector('sl-details');
    await restored.updateComplete;
    restored.open = false;
    await waitUntil(() => !entry().expanded);
    widget.loadedEventDetails = { 'event-1': event('event-1') };
    await widget.updateComplete;
    expect(entry().expanded).to.equal(false);
  });

  it('keeps raw transcript events collapsed when their window remounts', async () => {
    const panel: any = document.createElement('session-replay-panel');
    const host = await fixture<HTMLElement>(html`<div></div>`);
    render(panel.renderRawGatewayEvent(event('event-1')), host);
    let entry: any = host.querySelector('preloop-gateway-event');
    await entry.updateComplete;
    const details = entry.shadowRoot.querySelector('sl-details');
    await details.updateComplete;
    details.open = false;
    await waitUntil(() => !entry.expanded);
    render(html``, host);
    render(panel.renderRawGatewayEvent(event('event-1')), host);
    entry = host.querySelector('preloop-gateway-event');
    await entry.updateComplete;
    expect(entry.expanded).to.equal(false);
    expect(entry.shadowRoot.querySelector('sl-details').open).to.equal(false);
  });
});
