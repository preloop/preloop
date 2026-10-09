import { LitElement, html } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import { fixture, expect } from '@open-wc/testing';
import { ConsoleStatus } from './console-status';

@customElement('status-test-view')
class StatusTestView extends LitElement {
  readonly status = new ConsoleStatus(this);
  @state() loading = true;
  @state() error: string | null = null;
  @state() sectionErrors: Record<string, string> = {};
  @state() visible = true;
  render() {
    return this.visible ? html`<h1>Example</h1>` : html``;
  }
}

@customElement('underscored-status-test-view')
class UnderscoredStatusTestView extends LitElement {
  readonly status = new ConsoleStatus(this);
  @state() _loading = true;
  @state() _loadingDependencies = false;
  @state() _saving = false;
  @state() _artifactSettingsSaving = false;
  @state() _loadError: string | null = null;
  @state() _error: string | null = null;
  render() {
    return html`<h1>Example</h1>`;
  }
}

describe('ConsoleStatus', () => {
  it('tracks underscore-prefixed loading, saving and errors', async () => {
    const view = await fixture<UnderscoredStatusTestView>(
      html`<underscored-status-test-view></underscored-status-test-view>`
    );
    const text = () =>
      view.shadowRoot!.querySelector('[data-console-status]')!.textContent;
    expect(text()).to.equal('Loading updates.');
    view._loading = false;
    view._loadingDependencies = true;
    await view.updateComplete;
    expect(text()).to.equal('Loading updates.');
    view._loadingDependencies = false;
    await view.updateComplete;
    expect(text()).to.equal('Page ready.');
    view._saving = true;
    await view.updateComplete;
    expect(text()).to.equal('Loading updates.');
    view._saving = false;
    view._artifactSettingsSaving = true;
    await view.updateComplete;
    expect(text()).to.equal('Loading updates.');
    view._artifactSettingsSaving = false;
    view._loadError = 'Unavailable';
    await view.updateComplete;
    expect(text()).to.include('Could not complete');
    view._loadError = null;
    view._error = 'Unavailable';
    await view.updateComplete;
    expect(text()).to.include('Could not complete');
  });
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
    view.sectionErrors = { agents: 'Unavailable' };
    await view.updateComplete;
    expect(region().textContent).to.include('Could not complete');
    view.sectionErrors = {};
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
